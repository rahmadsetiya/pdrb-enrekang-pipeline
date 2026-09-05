import json
import tempfile
import unittest
from pathlib import Path

from pdrb_pipeline.pipeline import PROVENANCE, RAW_PDF
from pdrb_pipeline.provenance import load_and_verify


class ProvenanceTests(unittest.TestCase):
    def test_canonical_pdf_matches_recorded_provenance(self):
        provenance = load_and_verify(RAW_PDF, PROVENANCE)
        self.assertEqual(provenance["publication_number"], "73160.26004")
        self.assertEqual(provenance["target_table"]["pdf_page"], 127)

    def test_modified_pdf_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "changed.pdf"
            pdf.write_bytes(RAW_PDF.read_bytes() + b"changed")
            provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
            provenance["file_size_bytes"] = pdf.stat().st_size
            metadata = root / "provenance.json"
            metadata.write_text(json.dumps(provenance), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                load_and_verify(pdf, metadata)


if __name__ == "__main__":
    unittest.main()
