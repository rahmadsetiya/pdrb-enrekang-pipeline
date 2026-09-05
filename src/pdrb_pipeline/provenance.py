import hashlib
import json
from pathlib import Path


def load_and_verify(pdf_path: Path, provenance_path: Path) -> dict:
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    actual_size = pdf_path.stat().st_size
    if actual_size != provenance["file_size_bytes"]:
        raise ValueError(
            f"Raw PDF size mismatch: expected {provenance['file_size_bytes']}, "
            f"found {actual_size}."
        )

    digest = hashlib.sha256()
    with pdf_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_checksum = digest.hexdigest()
    if actual_checksum != provenance["sha256"]:
        raise ValueError(
            f"Raw PDF checksum mismatch: expected {provenance['sha256']}, "
            f"found {actual_checksum}."
        )
    return provenance
