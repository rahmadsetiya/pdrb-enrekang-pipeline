import psycopg
from prefect import flow, get_run_logger, task

from . import extract, load, pipeline, provenance
from .quality import (
    DataQualityError,
    QualityReport,
    check_loaded_data,
    check_processed_file,
)


def retry_transient_database_failure(task, task_run, state) -> bool:
    try:
        state.result()
    except psycopg.OperationalError:
        return True
    except Exception:
        return False
    return False


def log_quality_report(report: QualityReport) -> None:
    logger = get_run_logger()
    for line in report.lines():
        logger.info(line)


def run_quality_check(check, *args) -> QualityReport:
    try:
        report = check(*args)
    except DataQualityError as error:
        log_quality_report(error.report)
        raise
    log_quality_report(report)
    return report


@task(name="verify-source")
def verify_source_task() -> dict:
    metadata = provenance.load_and_verify(pipeline.RAW_PDF, pipeline.PROVENANCE)
    get_run_logger().info(
        "Verified canonical source publication=%s sha256=%s",
        metadata["publication_number"],
        metadata["sha256"][:12],
    )
    return metadata


@task(name="extract-table")
def extract_table_task(metadata: dict) -> extract.TableCandidate:
    page_text = extract.extract_page_text(pipeline.RAW_PDF)
    candidate = extract.parse_table_candidate(page_text)
    get_run_logger().info(
        "Extracted target page=%s candidate_rows=%s total_candidates=%s",
        metadata["target_table"]["pdf_page"],
        len(candidate.rows),
        len(candidate.total_candidates),
    )
    return candidate


@task(name="validate-table")
def validate_table_task(candidate: extract.TableCandidate) -> extract.ExtractedTable:
    table, report = extract.validate_table_with_report(candidate)
    log_quality_report(report)
    get_run_logger().info(
        "Validated industries=%s years=%s", len(table.rows), len(extract.YEARS)
    )
    return table


@task(name="normalize-observations")
def normalize_observations_task(table: extract.ExtractedTable) -> int:
    count = pipeline.materialize(table)
    get_run_logger().info(
        "Materialized observations=%s intermediate=%s processed=%s",
        count,
        pipeline.INTERMEDIATE,
        pipeline.PROCESSED,
    )
    return count


@task(name="check-processed-quality")
def check_processed_quality_task(normalized_count: int) -> QualityReport:
    report = run_quality_check(check_processed_file, pipeline.PROCESSED)
    get_run_logger().info(
        "Verified processed quality normalized_observations=%s", normalized_count
    )
    return report


@task(
    name="load-postgres",
    retries=2,
    retry_delay_seconds=[2, 5],
    retry_condition_fn=retry_transient_database_failure,
)
def load_postgres_task(database_url: str, processed_report: QualityReport) -> int:
    count = load.load_postgres(
        database_url,
        pipeline.PROCESSED,
        pipeline.PROVENANCE,
        pipeline.SCHEMA,
    )
    get_run_logger().info(
        "Loaded processed_quality=%s database_observations=%s",
        "PASS" if processed_report.passed else "FAIL",
        count,
    )
    return count


@task(
    name="check-loaded-quality",
    retries=2,
    retry_delay_seconds=[2, 5],
    retry_condition_fn=retry_transient_database_failure,
)
def check_loaded_quality_task(database_url: str, loaded_count: int) -> QualityReport:
    report = run_quality_check(check_loaded_data, database_url)
    get_run_logger().info(
        "Verified loaded quality loader_observations=%s",
        loaded_count,
    )
    return report


@flow(name="pdrb-enrekang-ingestion")
def pdrb_ingestion_flow(database_url: str) -> int:
    logger = get_run_logger()
    logger.info("Starting local PDRB ingestion flow")
    metadata = verify_source_task()
    candidate = extract_table_task(metadata)
    table = validate_table_task(candidate)
    normalized_count = normalize_observations_task(table)
    processed_report = check_processed_quality_task(normalized_count)
    loaded_count = load_postgres_task(database_url, processed_report)
    loaded_report = check_loaded_quality_task(database_url, loaded_count)
    verified_count = int(
        next(
            result.observed
            for result in loaded_report.results
            if result.rule_id == "row_count"
        )
    )
    logger.info("Completed local PDRB ingestion flow observations=%s", verified_count)
    return verified_count
