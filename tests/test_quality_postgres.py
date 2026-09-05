import os
import unittest

import psycopg

from pdrb_pipeline.load import load_postgres
from pdrb_pipeline.pipeline import PROCESSED, PROVENANCE, SCHEMA, transform
from pdrb_pipeline.quality import DataQualityError, check_loaded_data


DATABASE_URL = os.environ.get("DATABASE_URL")


@unittest.skipUnless(DATABASE_URL, "DATABASE_URL is unavailable")
class PostgresQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        transform()
        load_postgres(DATABASE_URL, PROCESSED, PROVENANCE, SCHEMA)

    def tearDown(self):
        load_postgres(DATABASE_URL, PROCESSED, PROVENANCE, SCHEMA)

    def test_valid_loaded_observations_pass_every_rule(self):
        report = check_loaded_data(DATABASE_URL)

        self.assertTrue(report.passed)
        self.assertTrue(all("status=PASS" in line for line in report.lines()))

    def test_missing_loaded_observation_identifies_row_count_rule(self):
        with psycopg.connect(DATABASE_URL) as connection:
            connection.execute(
                """
                DELETE FROM pdrb.observations
                WHERE region_code = '7316'
                  AND industry_code = 'A'
                  AND year = 2021
                  AND series_code = 'adhb'
                """
            )

        with self.assertRaises(DataQualityError) as raised:
            check_loaded_data(DATABASE_URL)
        self.assertIn("rule=row_count status=FAIL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
