import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from issue_queue import (
    build_board_drift,
    build_issue_index,
    build_issue_summaries,
    collect_linked_issue_numbers,
    expected_board_status,
    filter_summaries_by_label,
    is_issue_available,
    matches_label_filter,
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


if __name__ == "__main__":
    unittest.main()
