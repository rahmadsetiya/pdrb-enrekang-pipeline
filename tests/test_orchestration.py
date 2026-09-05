import unittest
from unittest.mock import Mock, call, patch

import psycopg
from prefect.logging import disable_run_logger

from pdrb_pipeline import orchestration
from pdrb_pipeline.quality import QualityReport, QualityResult


class FailedState:
    def __init__(self, error):
        self.error = error

    def result(self):
        raise self.error


class OrchestrationTests(unittest.TestCase):
    @staticmethod
    def passing_report(scope):
        return QualityReport(
            scope=scope,
            results=(QualityResult("row_count", True, "85", "85"),),
        )

    def test_tasks_have_explicit_stage_names_and_scoped_retries(self):
        tasks = (
            orchestration.verify_source_task,
            orchestration.extract_table_task,
            orchestration.validate_table_task,
            orchestration.normalize_observations_task,
            orchestration.check_processed_quality_task,
            orchestration.load_postgres_task,
            orchestration.check_loaded_quality_task,
        )

        self.assertEqual(
            [task.name for task in tasks],
            [
                "verify-source",
                "extract-table",
                "validate-table",
                "normalize-observations",
                "check-processed-quality",
                "load-postgres",
                "check-loaded-quality",
            ],
        )
        self.assertEqual([task.retries for task in tasks], [0, 0, 0, 0, 0, 2, 2])

    def test_retry_condition_only_accepts_operational_errors(self):
        operational = FailedState(psycopg.OperationalError("database unavailable"))
        deterministic = FailedState(ValueError("invalid table"))

        self.assertTrue(
            orchestration.retry_transient_database_failure(None, None, operational)
        )
        self.assertFalse(
            orchestration.retry_transient_database_failure(None, None, deterministic)
        )

    def test_flow_calls_tasks_in_dependency_order(self):
        manager = Mock()
        processed_report = self.passing_report("processed")
        loaded_report = self.passing_report("loaded")
        task_patches = {
            "verify": patch.object(
                orchestration, "verify_source_task", return_value={"source": "ok"}
            ),
            "extract": patch.object(
                orchestration, "extract_table_task", return_value="candidate"
            ),
            "validate": patch.object(
                orchestration, "validate_table_task", return_value="table"
            ),
            "normalize": patch.object(
                orchestration, "normalize_observations_task", return_value=85
            ),
            "processed_quality": patch.object(
                orchestration,
                "check_processed_quality_task",
                return_value=processed_report,
            ),
            "load": patch.object(
                orchestration, "load_postgres_task", return_value=85
            ),
            "loaded_quality": patch.object(
                orchestration,
                "check_loaded_quality_task",
                return_value=loaded_report,
            ),
        }

        started = {}
        for name, patcher in task_patches.items():
            started[name] = patcher.start()
            self.addCleanup(patcher.stop)
        for name, mocked_task in started.items():
            manager.attach_mock(mocked_task, name)

        with disable_run_logger():
            result = orchestration.pdrb_ingestion_flow.fn("postgresql://test")

        self.assertEqual(result, 85)
        self.assertEqual(
            manager.mock_calls,
            [
                call.verify(),
                call.extract({"source": "ok"}),
                call.validate("candidate"),
                call.normalize("table"),
                call.processed_quality(85),
                call.load("postgresql://test", processed_report),
                call.loaded_quality("postgresql://test", 85),
            ],
        )

    @patch("pdrb_pipeline.orchestration.pipeline.materialize", return_value=85)
    def test_normalization_task_delegates_to_domain_pipeline(self, materialize):
        table = object()
        with disable_run_logger():
            result = orchestration.normalize_observations_task.fn(table)

        self.assertEqual(result, 85)
        materialize.assert_called_once_with(table)

    @patch("pdrb_pipeline.orchestration.check_processed_file")
    def test_processed_quality_task_delegates_to_domain_quality(self, check):
        report = self.passing_report("processed")
        check.return_value = report

        with disable_run_logger():
            result = orchestration.check_processed_quality_task.fn(85)

        self.assertEqual(result, report)
        check.assert_called_once_with(orchestration.pipeline.PROCESSED)

    @patch("pdrb_pipeline.orchestration.check_loaded_data")
    def test_loaded_quality_task_delegates_to_domain_quality(self, check):
        report = self.passing_report("loaded")
        check.return_value = report

        with disable_run_logger():
            result = orchestration.check_loaded_quality_task.fn(
                "postgresql://test", 85
            )

        self.assertEqual(result, report)
        check.assert_called_once_with("postgresql://test")


if __name__ == "__main__":
    unittest.main()
