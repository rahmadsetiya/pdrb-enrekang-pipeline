import copy
import unittest
from pathlib import Path

from pdrb_pipeline.extract import normalize, parse_table
from pdrb_pipeline.quality import (
    DataQualityError,
    collect_observation_metrics,
    evaluate_observation_metrics,
    require_quality,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests/fixtures/appendix_1_page_127.txt"


class QualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.valid_rows = normalize(parse_table(FIXTURE.read_text(encoding="utf-8")))

    def report_for(self, rows):
        metrics = collect_observation_metrics(rows)
        return evaluate_observation_metrics("processed", metrics)

    def test_valid_observations_pass_every_rule(self):
        report = require_quality(self.report_for(self.valid_rows))

        self.assertTrue(report.passed)
        self.assertEqual(
            [result.rule_id for result in report.results],
            [
                "row_count",
                "industry_count",
                "year_range",
                "duplicate_keys",
                "required_not_null",
                "series_code",
                "unit",
                "publication_status",
                "positive_values",
            ],
        )
        self.assertTrue(all("status=PASS" in line for line in report.lines()))

    def test_controlled_bad_data_identifies_each_failed_rule(self):
        cases = {
            "row_count": lambda rows: rows.pop(),
            "industry_count": lambda rows: rows.__setitem__(
                slice(None),
                [row for row in rows if row["industry_code"] != "A"],
            ),
            "year_range": lambda rows: rows[0].__setitem__("year", "2030"),
            "duplicate_keys": lambda rows: rows.append(copy.deepcopy(rows[0])),
            "required_not_null": lambda rows: rows[0].__setitem__("region_name", ""),
            "series_code": lambda rows: rows[0].__setitem__("series_code", "adhk"),
            "unit": lambda rows: rows[0].__setitem__("unit", "million_idr"),
            "publication_status": lambda rows: rows[0].__setitem__(
                "publication_status", "preliminary"
            ),
            "positive_values": lambda rows: rows[0].__setitem__("value", "0.00"),
        }

        for rule_id, mutate in cases.items():
            with self.subTest(rule_id=rule_id):
                rows = copy.deepcopy(self.valid_rows)
                mutate(rows)
                with self.assertRaises(DataQualityError) as raised:
                    require_quality(self.report_for(rows))
                self.assertIn(f"rule={rule_id} status=FAIL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
