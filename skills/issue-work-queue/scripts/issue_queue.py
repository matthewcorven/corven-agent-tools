#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set


def run_gh(args: List[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "gh command failed")
    return result.stdout


def get_repo_name() -> str:
    try:
        return run_gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()
    except RuntimeError as exc:
        # Portable plugin build: no hardcoded fallback repo. Fail loudly instead of
        # silently querying a repository the operator never named.
        raise RuntimeError(
            "Could not detect the current repository; pass --repo <owner/repo>."
        ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover the next GitHub issues that are ready to work on based on their issue-body dependency sections.")
    parser.add_argument(
        "--mode",
        choices=["queue", "status"],
        default="queue",
        help="Output mode: queue preserves the existing ready-vs-blocked view; status adds Ralph/board counts.",
    )
    parser.add_argument("--repo", default=None, help="GitHub repository (owner/repo). Defaults to the current repo.")
    parser.add_argument(
        "--state",
        default="all",
        choices=["open", "all"],
        help=(
            "Issue state to fetch for dependency resolution (default: all). "
            "Closed issues are used only to resolve Depends On / Blocked By references and never appear in output lists. "
            "Use 'open' to reproduce the legacy resolution view."
        ),
    )
    parser.add_argument("--label", action="append", default=None, help="Label to include. Repeat for multiple labels. Defaults to squad.")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format (default: json).")
    parser.add_argument("--limit", type=int, default=1000, help="Maximum issues to fetch from GitHub (default: 1000).")
    parser.add_argument(
        "--project",
        default=None,
        help="GitHub Projects V2 number. When set (or declared in the repo's .github/issue-queue.json), additionally reports drift between the board's Status field and the queue state as an opt-in diagnostic (never affects readiness).",
    )
    parser.add_argument(
        "--project-owner",
        default=None,
        help="Owner (user or organization) of the project. Defaults to the repository owner from --repo.",
    )
    return parser.parse_args()


def normalize_eol(value: str) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n")


def parse_task_id(title: str) -> Optional[str]:
    match = re.search(r"\[Task\s+([0-9]+(?:\.[0-9]+)*(?:[a-z])?)\]", title or "", re.IGNORECASE)
    return match.group(1) if match else None


def extract_section_body(body: str, heading: str) -> str:
    lines = normalize_eol(body or "").split("\n")
    in_section = False
    section_lines: List[str] = []

    for line in lines:
        trimmed = line.strip()
        if not in_section:
            match = re.match(r"^#{2,3}\s+(.+)$", trimmed)
            if match and match.group(1).strip().lower() == heading.lower():
                in_section = True
            continue

        if re.match(r"^#{2,3}\s+", trimmed):
            break

        section_lines.append(line)

    return "\n".join(section_lines).strip()


def parse_section_references(body: str, heading: str) -> List[str]:
    section_body = extract_section_body(body, heading)
    if not section_body:
        return []

    references: List[str] = []
    token_pattern = re.compile(r"(?<![\w.])(?:#)?([0-9]+(?:\.[0-9]+)*(?:[a-z])?)(?![\w.])")
    for raw_line in section_body.splitlines():
        line = raw_line.strip()
        if not line or re.match(r"^(none|n/a)$", line, re.IGNORECASE):
            continue

        cleaned = re.sub(r"^[-*]\s*", "", line)
        cleaned = cleaned.replace("`", "").strip()
        if not cleaned or re.match(r"^(none|n/a)$", cleaned, re.IGNORECASE):
            continue

        for match in token_pattern.finditer(cleaned):
            references.append(match.group(1))

    return references


def has_label(issue: Dict[str, object], label_name: str) -> bool:
    labels = issue.get("labels", []) or []
    return any((label.get("name") or "").lower() == label_name.lower() for label in labels)


def has_member_label(issue: Dict[str, object]) -> bool:
    labels = issue.get("labels", []) or []
    return any((label.get("name") or "").lower().startswith("squad:") for label in labels)


def matches_label_filter(issue: Dict[str, object], label_name: str) -> bool:
    normalized_label = (label_name or "").lower()
    if normalized_label == "squad":
        return has_label(issue, "squad") or has_member_label(issue)
    return has_label(issue, label_name)


def fetch_issues(repo: str, state: str, limit: int) -> List[Dict[str, object]]:
    command_args = [
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        str(limit),
        "--json",
        "number,title,body,state,labels",
    ]
    return json.loads(run_gh(command_args))


def fetch_pull_requests(repo: str, state: str, limit: int) -> List[Dict[str, object]]:
    command_args = [
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        str(limit),
        "--json",
        "number,title,isDraft,state,closingIssuesReferences",
    ]
    return json.loads(run_gh(command_args))


def collect_linked_issue_numbers(pull_requests: List[Dict[str, object]], state: Optional[str] = None) -> Set[int]:
    linked_issue_numbers: Set[int] = set()
    for pull_request in pull_requests:
        if state is not None and pull_request.get("state") != state:
            continue
        for reference in pull_request.get("closingIssuesReferences") or []:
            if not isinstance(reference, dict):
                continue
            number = reference.get("number")
            if number is None:
                continue
            linked_issue_numbers.add(int(number))
    return linked_issue_numbers


def is_issue_available(summary: Dict[str, object], linked_issue_numbers: Set[int]) -> bool:
    return summary.get("state") == "OPEN" and not summary.get("is_blocked") and int(summary["number"]) not in linked_issue_numbers


def expected_board_status(summary: Dict[str, object], linked_open_issue_numbers: Set[int]) -> str:
    """Queue-derived expectation for the board's Status field.

    CLOSED -> Done; OPEN with a linked open PR -> In Progress; otherwise -> Todo.
    The board has no Blocked state, so blocked work is expected to sit in Todo.
    """
    if summary.get("state") != "OPEN":
        return "Done"
    if int(summary["number"]) in linked_open_issue_numbers:
        return "In Progress"
    return "Todo"


def parse_project_items(data: Dict[str, object], repo: str) -> Dict[int, Optional[str]]:
    """Map issue number -> board Status for project items belonging to `repo` (case-insensitive)."""
    normalized_repo = (repo or "").lower()
    statuses: Dict[int, Optional[str]] = {}
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, dict):
            continue
        if str(content.get("repository") or "").lower() != normalized_repo:
            continue
        number = content.get("number")
        if number is None:
            continue
        statuses[int(number)] = item.get("status")
    return statuses


def fetch_project_items(project: str, owner: str, repo: str, limit: int) -> Dict[int, Optional[str]]:
    output = run_gh(["project", "item-list", project, "--owner", owner, "--limit", str(limit), "--format", "json"])
    return parse_project_items(json.loads(output), repo)


def find_repo_config(start: Optional[object] = None) -> Optional[str]:
    """Locate `.github/issue-queue.json` from `start` (cwd by default) upward, stopping at the repo root.

    The first directory containing `.git` is treated as the repo root and is
    still checked for the config, but the walk never escapes the repository.
    """
    current = Path(start) if start is not None else Path.cwd()
    current = current.resolve()
    for directory in [current, *current.parents]:
        candidate = directory / ".github" / "issue-queue.json"
        if candidate.is_file():
            return str(candidate)
        if (directory / ".git").exists():
            return None
    return None


def parse_project_config(data: object, source: str) -> Optional[Dict[str, Optional[str]]]:
    """Validate a parsed issue-queue config. Returns None when it declares no project.

    Raises ValueError on malformed content so callers can surface the problem
    (this is a diagnostic — errors must be visible but never fatal to the queue).
    """
    if not isinstance(data, dict):
        raise ValueError(f"{source}: config must be a JSON object")

    project = data.get("project")
    if project is None:
        return None

    project_text = str(project).strip()
    if not project_text.isdigit():
        raise ValueError(f"{source}: 'project' must be a project number, got {project!r}")

    owner = data.get("owner")
    if owner is not None and not (isinstance(owner, str) and owner.strip()):
        raise ValueError(f"{source}: 'owner' must be a non-empty string when present")

    return {"project": project_text, "owner": owner.strip() if isinstance(owner, str) else None}


def load_project_config(start: Optional[object] = None) -> Optional[Dict[str, Optional[str]]]:
    """Load the repo's `.github/issue-queue.json`, or None when the repo declares no board."""
    path = find_repo_config(start)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return parse_project_config(data, path)


def build_board_drift(
    visible_summaries: List[Dict[str, object]],
    board_statuses: Dict[int, Optional[str]],
    linked_open_issue_numbers: Set[int],
) -> Dict[str, object]:
    """Compare the board's Status field against queue-derived expectations.

    Diagnostic only: entries here never change readiness. They tell the
    receiving agent exactly where the board and the queue disagree, and
    which side to trust (the queue — see the precedence rule in SKILL.md).
    """
    drift: List[Dict[str, object]] = []
    not_on_board: List[Dict[str, object]] = []

    for summary in visible_summaries:
        number = int(summary["number"])
        title = str(summary.get("title") or "")
        expected = expected_board_status(summary, linked_open_issue_numbers)

        if number not in board_statuses:
            if summary.get("state") == "OPEN":
                not_on_board.append({"number": number, "title": title, "expected_status": expected})
            continue

        actual = board_statuses[number]
        if actual == expected:
            continue

        if actual is None:
            kind, informational, hint = (
                "missing_status",
                True,
                "Board item has no Status value; set one so the card is interpretable.",
            )
        elif actual == "Done" and expected != "Done":
            kind, informational, hint = (
                "done_but_open",
                False,
                "Board says Done but the issue is still OPEN; verify the work finished or move the card back.",
            )
        elif expected == "Done":
            kind, informational, hint = (
                "closed_not_done",
                False,
                f"Issue is CLOSED but the board still shows {actual}; the card is stale.",
            )
        elif expected == "In Progress":
            kind, informational, hint = (
                "work_started_board_stale",
                False,
                f"A linked open PR exists but the board shows {actual}; move the card to In Progress.",
            )
        elif actual == "In Progress":
            kind, informational, hint = (
                "in_progress_without_pr",
                True,
                "Board shows In Progress but there is no linked open PR; work may be pre-PR. Informational only.",
            )
        else:
            kind, informational, hint = (
                "unexpected_status",
                True,
                f"Board status '{actual}' is outside the queue's Todo/In Progress/Done model; informational.",
            )

        drift.append(
            {
                "number": number,
                "title": title,
                "board_status": actual,
                "expected_status": expected,
                "kind": kind,
                "informational": informational,
                "hint": hint,
            }
        )

    drift_count = sum(1 for entry in drift if not entry["informational"])
    return {
        "drift": drift,
        "drift_count": drift_count,
        "informational_count": len(drift) - drift_count,
        "not_on_board": not_on_board,
        "not_on_board_count": len(not_on_board),
    }


def build_issue_index(issues: List[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    by_task_id: Dict[str, Dict[str, object]] = {}
    by_number: Dict[int, Dict[str, object]] = {}
    for issue in issues:
        number = int(issue["number"])
        by_number[number] = issue
        task_id = parse_task_id(str(issue.get("title") or ""))
        if task_id:
            by_task_id[task_id] = issue
    return {"by_task_id": by_task_id, "by_number": by_number}


def resolve_reference(reference: str, issue_index: Dict[str, Dict[str, object]]) -> Optional[Dict[str, object]]:
    normalized = reference.strip()
    if not normalized:
        return None

    if "." in normalized:
        return issue_index["by_task_id"].get(normalized)

    try:
        issue_number = int(normalized)
    except ValueError:
        return None

    return issue_index["by_number"].get(issue_number)


def build_issue_summaries(issues: List[Dict[str, object]], issue_index: Dict[str, Dict[str, object]]) -> List[Dict[str, object]]:
    summaries: List[Dict[str, object]] = []
    for issue in issues:
        title = str(issue.get("title") or "")
        body = str(issue.get("body") or "")
        task_id = parse_task_id(title)
        # Readiness is driven by the issue body sections, not the title.
        # "Depends On" and "Blocked By" are the source of truth for ordering.
        depends_on = parse_section_references(body, "Depends On")
        blocked_by = parse_section_references(body, "Blocked By")

        # A reference is unsatisfied only when its referent is OPEN. A CLOSED referent is
        # satisfied (completed work cannot block). A missing referent means the referenced
        # issue does not exist in any state — a distinct condition from an open referent,
        # so missing entries stay flagged as missing in both JSON and text output.
        unresolved_dependencies = []
        for reference in depends_on:
            resolved = resolve_reference(reference, issue_index)
            if not resolved or resolved.get("state") == "OPEN":
                unresolved_dependencies.append({"reference": reference, "resolved": resolved, "missing": resolved is None})

        unresolved_blockers = []
        for reference in blocked_by:
            resolved = resolve_reference(reference, issue_index)
            if not resolved or resolved.get("state") == "OPEN":
                unresolved_blockers.append({"reference": reference, "resolved": resolved, "missing": resolved is None})

        summaries.append(
            {
                "number": int(issue["number"]),
                "task_id": task_id,
                "title": title,
                "state": issue.get("state"),
                "depends_on": depends_on,
                "blocked_by": blocked_by,
                "unresolved_dependencies": unresolved_dependencies,
                "unresolved_blockers": unresolved_blockers,
                "is_blocked": bool(unresolved_dependencies or unresolved_blockers),
            }
        )

    summaries.sort(key=lambda item: int(item["number"]))
    return summaries


def filter_summaries_by_label(
    summaries: List[Dict[str, object]],
    issues: List[Dict[str, object]],
    labels: List[str],
) -> List[Dict[str, object]]:
    if not labels:
        return list(summaries)

    issues_by_number = {int(issue["number"]): issue for issue in issues}
    visible_summaries = []
    for summary in summaries:
        issue = issues_by_number.get(int(summary["number"]))
        if issue and all(matches_label_filter(issue, label) for label in labels):
            visible_summaries.append(summary)
    return visible_summaries


def _format_reference(entry: Dict[str, object]) -> str:
    if entry.get("resolved"):
        return f"#{entry['resolved']['number']}"
    # Missing referent: no issue exists with this task id / number in any state.
    # Kept visually distinct from an open referent so broken references cannot hide.
    return f"{entry['reference']} (missing)"


def format_queue_text(result: Dict[str, object]) -> str:
    lines = [f"Repository: {result['repo']}", f"State: {result['state']}", ""]

    if result.get("next_available"):
        lines.append("Next available:")
        lines.append(f"- #{result['next_available']['number']} {result['next_available']['title']}")
        lines.append("")
    else:
        lines.append("Next available: none")
        lines.append("")

    lines.append("Available:")
    available = result.get("available") or []
    if available:
        for issue in available:
            lines.append(f"- #{issue['number']} {issue['title']}")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Blocked:")
    blocked = result.get("blocked") or []
    if blocked:
        for issue in blocked:
            reasons = []
            if issue.get("unresolved_dependencies"):
                deps = ", ".join(
                    _format_reference(entry)
                    for entry in issue["unresolved_dependencies"]
                )
                reasons.append(f"depends on {deps}")
            if issue.get("unresolved_blockers"):
                blockers = ", ".join(
                    _format_reference(entry)
                    for entry in issue["unresolved_blockers"]
                )
                reasons.append(f"blocked by {blockers}")
            lines.append(f"- #{issue['number']} {issue['title']} ({'; '.join(reasons)})")
    else:
        lines.append("- none")

    board = result.get("board")
    if board:
        lines.append("")
        lines.extend(format_board_text(board))

    return "\n".join(lines)


def format_status_text(result: Dict[str, object]) -> str:
    lines = [f"Repository: {result['repo']}", f"State: {result['state']}", "", "Board status:"]

    next_available = result.get("next_available")
    if next_available:
        lines.append(f"- Next available: #{next_available['number']} {next_available['title']}")
    else:
        lines.append("- Next available: none")

    lines.append(f"- Available count: {result['available_count']}")
    lines.append(f"- Blocked count: {result['blocked_count']}")
    lines.append(f"- Untriaged squad issues: {result['untriaged_count']}")
    lines.append(f"- Member-assigned squad issues: {result['assigned_count']}")
    lines.append(f"- Open PR count: {result['open_pr_count']}")
    lines.append(f"- Draft PR count: {result['draft_pr_count']}")
    lines.append("")

    lines.append("Available:")
    available = result.get("available") or []
    if available:
        for issue in available:
            lines.append(f"- #{issue['number']} {issue['title']}")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Blocked:")
    blocked = result.get("blocked") or []
    if blocked:
        for issue in blocked:
            reasons = []
            if issue.get("unresolved_dependencies"):
                deps = ", ".join(
                    _format_reference(entry)
                    for entry in issue["unresolved_dependencies"]
                )
                reasons.append(f"depends on {deps}")
            if issue.get("unresolved_blockers"):
                blockers = ", ".join(
                    _format_reference(entry)
                    for entry in issue["unresolved_blockers"]
                )
                reasons.append(f"blocked by {blockers}")
            lines.append(f"- #{issue['number']} {issue['title']} ({'; '.join(reasons)})")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Untriaged squad issues:")
    untriaged = result.get("untriaged") or []
    if untriaged:
        for issue in untriaged:
            lines.append(f"- #{issue['number']} {issue['title']}")
    else:
        lines.append("- none")

    board = result.get("board")
    if board:
        lines.append("")
        lines.extend(format_board_text(board))

    return "\n".join(lines)


def format_board_text(board: Dict[str, object]) -> List[str]:
    project = board.get("project")
    location = ""
    if project is not None:
        location = f" (project #{project}"
        if board.get("owner"):
            location += f" @{board['owner']}"
        location += ")"
    header = f"Board drift{location}:"
    if board.get("error"):
        return [header, f"- unavailable: {board['error']}"]

    lines = [header]
    drift = board.get("drift") or []
    if drift:
        for entry in drift:
            board_status = entry.get("board_status") or "unset"
            marker = "; informational" if entry.get("informational") else ""
            lines.append(
                f"- #{entry['number']} {entry['title']} — board: {board_status}, "
                f"expected: {entry['expected_status']} [{entry['kind']}{marker}] {entry['hint']}"
            )
    else:
        lines.append("- none")

    not_on_board = board.get("not_on_board") or []
    if not_on_board:
        numbers = ", ".join(f"#{entry['number']}" for entry in not_on_board)
        lines.append(f"- Open queue issues not on the board ({len(not_on_board)}): {numbers}")

    return lines


def main() -> int:
    args = parse_args()
    repo = args.repo or get_repo_name()
    labels = [label for label in (args.label or ["squad"]) if label]

    issues = fetch_issues(repo, args.state, args.limit)

    issue_graph = [issue for issue in issues if not issue.get("pull_request")]
    issue_index = build_issue_index(issue_graph)
    summaries = build_issue_summaries(issue_graph, issue_index)
    visible_summaries = filter_summaries_by_label(summaries, issue_graph, labels)
    pull_requests = fetch_pull_requests(repo, "all", args.limit)
    linked_issue_numbers = collect_linked_issue_numbers(pull_requests)
    linked_open_issue_numbers = collect_linked_issue_numbers(pull_requests, "OPEN")
    available = [issue for issue in visible_summaries if is_issue_available(issue, linked_issue_numbers)]
    blocked = [issue for issue in visible_summaries if issue["state"] == "OPEN" and issue["is_blocked"]]

    queue_result = {
        "repo": repo,
        "state": args.state,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "next_available": available[0] if available else None,
        "available": available,
        "blocked": blocked,
    }

    # Board drift diagnostic: project from the --project flag, else the repo's
    # .github/issue-queue.json (repo declares, tool reads — the agent never has
    # to know the number). Never feeds back into readiness; failures degrade to
    # board.error so the core queue always renders.
    project = args.project
    project_owner = args.project_owner
    config_error: Optional[str] = None
    if project is None:
        try:
            project_config = load_project_config()
        except ValueError as exc:
            project_config = None
            config_error = str(exc)
        if project_config is not None:
            project = project_config.get("project")
            if project_owner is None:
                project_owner = project_config.get("owner")

    if project is not None or config_error is not None:
        board_result: Dict[str, object] = {}
        if project is not None:
            owner = project_owner or repo.split("/")[0]
            board_result["project"] = project
            board_result["owner"] = owner
            try:
                board_statuses = fetch_project_items(project, owner, repo, args.limit)
                board_result.update(build_board_drift(visible_summaries, board_statuses, linked_open_issue_numbers))
            except Exception as exc:
                board_result["error"] = str(exc)
        else:
            board_result["error"] = f"invalid issue-queue config: {config_error}"
        queue_result["board"] = board_result

    if args.mode == "status":
        # Board counts are always computed from OPEN issues only. When the fetch state is
        # already "open" reuse it; otherwise make the one extra fetch. With the default
        # "all" this costs a single additional gh call and keeps board semantics unchanged.
        open_issues = issues if args.state == "open" else fetch_issues(repo, "open", args.limit)
        open_squad_issues = [issue for issue in open_issues if matches_label_filter(issue, "squad")]
        untriaged = [issue for issue in open_squad_issues if not has_member_label(issue)]
        assigned = [issue for issue in open_squad_issues if has_member_label(issue)]
        pull_requests = fetch_pull_requests(repo, "all", args.limit)
        open_pull_requests = [pr for pr in pull_requests if pr.get("state") == "OPEN"]

        result = {
            **queue_result,
            "mode": "status",
            "available_count": len(available),
            "blocked_count": len(blocked),
            "untriaged_count": len(untriaged),
            "assigned_count": len(assigned),
            "untriaged": [{"number": int(issue["number"]), "title": str(issue.get("title") or "")} for issue in untriaged],
            "open_pr_count": len(open_pull_requests),
            "draft_pr_count": sum(1 for pr in open_pull_requests if pr.get("isDraft")),
        }
    else:
        result = queue_result

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        formatter = format_status_text if args.mode == "status" else format_queue_text
        print(formatter(result))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - simple CLI wrapper
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
