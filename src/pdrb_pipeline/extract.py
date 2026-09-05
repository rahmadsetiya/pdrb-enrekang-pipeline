import csv
import re
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .quality import (
    EXPECTED_SERIES_CODE,
    EXPECTED_STATUSES,
    EXPECTED_UNIT,
    EXPECTED_YEARS,
    QualityReport,
    check_published_totals,
)


PDF_PAGE = 127
YEARS = EXPECTED_YEARS
YEAR_HEADERS = ("2021", "2022", "2023", "2024*", "2025**")
YEAR_STATUSES = EXPECTED_STATUSES
INDUSTRIES = {
    "A": "Pertanian, Kehutanan, dan Perikanan",
    "B": "Pertambangan dan Penggalian",
    "C": "Industri Pengolahan",
    "D": "Pengadaan Listrik dan Gas",
    "E": "Pengadaan Air, Pengelolaan Sampah, Limbah dan Daur Ulang",
    "F": "Konstruksi",
    "G": "Perdagangan Besar dan Eceran; Reparasi Mobil dan Sepeda Motor",
    "H": "Transportasi dan Pergudangan",
    "I": "Penyediaan Akomodasi dan Makan Minum",
    "J": "Informasi dan Komunikasi",
    "K": "Jasa Keuangan dan Asuransi",
    "L": "Real Estat",
    "M,N": "Jasa Perusahaan",
    "O": "Administrasi Pemerintahan, Pertahanan dan Jaminan Sosial Wajib",
    "P": "Jasa Pendidikan",
    "Q": "Jasa Kesehatan dan Kegiatan Sosial",
    "R,S,T,U": "Jasa lainnya",
}
VALUE_PATTERN = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}")
CODE_PATTERN = re.compile(r"^\s*(R,S,T,U|M,N|[A-L]|[O-Q])(?:\s|$)")
HEADER_PATTERN = re.compile(r"2021\s+2022\s+2023\s+2024\*\s+2025\*\*")
TOTAL_TOLERANCE = Decimal("0.05")


@dataclass(frozen=True)
class ExtractedTable:
    rows: dict[str, tuple[Decimal, ...]]
    published_totals: tuple[Decimal, ...]


@dataclass(frozen=True)
class TableCandidate:
    rows: tuple[tuple[str, tuple[Decimal, ...]], ...]
    total_candidates: tuple[tuple[Decimal, ...], ...]
    title_found: bool
    header_found: bool


def extract_page_text(pdf_path: Path, executable: str = "pdftotext") -> str:
    result = subprocess.run(
        [
            executable,
            "-f",
            str(PDF_PAGE),
            "-l",
            str(PDF_PAGE),
            "-layout",
            str(pdf_path),
            "-",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def parse_decimal(value: str) -> Decimal:
    if not VALUE_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid published numeric value: {value}")
    return Decimal(value.replace(",", ""))


def parse_table_candidate(page_text: str) -> TableCandidate:
    rows: list[tuple[str, tuple[Decimal, ...]]] = []
    total_candidates: list[tuple[Decimal, ...]] = []
    for line in page_text.splitlines():
        values = VALUE_PATTERN.findall(line)
        if len(values) != len(YEARS):
            continue

        parsed_values = tuple(parse_decimal(value) for value in values)
        code_match = CODE_PATTERN.match(line)
        if code_match:
            rows.append((code_match.group(1), parsed_values))
        else:
            total_candidates.append(parsed_values)

    return TableCandidate(
        rows=tuple(rows),
        total_candidates=tuple(total_candidates),
        title_found="Lampiran" in page_text and "Harga Berlaku" in page_text,
        header_found=bool(HEADER_PATTERN.search(page_text)),
    )


def validate_table_with_report(
    candidate: TableCandidate,
) -> tuple[ExtractedTable, QualityReport]:
    if not candidate.title_found:
        raise ValueError("Appendix 1 ADHB title was not found on the extracted page.")
    if not candidate.header_found:
        raise ValueError("Expected 2021-2025 columns and status markers were not found.")

    rows: dict[str, tuple[Decimal, ...]] = {}
    for code, values in candidate.rows:
        if code in rows:
            raise ValueError(f"Duplicate industry code in extraction: {code}")
        rows[code] = values

    expected_codes = set(INDUSTRIES)
    if set(rows) != expected_codes:
        missing = sorted(expected_codes - set(rows))
        unexpected = sorted(set(rows) - expected_codes)
        raise ValueError(
            f"Unexpected industry codes; missing={missing}, unexpected={unexpected}"
        )
    if len(candidate.total_candidates) != 1:
        raise ValueError(
            "Expected one published total row, found "
            f"{len(candidate.total_candidates)}."
        )

    table = ExtractedTable(rows=rows, published_totals=candidate.total_candidates[0])
    report = validate_totals(table)
    return table, report


def validate_table(candidate: TableCandidate) -> ExtractedTable:
    table, _ = validate_table_with_report(candidate)
    return table


def parse_table(page_text: str) -> ExtractedTable:
    return validate_table(parse_table_candidate(page_text))


def validate_totals(table: ExtractedTable) -> QualityReport:
    return check_published_totals(
        table.rows,
        table.published_totals,
        YEARS,
        TOTAL_TOLERANCE,
    )


def write_intermediate(table: ExtractedTable, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("row_type", "industry_code", "industry_name", *YEAR_HEADERS))
        for code, name in INDUSTRIES.items():
            writer.writerow(
                ("industry", code, name, *(format(v, ".2f") for v in table.rows[code]))
            )
        writer.writerow(
            (
                "published_total",
                "",
                "Produk Domestik Regional Bruto",
                *(format(v, ".2f") for v in table.published_totals),
            )
        )


def normalize(table: ExtractedTable) -> list[dict[str, str]]:
    observations = []
    for code, name in INDUSTRIES.items():
        for index, year in enumerate(YEARS):
            observations.append(
                {
                    "region_code": "7316",
                    "region_name": "Kabupaten Enrekang",
                    "industry_code": code,
                    "industry_name": name,
                    "year": str(year),
                    "series_code": EXPECTED_SERIES_CODE,
                    "unit": EXPECTED_UNIT,
                    "value": format(table.rows[code][index], ".2f"),
                    "publication_status": YEAR_STATUSES[year],
                }
            )
    return observations


def write_observations(observations: list[dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
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
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(observations)
