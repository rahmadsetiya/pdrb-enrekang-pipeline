import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (PROJECT_ROOT / "sql/001_initial_schema.sql").read_text(encoding="utf-8")


class SchemaTests(unittest.TestCase):
    def test_schema_has_normalized_tables_and_observation_key(self):
        for table in ("source_datasets", "regions", "industries", "observations"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS pdrb.{table}", SCHEMA)
        self.assertIn(
            "PRIMARY KEY (region_code, industry_code, year, series_code)",
            SCHEMA,
        )
        self.assertIn("value NUMERIC(18, 2)", SCHEMA)


if __name__ == "__main__":
    unittest.main()
