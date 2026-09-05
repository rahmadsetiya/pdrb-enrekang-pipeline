import os
from pathlib import Path

from .extract import (
    ExtractedTable,
    extract_page_text,
    normalize,
    parse_table,
    write_intermediate,
    write_observations,
)
from .provenance import load_and_verify


PROJECT_ROOT = Path(os.environ.get("PDRB_PROJECT_ROOT", Path.cwd())).resolve()
RAW_PDF = PROJECT_ROOT / "data/raw/bps/pdrb_enrekang_by_industry_2021_2025.pdf"
PROVENANCE = RAW_PDF.with_suffix(".provenance.json")
INTERMEDIATE = PROJECT_ROOT / "data/intermediate/pdrb_adhb_by_industry_wide.csv"
PROCESSED = PROJECT_ROOT / "data/processed/pdrb_adhb_observations.csv"
SCHEMA = PROJECT_ROOT / "sql/001_initial_schema.sql"


def transform(executable: str = "pdftotext") -> int:
    load_and_verify(RAW_PDF, PROVENANCE)
    page_text = extract_page_text(RAW_PDF, executable=executable)
    table = parse_table(page_text)
    return materialize(table)


def materialize(table: ExtractedTable) -> int:
    write_intermediate(table, INTERMEDIATE)
    observations = normalize(table)
    write_observations(observations, PROCESSED)
    return len(observations)
