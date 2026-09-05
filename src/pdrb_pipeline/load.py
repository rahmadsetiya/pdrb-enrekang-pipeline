import csv
import json
from pathlib import Path

from .quality import EXPECTED_ROW_COUNT, check_loaded_data

EXPECTED_OBSERVATIONS = EXPECTED_ROW_COUNT
COUNT_OBSERVATIONS_SQL = """
    SELECT COUNT(*)
    FROM pdrb.observations
    WHERE region_code = '7316' AND series_code = 'adhb'
"""


def _count_observations(cursor) -> int:
    return cursor.execute(COUNT_OBSERVATIONS_SQL).fetchone()[0]


def count_observations(database_url: str) -> int:
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            return _count_observations(cursor)


def verify_observation_count(database_url: str) -> int:
    report = check_loaded_data(database_url)
    row_count = next(
        result.observed for result in report.results if result.rule_id == "row_count"
    )
    return int(row_count)


def load_postgres(
    database_url: str,
    processed_path: Path,
    provenance_path: Path,
    schema_path: Path,
) -> int:
    import psycopg

    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    with processed_path.open(encoding="utf-8") as source:
        observations = list(csv.DictReader(source))

    with psycopg.connect(database_url) as connection:
        connection.execute(schema_path.read_text(encoding="utf-8"))
        with connection.cursor() as cursor:
            source_id = cursor.execute(
                """
                INSERT INTO pdrb.source_datasets (
                    publisher, publication_title, publication_number,
                    publication_url, release_date, retrieved_at, raw_path, sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (publication_number) DO UPDATE SET
                    publisher = EXCLUDED.publisher,
                    publication_title = EXCLUDED.publication_title,
                    publication_url = EXCLUDED.publication_url,
                    release_date = EXCLUDED.release_date,
                    retrieved_at = EXCLUDED.retrieved_at,
                    raw_path = EXCLUDED.raw_path,
                    sha256 = EXCLUDED.sha256
                RETURNING id
                """,
                (
                    provenance["publisher"],
                    provenance["publication_title"],
                    provenance["publication_number"],
                    provenance["publication_url"],
                    provenance["release_date"],
                    provenance["retrieved_at"],
                    provenance["raw_file"],
                    provenance["sha256"],
                ),
            ).fetchone()[0]

            cursor.execute(
                """
                INSERT INTO pdrb.regions (bps_code, name, province_name)
                VALUES ('7316', 'Kabupaten Enrekang', 'Sulawesi Selatan')
                ON CONFLICT (bps_code) DO UPDATE SET
                    name = EXCLUDED.name,
                    province_name = EXCLUDED.province_name
                """
            )
            cursor.executemany(
                """
                INSERT INTO pdrb.industries (code, name) VALUES (%s, %s)
                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
                """,
                sorted(
                    {
                        (row["industry_code"], row["industry_name"])
                        for row in observations
                    }
                ),
            )
            cursor.executemany(
                """
                INSERT INTO pdrb.observations (
                    source_dataset_id, region_code, industry_code, year,
                    series_code, unit, value, publication_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (region_code, industry_code, year, series_code)
                DO UPDATE SET
                    source_dataset_id = EXCLUDED.source_dataset_id,
                    unit = EXCLUDED.unit,
                    value = EXCLUDED.value,
                    publication_status = EXCLUDED.publication_status
                """,
                [
                    (
                        source_id,
                        row["region_code"],
                        row["industry_code"],
                        int(row["year"]),
                        row["series_code"],
                        row["unit"],
                        row["value"],
                        row["publication_status"],
                    )
                    for row in observations
                ],
            )
            count = _count_observations(cursor)
    return count
