import argparse
import os

from .load import load_postgres
from .pipeline import INTERMEDIATE, PROCESSED, PROVENANCE, SCHEMA, transform


def parse_args():
    parser = argparse.ArgumentParser(description="Process Kabupaten Enrekang PDRB data.")
    parser.add_argument(
        "command",
        choices=("transform", "load", "run"),
        help="Pipeline stage to execute.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command in {"transform", "run"}:
        count = transform()
        print(f"Wrote {count} observations to {PROCESSED}")
        print(f"Wrote validated wide table to {INTERMEDIATE}")

    if args.command in {"load", "run"}:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise SystemExit("DATABASE_URL is required for PostgreSQL loading.")
        count = load_postgres(database_url, PROCESSED, PROVENANCE, SCHEMA)
        print(f"PostgreSQL contains {count} observations.")


if __name__ == "__main__":
    main()
