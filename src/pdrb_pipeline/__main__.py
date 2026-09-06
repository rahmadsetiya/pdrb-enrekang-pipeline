import argparse
import os

from .load import load_postgres
from .pipeline import (
    INTERMEDIATE,
    PROCESSED,
    PROJECT_ROOT,
    PROVENANCE,
    SCHEMA,
    transform,
)
from .quality import check_loaded_data, check_processed_file


def parse_args():
    parser = argparse.ArgumentParser(description="Process Kabupaten Enrekang PDRB data.")
    parser.add_argument(
        "command",
        choices=("transform", "load", "run", "orchestrate", "status"),
        help="Pipeline stage to execute.",
    )
    parser.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="Status output format (only used by the status command).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "status":
        from .operational import build_operational_report

        report = build_operational_report(PROJECT_ROOT)
        print(report.to_json() if args.format == "json" else report.to_human())
        return report.exit_code

    if args.command == "orchestrate":
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise SystemExit("DATABASE_URL is required for PostgreSQL loading.")
        from .orchestration import pdrb_ingestion_flow

        count = pdrb_ingestion_flow(database_url)
        print(f"Orchestrated flow verified {count} PostgreSQL observations.")
        return 0

    if args.command in {"transform", "run"}:
        count = transform()
        print(f"Wrote {count} observations to {PROCESSED}")
        print(f"Wrote validated wide table to {INTERMEDIATE}")

    if args.command in {"load", "run"}:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise SystemExit("DATABASE_URL is required for PostgreSQL loading.")
        if args.command == "load":
            check_processed_file(PROCESSED)
        count = load_postgres(database_url, PROCESSED, PROVENANCE, SCHEMA)
        check_loaded_data(database_url)
        print(f"PostgreSQL contains {count} observations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
