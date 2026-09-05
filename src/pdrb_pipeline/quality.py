import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping


EXPECTED_ROW_COUNT = 85
EXPECTED_INDUSTRY_COUNT = 17
EXPECTED_YEARS = (2021, 2022, 2023, 2024, 2025)
EXPECTED_SERIES_CODE = "adhb"
EXPECTED_UNIT = "billion_idr"
EXPECTED_STATUSES = {
    2021: "final",
    2022: "final",
    2023: "final",
    2024: "preliminary",
    2025: "very_preliminary",
}
REQUIRED_FIELDS = (
    "region_code",
    "region_name",
    "industry_code",
    "industry_name",
    "year",
    "series_code",
    "unit",
    "value",
    "publication_status",
)
KEY_FIELDS = ("region_code", "industry_code", "year", "series_code")


@dataclass(frozen=True)
class QualityResult:
    rule_id: str
    passed: bool
    observed: str
    expected: str

    def format(self, scope: str) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"quality scope={scope} rule={self.rule_id} status={status} "
            f"observed={self.observed} expected={self.expected}"
        )


@dataclass(frozen=True)
class QualityReport:
    scope: str
    results: tuple[QualityResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    def lines(self) -> tuple[str, ...]:
        return tuple(result.format(self.scope) for result in self.results)


class DataQualityError(ValueError):
    def __init__(self, report: QualityReport):
        self.report = report
        failures = "; ".join(
            result.format(report.scope)
            for result in report.results
            if not result.passed
        )
        super().__init__(f"Critical data quality failure: {failures}")


@dataclass(frozen=True)
class ObservationMetrics:
    row_count: int
    industry_count: int
    year_min: int | None
    year_max: int | None
    year_count: int
    invalid_year_count: int
    duplicate_count: int
    null_count: int
    invalid_series_count: int
    invalid_unit_count: int
    invalid_status_count: int
    invalid_or_non_positive_value_count: int


def require_quality(report: QualityReport) -> QualityReport:
    if not report.passed:
        raise DataQualityError(report)
    return report


def evaluate_observation_metrics(
    scope: str, metrics: ObservationMetrics
) -> QualityReport:
    expected_years = f"{EXPECTED_YEARS[0]}-{EXPECTED_YEARS[-1]} ({len(EXPECTED_YEARS)})"
    observed_years = (
        f"{metrics.year_min}-{metrics.year_max} ({metrics.year_count}), "
        f"invalid={metrics.invalid_year_count}"
    )
    results = (
        QualityResult(
            "row_count",
            metrics.row_count == EXPECTED_ROW_COUNT,
            str(metrics.row_count),
            str(EXPECTED_ROW_COUNT),
        ),
        QualityResult(
            "industry_count",
            metrics.industry_count == EXPECTED_INDUSTRY_COUNT,
            str(metrics.industry_count),
            str(EXPECTED_INDUSTRY_COUNT),
        ),
        QualityResult(
            "year_range",
            metrics.invalid_year_count == 0
            and metrics.year_min == EXPECTED_YEARS[0]
            and metrics.year_max == EXPECTED_YEARS[-1]
            and metrics.year_count == len(EXPECTED_YEARS),
            observed_years,
            expected_years,
        ),
        QualityResult(
            "duplicate_keys",
            metrics.duplicate_count == 0,
            str(metrics.duplicate_count),
            "0",
        ),
        QualityResult(
            "required_not_null",
            metrics.null_count == 0,
            str(metrics.null_count),
            "0",
        ),
        QualityResult(
            "series_code",
            metrics.invalid_series_count == 0,
            str(metrics.invalid_series_count),
            f"0 invalid; value={EXPECTED_SERIES_CODE}",
        ),
        QualityResult(
            "unit",
            metrics.invalid_unit_count == 0,
            str(metrics.invalid_unit_count),
            f"0 invalid; value={EXPECTED_UNIT}",
        ),
        QualityResult(
            "publication_status",
            metrics.invalid_status_count == 0,
            str(metrics.invalid_status_count),
            "0 invalid for year",
        ),
        QualityResult(
            "positive_values",
            metrics.invalid_or_non_positive_value_count == 0,
            str(metrics.invalid_or_non_positive_value_count),
            "0 invalid or non-positive",
        ),
    )
    return QualityReport(scope=scope, results=results)


def collect_observation_metrics(
    observations: Iterable[Mapping[str, object]],
) -> ObservationMetrics:
    rows = list(observations)
    industries = {
        str(row.get("industry_code", "")).strip()
        for row in rows
        if str(row.get("industry_code", "")).strip()
    }
    years: list[int] = []
    invalid_year_count = 0
    invalid_status_count = 0
    invalid_value_count = 0
    keys: list[tuple[object, ...]] = []

    for row in rows:
        keys.append(tuple(row.get(field) for field in KEY_FIELDS))
        try:
            year = int(str(row.get("year", "")))
            years.append(year)
        except (TypeError, ValueError):
            year = None
            invalid_year_count += 1

        if year is None or row.get("publication_status") != EXPECTED_STATUSES.get(year):
            invalid_status_count += 1

        try:
            value = Decimal(str(row.get("value", "")))
            if not value.is_finite() or value <= 0:
                invalid_value_count += 1
        except (InvalidOperation, TypeError, ValueError):
            invalid_value_count += 1

    duplicate_count = len(keys) - len(set(keys))
    null_count = sum(
        1
        for row in rows
        for field in REQUIRED_FIELDS
        if row.get(field) is None or str(row.get(field)).strip() == ""
    )
    return ObservationMetrics(
        row_count=len(rows),
        industry_count=len(industries),
        year_min=min(years) if years else None,
        year_max=max(years) if years else None,
        year_count=len(set(years)),
        invalid_year_count=invalid_year_count,
        duplicate_count=duplicate_count,
        null_count=null_count,
        invalid_series_count=sum(
            row.get("series_code") != EXPECTED_SERIES_CODE for row in rows
        ),
        invalid_unit_count=sum(row.get("unit") != EXPECTED_UNIT for row in rows),
        invalid_status_count=invalid_status_count,
        invalid_or_non_positive_value_count=invalid_value_count,
    )


def check_processed_file(path: Path) -> QualityReport:
    with path.open(encoding="utf-8", newline="") as source:
        metrics = collect_observation_metrics(csv.DictReader(source))
    return require_quality(evaluate_observation_metrics("processed", metrics))


LOADED_METRICS_SQL = """
    WITH scoped AS (
        SELECT source_dataset_id, region_code, industry_code, year,
               series_code, unit, value, publication_status
        FROM pdrb.observations
        WHERE region_code = %s
    ), duplicate_groups AS (
        SELECT COUNT(*) - 1 AS extras
        FROM scoped
        GROUP BY region_code, industry_code, year, series_code
        HAVING COUNT(*) > 1
    )
    SELECT
        COUNT(*),
        COUNT(DISTINCT industry_code),
        MIN(year),
        MAX(year),
        COUNT(DISTINCT year),
        COUNT(*) FILTER (WHERE year NOT BETWEEN %s AND %s),
        COALESCE((SELECT SUM(extras) FROM duplicate_groups), 0),
        COUNT(*) FILTER (
            WHERE source_dataset_id IS NULL OR region_code IS NULL
               OR industry_code IS NULL OR year IS NULL OR series_code IS NULL
               OR unit IS NULL OR value IS NULL OR publication_status IS NULL
        ),
        COUNT(*) FILTER (WHERE series_code <> %s),
        COUNT(*) FILTER (WHERE unit <> %s),
        COUNT(*) FILTER (
            WHERE publication_status <> CASE year
                WHEN %s THEN %s
                WHEN %s THEN %s
                WHEN %s THEN %s
                WHEN %s THEN %s
                WHEN %s THEN %s
            END
        ),
        COUNT(*) FILTER (WHERE value <= 0 OR value = 'NaN'::numeric)
    FROM scoped
"""


def collect_loaded_metrics(cursor, region_code: str = "7316") -> ObservationMetrics:
    status_parameters = tuple(
        item for pair in EXPECTED_STATUSES.items() for item in pair
    )
    values = cursor.execute(
        LOADED_METRICS_SQL,
        (
            region_code,
            EXPECTED_YEARS[0],
            EXPECTED_YEARS[-1],
            EXPECTED_SERIES_CODE,
            EXPECTED_UNIT,
            *status_parameters,
        ),
    ).fetchone()
    return ObservationMetrics(*values)


def check_loaded_data(database_url: str) -> QualityReport:
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            metrics = collect_loaded_metrics(cursor)
    return require_quality(evaluate_observation_metrics("loaded", metrics))


def check_published_totals(
    rows: Mapping[str, tuple[Decimal, ...]],
    published_totals: tuple[Decimal, ...],
    years: tuple[int, ...],
    tolerance: Decimal,
) -> QualityReport:
    failures = []
    max_delta = Decimal("0")
    for index, year in enumerate(years):
        calculated = sum(values[index] for values in rows.values())
        published = published_totals[index]
        delta = abs(calculated - published)
        max_delta = max(max_delta, delta)
        if delta > tolerance:
            failures.append(
                f"Industry sum for {year} is {calculated}, published total is {published}"
            )

    result = QualityResult(
        "published_total_reconciliation",
        not failures,
        ", ".join(failures) if failures else f"max_delta={max_delta}",
        f"delta<={tolerance}",
    )
    return require_quality(QualityReport(scope="extracted", results=(result,)))
