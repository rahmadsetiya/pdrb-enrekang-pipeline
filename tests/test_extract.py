import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pdrb_pipeline.extract import (
    INDUSTRIES,
    extract_page_text,
    normalize,
    parse_decimal,
    parse_table,
    write_intermediate,
    write_observations,
)
from pdrb_pipeline.pipeline import RAW_PDF


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests/fixtures/appendix_1_page_127.txt"


class ExtractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page_text = FIXTURE.read_text(encoding="utf-8")

    def test_parse_decimal_preserves_published_precision(self):
        self.assertEqual(parse_decimal("3,302.07"), Decimal("3302.07"))
        with self.assertRaisesRegex(ValueError, "Invalid published"):
            parse_decimal("3.302,07")

    def test_target_table_is_extracted_and_reconciled(self):
        table = parse_table(self.page_text)

        self.assertEqual(tuple(table.rows), tuple(INDUSTRIES))
        self.assertEqual(table.rows["A"][0], Decimal("3302.07"))
        self.assertEqual(table.rows["R,S,T,U"][-1], Decimal("75.66"))
        self.assertEqual(table.published_totals[-1], Decimal("11341.40"))

    def test_missing_industry_is_rejected(self):
        text_without_b = "\n".join(
            line
            for line in self.page_text.splitlines()
            if not line.lstrip().startswith("B    Pertambangan")
        )
        with self.assertRaisesRegex(ValueError, r"missing=\['B'\]"):
            parse_table(text_without_b)

    def test_missing_year_status_marker_is_rejected(self):
        invalid_text = self.page_text.replace("2025**", "2025")
        with self.assertRaisesRegex(ValueError, "status markers"):
            parse_table(invalid_text)

    def test_total_mismatch_is_rejected(self):
        invalid_text = self.page_text.replace("8,204.11", "8,999.99")
        with self.assertRaisesRegex(ValueError, "Industry sum for 2021"):
            parse_table(invalid_text)

    def test_normalization_produces_85_sector_observations(self):
        observations = normalize(parse_table(self.page_text))

        self.assertEqual(len(observations), 85)
        self.assertEqual(
            {row["publication_status"] for row in observations if row["year"] == "2024"},
            {"preliminary"},
        )
        self.assertEqual(
            {row["publication_status"] for row in observations if row["year"] == "2025"},
            {"very_preliminary"},
        )
        self.assertNotIn(
            "Produk Domestik Regional Bruto",
            {row["industry_name"] for row in observations},
        )

    def test_derived_files_are_deterministic(self):
        table = parse_table(self.page_text)
        observations = normalize(table)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_wide = root / "first-wide.csv"
            second_wide = root / "second-wide.csv"
            first_long = root / "first-long.csv"
            second_long = root / "second-long.csv"
            write_intermediate(table, first_wide)
            write_intermediate(table, second_wide)
            write_observations(observations, first_long)
            write_observations(observations, second_long)

            self.assertEqual(first_wide.read_bytes(), second_wide.read_bytes())
            self.assertEqual(first_long.read_bytes(), second_long.read_bytes())

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext is unavailable")
    def test_canonical_pdf_reproduces_fixture_structure(self):
        fixture_table = parse_table(self.page_text)
        extracted_table = parse_table(extract_page_text(RAW_PDF))

        self.assertEqual(extracted_table, fixture_table)
        self.assertEqual(len(normalize(extracted_table)), 85)


if __name__ == "__main__":
    unittest.main()
