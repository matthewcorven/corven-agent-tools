import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from issue_queue import (
    build_issue_index,
    build_issue_summaries,
    collect_linked_issue_numbers,
    filter_summaries_by_label,
    is_issue_available,
    matches_label_filter,
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


if __name__ == "__main__":
    unittest.main()
