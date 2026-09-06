import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pdrb_pipeline.discovery import (
    GitHubIssue,
    IssueInspection,
    build_discovery_report,
    inspect_github_issues,
)
from pdrb_pipeline.operational import (
    BLOCKED,
    FAIL,
    PASS,
    OperationalCheck,
    OperationalReport,
)


def inspection(*issues):
    return IssueInspection(True, tuple(issues), f"Inspected {len(issues)} issue(s).")


def failed_check(check_id="postgres", cause="The check could not verify health."):
    return OperationalCheck(
        check_id,
        FAIL,
        True,
        "operational check failed",
        {"result": "UNREACHABLE"},
        cause,
        "Inspect the check.",
    )


class DiscoveryReportTests(unittest.TestCase):
    def test_pass_only_report_produces_no_candidates(self):
        status = OperationalReport(
            (OperationalCheck("postgres", PASS, True, "healthy", {"row_count": 85}),)
        )

        report = build_discovery_report(status, inspection())

        self.assertEqual(report.proposals, ())
        self.assertEqual(report.skipped_checks, ())

    def test_fail_produces_structurally_separate_proposal_sections(self):
        report = build_discovery_report(
            OperationalReport((failed_check("unknown-check"),)), inspection()
        )

        proposal = report.proposals[0]
        self.assertEqual(proposal.facts["status"], FAIL)
        self.assertEqual(proposal.hypotheses[0]["confidence"], "unverified")
        self.assertNotIn("hypotheses", proposal.facts)
        self.assertIn("required operational check", proposal.impact)
        self.assertEqual(proposal.affected_area, "unknown")
        self.assertTrue(proposal.acceptance_criteria)
        self.assertTrue(proposal.approval_required)
        self.assertTrue(proposal.approval_ready)

    def test_required_blocked_produces_candidate(self):
        blocked = OperationalCheck(
            "postgres",
            BLOCKED,
            True,
            "database check unavailable",
            {"result": "UNAVAILABLE"},
            "The check did not complete.",
        )

        report = build_discovery_report(
            OperationalReport((blocked,)), inspection()
        )

        self.assertEqual(len(report.proposals), 1)
        self.assertIn("health is unverified", report.proposals[0].impact)

    def test_optional_blocked_is_skipped(self):
        blocked = OperationalCheck(
            "backup", BLOCKED, False, "not configured", "NOT_CONFIGURED"
        )

        report = build_discovery_report(
            OperationalReport((blocked,)), inspection()
        )

        self.assertEqual(report.proposals, ())
        self.assertEqual(report.skipped_checks[0]["check_id"], "backup")

    def test_open_fingerprint_match_is_duplicate(self):
        first = build_discovery_report(
            OperationalReport((failed_check(),)), inspection()
        ).proposals[0]
        existing = GitHubIssue(
            21,
            first.title,
            f"<!-- pdrb-ops-fingerprint: {first.fingerprint} -->",
            "OPEN",
            "https://example.test/issues/21",
        )

        proposal = build_discovery_report(
            OperationalReport((failed_check(),)), inspection(existing)
        ).proposals[0]

        self.assertEqual(proposal.duplicate.classification, "OPEN_DUPLICATE")
        self.assertEqual(proposal.duplicate.matches[0].number, 21)
        self.assertFalse(proposal.approval_ready)

    def test_closed_fingerprint_match_is_recurrence(self):
        first = build_discovery_report(
            OperationalReport((failed_check(),)), inspection()
        ).proposals[0]
        existing = GitHubIssue(
            17,
            first.title,
            f"<!-- pdrb-ops-fingerprint: {first.fingerprint} -->",
            "CLOSED",
            "https://example.test/issues/17",
        )

        proposal = build_discovery_report(
            OperationalReport((failed_check(),)), inspection(existing)
        ).proposals[0]

        self.assertEqual(proposal.duplicate.classification, "RECURRENCE")
        self.assertTrue(proposal.approval_ready)

    def test_unmarked_open_match_is_possible_duplicate(self):
        first = build_discovery_report(
            OperationalReport((failed_check(),)), inspection()
        ).proposals[0]
        existing = GitHubIssue(
            29,
            first.title,
            "The postgres operational check is failing without a fingerprint.",
            "OPEN",
            "https://example.test/issues/29",
        )

        proposal = build_discovery_report(
            OperationalReport((failed_check(),)), inspection(existing)
        ).proposals[0]

        self.assertEqual(
            proposal.duplicate.classification, "POSSIBLE_DUPLICATE"
        )
        self.assertFalse(proposal.approval_ready)

    def test_github_unavailable_fails_closed(self):
        unavailable = IssueInspection(False, (), "GitHub inspection failed.")

        report = build_discovery_report(
            OperationalReport((failed_check(),)), unavailable
        )
        proposal = report.proposals[0]

        self.assertEqual(report.duplicate_inspection, BLOCKED)
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(proposal.duplicate.classification, "UNKNOWN")
        self.assertFalse(proposal.approval_ready)

    def test_json_is_deterministic(self):
        status = OperationalReport((failed_check(),))

        first = build_discovery_report(status, inspection()).to_json()
        second = build_discovery_report(status, inspection()).to_json()

        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["schema_version"], 1)
        self.assertNotIn("generated_at", first)

    def test_secret_like_values_are_redacted_or_excluded(self):
        check = OperationalCheck(
            "postgres",
            FAIL,
            True,
            "database_url=postgresql://user:password@example.test/db failed",
            {
                "result": "UNREACHABLE",
                "database_url": "postgresql://user:password@example.test/db",
            },
            "token=private-value could not connect",
        )

        rendered = build_discovery_report(
            OperationalReport((check,)), inspection()
        ).to_json()

        self.assertNotIn("password@example.test", rendered)
        self.assertNotIn("private-value", rendered)
        self.assertNotIn('"database_url"', rendered)
        self.assertIn("REDACTED", rendered)

    def test_secret_like_duplicate_metadata_is_redacted(self):
        first = build_discovery_report(
            OperationalReport((failed_check(),)), inspection()
        ).proposals[0]
        existing = GitHubIssue(
            31,
            "token=private-value",
            f"<!-- pdrb-ops-fingerprint: {first.fingerprint} -->",
            "OPEN",
            "https://example.test/issues/31?token=private-value",
        )

        rendered = build_discovery_report(
            OperationalReport((failed_check(),)), inspection(existing)
        ).to_json()

        self.assertNotIn("private-value", rendered)
        self.assertIn("REDACTED", rendered)


class GitHubInspectionTests(unittest.TestCase):
    @patch("pdrb_pipeline.discovery._run_command")
    def test_issue_inspection_uses_read_only_gh_command(self, run_command):
        run_command.return_value = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                [
                    {
                        "number": 10,
                        "title": "Example",
                        "body": "Body",
                        "state": "OPEN",
                        "url": "https://example.test/issues/10",
                    }
                ]
            ),
            "",
        )

        result = inspect_github_issues(Path("/project"))

        self.assertTrue(result.available)
        command = run_command.call_args.args[0]
        self.assertEqual(command[:3], ("gh", "issue", "list"))
        self.assertNotIn("create", command)
        self.assertNotIn("edit", command)
        self.assertNotIn("close", command)

    @patch("pdrb_pipeline.discovery._run_command")
    def test_issue_inspection_failure_returns_no_issue_data(self, run_command):
        run_command.return_value = subprocess.CompletedProcess(
            [], 1, "sensitive-output", "sensitive-error"
        )

        result = inspect_github_issues(Path("/project"))

        self.assertFalse(result.available)
        self.assertEqual(result.issues, ())
        self.assertNotIn("sensitive", result.summary)


class DiscoveryCliTests(unittest.TestCase):
    def test_cli_has_no_create_option(self):
        from pdrb_pipeline.__main__ import parse_args

        with patch.object(sys, "argv", ["pdrb_pipeline", "propose-issues", "--create"]):
            with self.assertRaises(SystemExit):
                parse_args()

    @patch("pdrb_pipeline.discovery.inspect_github_issues")
    @patch("pdrb_pipeline.operational.build_operational_report")
    @patch("pdrb_pipeline.__main__.parse_args")
    def test_cli_emits_proposals_without_mutation_path(
        self, parse_args, build_status, inspect_issues
    ):
        from pdrb_pipeline.__main__ import main

        parse_args.return_value = SimpleNamespace(
            command="propose-issues", format="json", database_only=False
        )
        build_status.return_value = OperationalReport((failed_check(),))
        inspect_issues.return_value = inspection()

        with patch("builtins.print") as print_output:
            exit_code = main()

        self.assertEqual(exit_code, 0)
        payload = json.loads(print_output.call_args.args[0])
        self.assertEqual(len(payload["proposals"]), 1)
        self.assertTrue(payload["proposals"][0]["approval_required"])


if __name__ == "__main__":
    unittest.main()
