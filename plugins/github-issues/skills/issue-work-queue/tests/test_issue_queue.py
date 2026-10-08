import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from issue_queue import (
    build_board_drift,
    build_flow_graph,
    build_issue_index,
    build_issue_summaries,
    collect_linked_issue_numbers,
    compute_flow_tiers,
    expected_board_status,
    filter_summaries_by_label,
    format_flow_ascii,
    format_flow_markdown,
    format_flow_mermaid,
    is_issue_available,
    load_project_config,
    matches_label_filter,
    parse_project_config,
    parse_project_items,
)


class IssueQueueTests(unittest.TestCase):
    def test_collect_linked_issue_numbers_uses_closing_issue_references(self) -> None:
        pull_requests = [
            {"closingIssuesReferences": [{"number": 12}, {"number": 25}]},
            {"closingIssuesReferences": [{"number": 12}]},
            {"closingIssuesReferences": []},
        ]

        self.assertEqual({12, 25}, collect_linked_issue_numbers(pull_requests))

    def test_is_issue_available_excludes_issues_with_linked_pull_requests(self) -> None:
        summary = {"number": 12, "state": "OPEN", "is_blocked": False}

        self.assertTrue(is_issue_available(summary, set()))
        self.assertFalse(is_issue_available(summary, {12}))

        blocked_summary = {"number": 13, "state": "OPEN", "is_blocked": True}
        self.assertFalse(is_issue_available(blocked_summary, set()))

    def test_matches_label_filter_treats_squad_as_member_squad_labels(self) -> None:
        issue = {"labels": [{"name": "squad:neo"}]}

        self.assertTrue(matches_label_filter(issue, "squad"))
        self.assertTrue(matches_label_filter(issue, "squad:neo"))
        self.assertFalse(matches_label_filter(issue, "squad:tank"))

    def test_filter_summaries_by_label_keeps_dependency_resolution_on_full_issue_graph(self) -> None:
        issues = [
            {"number": 1, "title": "Issue 1", "body": "## Depends On\n2", "state": "OPEN", "labels": [{"name": "squad:neo"}]},
            {"number": 2, "title": "Issue 2", "body": "", "state": "OPEN", "labels": [{"name": "squad:tank"}]},
        ]

        issue_index = build_issue_index(issues)
        summaries = build_issue_summaries(issues, issue_index)
        filtered_summaries = filter_summaries_by_label(summaries, issues, ["squad:neo"])

        self.assertEqual([1], [summary["number"] for summary in filtered_summaries])
        self.assertEqual("2", filtered_summaries[0]["unresolved_dependencies"][0]["reference"])
        self.assertEqual(False, filtered_summaries[0]["unresolved_dependencies"][0]["missing"])

    def test_collect_linked_issue_numbers_filters_by_pr_state(self) -> None:
        pull_requests = [
            {"state": "OPEN", "closingIssuesReferences": [{"number": 12}]},
            {"state": "MERGED", "closingIssuesReferences": [{"number": 25}]},
        ]

        self.assertEqual({12}, collect_linked_issue_numbers(pull_requests, "OPEN"))
        self.assertEqual({12, 25}, collect_linked_issue_numbers(pull_requests))

    def test_expected_board_status_derives_from_queue_state(self) -> None:
        self.assertEqual("Done", expected_board_status({"number": 1, "state": "CLOSED"}, set()))
        self.assertEqual("In Progress", expected_board_status({"number": 2, "state": "OPEN"}, {2}))
        self.assertEqual("Todo", expected_board_status({"number": 3, "state": "OPEN"}, {2}))

    def test_parse_project_items_filters_to_repo_case_insensitively(self) -> None:
        data = {
            "items": [
                {"content": {"repository": "Owner/Repo", "number": 1}, "status": "Done"},
                {"content": {"repository": "other/repo", "number": 2}, "status": "Todo"},
                {"status": "Todo"},
            ]
        }

        self.assertEqual({1: "Done"}, parse_project_items(data, "owner/repo"))

    def test_build_board_drift_classifies_mismatches(self) -> None:
        summaries = [
            {"number": 1, "state": "OPEN", "title": "match"},
            {"number": 2, "state": "OPEN", "title": "done but open"},
            {"number": 3, "state": "CLOSED", "title": "closed not done"},
            {"number": 4, "state": "OPEN", "title": "work started"},
            {"number": 5, "state": "OPEN", "title": "in progress no pr"},
            {"number": 6, "state": "OPEN", "title": "not on board"},
            {"number": 7, "state": "CLOSED", "title": "closed not on board"},
        ]
        board_statuses = {1: "Todo", 2: "Done", 3: "In Progress", 4: "Todo", 5: "In Progress"}

        result = build_board_drift(summaries, board_statuses, {4})

        kinds = {entry["number"]: entry["kind"] for entry in result["drift"]}
        self.assertEqual(
            {"done_but_open", "closed_not_done", "work_started_board_stale", "in_progress_without_pr"},
            set(kinds.values()),
        )
        self.assertEqual(3, result["drift_count"])
        self.assertEqual(1, result["informational_count"])
        self.assertEqual([6], [entry["number"] for entry in result["not_on_board"]])
        self.assertEqual(1, result["not_on_board_count"])
        self.assertTrue(next(entry for entry in result["drift"] if entry["number"] == 5)["informational"])

    def test_build_board_drift_flags_missing_status_and_unknown_values(self) -> None:
        summaries = [
            {"number": 1, "state": "OPEN", "title": "no status"},
            {"number": 2, "state": "OPEN", "title": "custom status"},
        ]
        board_statuses = {1: None, 2: "Blocked"}

        result = build_board_drift(summaries, board_statuses, set())

        kinds = {entry["kind"] for entry in result["drift"]}
        self.assertEqual({"missing_status", "unexpected_status"}, kinds)
        self.assertEqual(0, result["drift_count"])
        self.assertEqual(2, result["informational_count"])

    def test_parse_project_config_accepts_numbers_and_strings(self) -> None:
        self.assertEqual({"project": "3", "owner": None}, parse_project_config({"project": 3}, "cfg"))
        self.assertEqual(
            {"project": "12", "owner": "acme"},
            parse_project_config({"project": "12", "owner": "acme"}, "cfg"),
        )
        self.assertIsNone(parse_project_config({"owner": "acme"}, "cfg"))

        with self.assertRaises(ValueError):
            parse_project_config({"project": "abc"}, "cfg")
        with self.assertRaises(ValueError):
            parse_project_config(["project", 3], "cfg")

    def test_load_project_config_finds_repo_file_from_nested_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / ".git").mkdir()
            (root_path / ".github").mkdir()
            (root_path / ".github" / "issue-queue.json").write_text('{"project": 3}', encoding="utf-8")
            nested = root_path / "src" / "app"
            nested.mkdir(parents=True)

            self.assertEqual({"project": "3", "owner": None}, load_project_config(nested))

    def test_load_project_config_stops_at_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / ".github").mkdir()
            (root_path / ".github" / "issue-queue.json").write_text('{"project": 7}', encoding="utf-8")
            repo = root_path / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "src").mkdir()

            self.assertIsNone(load_project_config(repo / "src"))

    def test_load_project_config_raises_on_malformed_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / ".git").mkdir()
            (root_path / ".github").mkdir()
            (root_path / ".github" / "issue-queue.json").write_text("{not json", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_project_config(root_path)

    # ------------------------------------------------------------------
    # Flow diagram formats
    # ------------------------------------------------------------------

    @staticmethod
    def _flow_fixture() -> tuple:
        issues = [
            {"number": 1, "title": "Design", "body": "", "state": "CLOSED", "labels": []},
            {"number": 5, "title": "API", "body": "## Depends On\n1", "state": "OPEN", "labels": []},
            {"number": 7, "title": "UI", "body": "## Depends On\n#5", "state": "OPEN", "labels": []},
            {"number": 9, "title": "Docs", "body": "", "state": "OPEN", "labels": []},
            {"number": 15, "title": "Ghost", "body": "## Depends On\n#99", "state": "OPEN", "labels": []},
        ]
        index = build_issue_index(issues)
        summaries = build_issue_summaries(issues, index)
        available = [s for s in summaries if s["state"] == "OPEN" and not s["is_blocked"]]
        blocked = [s for s in summaries if s["state"] == "OPEN" and s["is_blocked"]]
        result = {
            "repo": "acme/demo",
            "state": "all",
            "generated_at": "2026-10-08T00:00:00+00:00",
            "next_available": available[0] if available else None,
            "available": available,
            "blocked": blocked,
            "mode": "queue",
        }
        return result, build_flow_graph(summaries, available, blocked, index)

    def test_build_flow_graph_kinds_and_edges(self) -> None:
        _, flow = self._flow_fixture()
        kinds = {node["id"]: node["kind"] for node in flow["nodes"]}
        self.assertEqual("done", kinds["n1"])
        # #5 depends on CLOSED #1 -> satisfied -> ready.
        self.assertEqual("ready", kinds["n5"])
        # #7 depends on OPEN #5 -> blocked.
        self.assertEqual("blocked", kinds["n7"])
        self.assertEqual("ready", kinds["n9"])
        self.assertEqual("missing", kinds["m_99"])

        edges = {(edge["from"], edge["to"]) for edge in flow["edges"]}
        self.assertIn(("n1", "n5"), edges)
        self.assertIn(("n5", "n7"), edges)
        self.assertIn(("m_99", "n15"), edges)

    def test_compute_flow_tiers_orders_by_dependency_depth(self) -> None:
        _, flow = self._flow_fixture()
        tiers, cyclic = compute_flow_tiers(flow)
        self.assertEqual(0, tiers["n9"])
        self.assertEqual(0, tiers["n15"])
        # Satisfied (done) references do not push ready work down a tier.
        self.assertEqual(0, tiers["n5"])
        self.assertEqual(1, tiers["n7"])
        self.assertEqual(set(), cyclic)

    def test_compute_flow_tiers_flags_every_cycle_member(self) -> None:
        issues = [
            {"number": 1, "title": "A", "body": "## Depends On\n#2", "state": "OPEN", "labels": []},
            {"number": 2, "title": "B", "body": "## Depends On\n#1", "state": "OPEN", "labels": []},
        ]
        index = build_issue_index(issues)
        summaries = build_issue_summaries(issues, index)
        flow = build_flow_graph(summaries, [], summaries, index)

        _, cyclic = compute_flow_tiers(flow)
        self.assertEqual({"n1", "n2"}, cyclic)

    def test_format_flow_ascii_tiers_and_missing_section(self) -> None:
        result, flow = self._flow_fixture()
        output = format_flow_ascii(result, flow)

        self.assertIn("Tier 0:", output)
        self.assertIn("- #9 Docs", output)
        self.assertIn("- #5 API", output)
        self.assertIn("Tier 1:", output)
        self.assertIn("- #7 UI (needs #5)", output)
        self.assertIn("99 (missing) ← #15 Ghost", output)
        # Satisfied CLOSED referents never appear as actionable tiers.
        self.assertNotIn("- #1 Design", output)

    def test_format_flow_mermaid_emits_classes_and_links(self) -> None:
        result, flow = self._flow_fixture()
        output = format_flow_mermaid(result, flow)

        self.assertTrue(output.startswith("flowchart TD"))
        self.assertIn('n9["#9 Docs"]("https://github.com/acme/demo/issues/9")', output)
        self.assertIn("n1 --> n5", output)
        self.assertIn("classDef missing", output)
        self.assertIn("class m_99 missing", output)
        self.assertIn("class n1 done", output)

    def test_format_flow_mermaid_escapes_quotes_in_titles(self) -> None:
        issues = [{"number": 3, "title": 'Say "hi"', "body": "", "state": "OPEN", "labels": []}]
        index = build_issue_index(issues)
        summaries = build_issue_summaries(issues, index)
        flow = build_flow_graph(summaries, summaries, [], index)
        result = {"repo": "a/b", "state": "all", "generated_at": "x", "available": summaries, "blocked": [], "mode": "queue"}

        output = format_flow_mermaid(result, flow)
        self.assertIn("Say 'hi'", output)
        self.assertNotIn('"Say', output.split("flowchart TD", 1)[1].split("\n", 1)[1])

    def test_format_flow_markdown_embeds_mermaid_block_and_lists(self) -> None:
        result, flow = self._flow_fixture()
        output = format_flow_markdown(result, flow)

        self.assertIn("# Dependency flow — acme/demo", output)
        self.assertIn("```mermaid", output)
        self.assertIn("## Ready", output)
        self.assertIn("- #9 Docs", output)
        self.assertIn("## Blocked", output)
        self.assertIn("- #7 UI (needs #5)", output)


if __name__ == "__main__":
    unittest.main()
