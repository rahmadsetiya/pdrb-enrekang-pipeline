import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg

from pdrb_pipeline.operational import (
    BLOCKED,
    FAIL,
    PASS,
    OperationalCheck,
    OperationalReport,
    check_backup,
    check_compose,
    check_disk,
    check_postgres,
    check_repository,
)
from pdrb_pipeline.quality import ObservationMetrics, evaluate_observation_metrics


def passing_check(check_id="test", required=True):
    return OperationalCheck(check_id, PASS, required, "passed", "OK")


class OperationalReportTests(unittest.TestCase):
    def test_optional_blocked_check_does_not_block_healthy_report(self):
        report = OperationalReport(
            (
                passing_check(),
                OperationalCheck(
                    "backup", BLOCKED, False, "not configured", "NOT_CONFIGURED"
                ),
            )
        )

        self.assertEqual(report.status, PASS)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(json.loads(report.to_json())["overall_status"], PASS)

    def test_required_fail_and_blocked_have_distinct_exit_codes(self):
        failed = OperationalReport(
            (OperationalCheck("db", FAIL, True, "failed", "UNREACHABLE"),)
        )
        blocked = OperationalReport(
            (OperationalCheck("db", BLOCKED, True, "blocked", "NOT_CONFIGURED"),)
        )

        self.assertEqual((failed.status, failed.exit_code), (FAIL, 1))
        self.assertEqual((blocked.status, blocked.exit_code), (BLOCKED, 2))

    def test_json_is_deterministic_and_machine_readable(self):
        report = OperationalReport((passing_check(),))

        first = report.to_json()
        second = report.to_json()

        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["schema_version"], 1)
        self.assertNotIn("generated_at", first)

    def test_human_failure_includes_cause_and_action(self):
        report = OperationalReport(
            (
                OperationalCheck(
                    "postgres",
                    FAIL,
                    True,
                    "connection failed",
                    "UNREACHABLE",
                    "Database is unavailable.",
                    "Inspect database status.",
                ),
            )
        )

        output = report.to_human()

        self.assertIn("[FAIL] postgres: connection failed", output)
        self.assertIn("Cause: Database is unavailable.", output)
        self.assertIn("Next: Inspect database status.", output)

    @patch("pdrb_pipeline.operational.build_operational_report")
    def test_status_cli_emits_json_and_returns_report_exit_code(self, build_report):
        from pdrb_pipeline.__main__ import main

        build_report.return_value = OperationalReport((passing_check(),))
        output = io.StringIO()

        with patch.object(sys, "argv", ["pdrb_pipeline", "status", "--format", "json"]):
            with patch("sys.stdout", output):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["overall_status"], PASS)


class CollectorTests(unittest.TestCase):
    @patch("pdrb_pipeline.operational._run_command")
    def test_repository_dirty_is_attributable_failure(self, run_command):
        run_command.return_value = subprocess.CompletedProcess(
            [], 0, "## master...origin/master\n M README.md\n?? local.txt\n", ""
        )

        result = check_repository(Path("/project"))

        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.observed["change_count"], 2)
        self.assertIn("git status", result.action)
        run_command.assert_called_once_with(
            (
                "git",
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "--branch",
            ),
            Path("/project"),
        )

    @patch("pdrb_pipeline.operational._run_command")
    def test_compose_permission_error_is_optional_blocked(self, run_command):
        run_command.return_value = subprocess.CompletedProcess(
            [], 1, "", "permission denied while connecting to the Docker daemon"
        )

        result = check_compose(Path("/project"))

        self.assertEqual(result.status, BLOCKED)
        self.assertFalse(result.required)
        self.assertEqual(result.observed, {"exit_code": 1})
        run_command.assert_called_once_with(
            ("docker", "compose", "ps", "--all", "--format", "json"),
            Path("/project"),
        )

    @patch("pdrb_pipeline.operational._run_command")
    def test_healthy_compose_database_passes(self, run_command):
        run_command.return_value = subprocess.CompletedProcess(
            [],
            0,
            '{"Health":"healthy","Service":"db","State":"running"}\n',
            "",
        )

        result = check_compose(Path("/project"))

        self.assertEqual(result.status, PASS)
        self.assertEqual(result.observed[0]["service"], "db")

    def test_unconfigured_postgres_is_required_blocked(self):
        result = check_postgres(None)

        self.assertEqual(result.status, BLOCKED)
        self.assertTrue(result.required)
        self.assertEqual(result.observed, "NOT_CONFIGURED")

    @patch("pdrb_pipeline.operational.evaluate_loaded_data")
    def test_healthy_postgres_reports_slice_metrics(self, evaluate):
        evaluate.return_value = evaluate_observation_metrics(
            "loaded",
            ObservationMetrics(85, 17, 2021, 2025, 5, 0, 0, 0, 0, 0, 0, 0),
        )

        result = check_postgres("database-url-marker")

        self.assertEqual(result.status, PASS)
        self.assertEqual(result.observed["row_count"], 85)
        self.assertEqual(result.observed["industry_count"], 17)
        self.assertIn("2021-2025", result.observed["year_range"])
        evaluate.assert_called_once_with(
            "database-url-marker", connect_timeout=3, read_only=True
        )

    @patch("pdrb_pipeline.operational.evaluate_loaded_data")
    def test_bad_loaded_data_reports_stable_quality_rule(self, evaluate):
        evaluate.return_value = evaluate_observation_metrics(
            "loaded",
            ObservationMetrics(84, 17, 2021, 2025, 5, 0, 0, 0, 0, 0, 0, 0),
        )

        result = check_postgres("database-url-marker")

        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.observed["failed_rules"], ["row_count"])
        self.assertIn("row_count", result.cause)

    @patch("pdrb_pipeline.operational.evaluate_loaded_data")
    def test_unreachable_postgres_does_not_expose_database_url(self, evaluate):
        database_url = "database-url-marker"
        evaluate.side_effect = psycopg.OperationalError("connection failed")

        result = check_postgres(database_url)
        rendered = json.dumps(result.to_dict())

        self.assertEqual(result.status, FAIL)
        self.assertNotIn(database_url, rendered)

    def test_backup_is_not_inferred_when_unconfigured(self):
        result = check_backup(Path("/project"), None)

        self.assertEqual(result.status, BLOCKED)
        self.assertFalse(result.required)
        self.assertEqual(result.observed, "NOT_CONFIGURED")

    def test_configured_backup_path_must_be_absolute(self):
        result = check_backup(Path("/project"), "relative/backups")

        self.assertEqual(result.status, FAIL)
        self.assertTrue(result.required)
        self.assertIn("absolute", result.summary)

    def test_configured_backup_reports_latest_matching_archive_only(self):
        with (
            tempfile.TemporaryDirectory() as project,
            tempfile.TemporaryDirectory() as backups,
        ):
            backup_dir = Path(backups)
            (backup_dir / "notes.txt").write_text("ignore", encoding="utf-8")
            older = backup_dir / "pdrb-enrekang-pipeline-20260905T010000Z.tar.gz"
            latest = backup_dir / "pdrb-enrekang-pipeline-20260906T010000Z.tar.gz"
            older.write_bytes(b"old")
            latest.write_bytes(b"latest")

            result = check_backup(Path(project), backups)

        self.assertEqual(result.status, PASS)
        self.assertTrue(result.required)
        self.assertEqual(result.observed["archive_count"], 2)
        self.assertEqual(result.observed["latest_name"], latest.name)

    @patch("pdrb_pipeline.operational.shutil.disk_usage")
    def test_high_disk_usage_is_attributable_failure(self, disk_usage):
        disk_usage.return_value = SimpleNamespace(total=100, used=90, free=10)

        result = check_disk(Path("/project"))

        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.observed["used_percent"], 90.0)
        self.assertIn("df -h", result.action)


if __name__ == "__main__":
    unittest.main()
