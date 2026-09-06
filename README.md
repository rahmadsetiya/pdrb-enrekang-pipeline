# PDRB Enrekang Pipeline

A deliberately small data engineering pipeline that extracts one table from an
official BPS Kabupaten Enrekang publication, validates and normalizes it, and
loads it into PostgreSQL.

## First vertical slice

The canonical source is the BPS publication *Produk Domestik Regional Bruto
Kabupaten Enrekang Menurut Lapangan Usaha 2021-2025*, publication number
`73160.26004`. The raw PDF is committed unchanged at
`data/raw/bps/pdrb_enrekang_by_industry_2021_2025.pdf`; its provenance and
SHA-256 checksum are recorded in the adjacent JSON file.

This slice processes only Appendix 1 on PDF page 127 (printed page 107): PDRB
at current prices (`ADHB`) by 17 industries for 2021-2025. The PDF has a usable
text layer, so extraction uses Poppler's `pdftotext -layout`; OCR is neither
needed nor included.

The data layers are intentionally distinct:

- `data/raw/bps/`: immutable canonical PDF and provenance metadata
- `data/intermediate/`: generated wide table matching the publication layout
- `data/processed/`: generated long-form observations ready for loading

The website statistical table is not an ingestion source. It may only be used
as an independent reference when checking published values.

## Architecture

```text
official BPS PDF (raw, checksummed)
  -> page 127 text extraction
  -> structural and published-total validation
  -> wide intermediate CSV
  -> 85 normalized observations
  -> PostgreSQL
```

The published total row validates each year's 17 industry values within a
`0.05` billion rupiah rounding tolerance. It is not loaded as an industry.

PostgreSQL uses four normalized tables in the `pdrb` schema:

- `source_datasets`: publication provenance and raw-file checksum
- `regions`: BPS region code and name
- `industries`: BPS industry category code and name
- `observations`: one ADHB value per region, industry, and year

## Setup

Copy the local development environment template and choose a local password:

```bash
cp .env.example .env
```

The Compose network is internal; PostgreSQL does not publish a host port.
Validate and start PostgreSQL:

```bash
docker compose config
docker compose up -d db
docker compose ps
```

## Run the pipeline

Run the complete raw-to-PostgreSQL path:

```bash
docker compose run --rm pipeline python -m pdrb_pipeline run
```

The command verifies the raw PDF checksum before extraction, validates the
table before normalization, writes the two generated CSVs, and loads exactly
85 observations in one transaction. Re-running it updates the same natural
keys rather than creating duplicates.

Run only the file transformation or reload an existing processed file:

```bash
docker compose run --rm pipeline python -m pdrb_pipeline transform
docker compose run --rm pipeline python -m pdrb_pipeline load
```

## Orchestrated flow

Run the same pipeline as a local Prefect flow:

```bash
docker compose run --rm pipeline python -m pdrb_pipeline orchestrate
```

The flow runs synchronously with seven explicit stages:

```text
verify-source
  -> extract-table
  -> validate-table
  -> normalize-observations
  -> check-processed-quality
  -> load-postgres
  -> check-loaded-quality
```

Each stage has its own Prefect task state and safe operational logs. The logs
include stage names, source identity, row counts, and output paths, but never
the database URL or credentials. The flow runs locally and does not require a
Prefect server, worker, hosted account, or scheduler.

The Compose environment explicitly selects ephemeral mode and disables Prefect
analytics and resource telemetry for this local-only flow. Prefect still
records task states and logs in the temporary local backend used for each
invocation.

Only the two PostgreSQL tasks retry: they make at most two additional attempts
after transient `psycopg.OperationalError` failures, waiting 2 then 5 seconds.
Source, extraction, validation, processed quality, and non-operational database
errors fail immediately because retrying deterministic work would hide the
actual fault.

The original `transform`, `load`, and `run` commands remain available for
direct execution and produce the same intermediate and processed files.

## Data quality

The quality contract is implemented once in the framework-independent domain
layer. It checks the generated processed CSV and the loaded Enrekang slice for:

- `row_count`: exactly 85 observations
- `industry_count`: exactly 17 industries
- `year_range`: all five years from 2021 through 2025
- `duplicate_keys`: no duplicate region/industry/year/series keys
- `required_not_null`: no missing required values
- `series_code`: only `adhb`
- `unit`: only `billion_idr`
- `publication_status`: the expected status for each year
- `positive_values`: finite values strictly greater than zero

The earlier `published_total_reconciliation` rule remains part of table
validation before normalization. It compares each year's 17-industry sum with
the publication total using the existing `0.05` billion rupiah tolerance.

The orchestrated command emits one deterministic line per check, for example:

```text
quality scope=processed rule=row_count status=PASS observed=85 expected=85
```

A critical failure raises `DataQualityError`; the message includes the scope,
stable rule ID, observed result, and expectation. Quality failures are not
retried. Logs do not include database credentials, URLs, or row payloads.

## Query the result

```bash
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT o.year, i.code, i.name, o.value, o.unit, o.publication_status
FROM pdrb.observations AS o
JOIN pdrb.industries AS i ON i.code = o.industry_code
WHERE o.region_code = '7316' AND o.series_code = 'adhb'
ORDER BY o.year, i.code;
"
```

Confirm the expected row count:

```bash
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT COUNT(*) FROM pdrb.observations;"
```

## Operational status

Run the read-only status command from the repository root in the local Python
environment:

```bash
python -m pdrb_pipeline status
python -m pdrb_pipeline status --format json
```

The command inspects the Git working tree, Docker Compose status where access
is available, PostgreSQL connectivity and loaded-data quality, optional backup
metadata, and project-filesystem usage. It never transforms or loads data,
initializes schema, changes Git state, starts or restarts containers, creates or
extracts backups, applies retention, or remediates failures.

Checks use `PASS`, `FAIL`, or `BLOCKED`. Human output is concise and includes a
likely cause and next diagnostic action for non-passing checks. JSON uses a
stable schema and check order, contains no generated timestamp, and is suitable
for a future operations agent:

```json
{"checks":[],"counts":{"BLOCKED":0,"FAIL":0,"PASS":0},"overall_status":"PASS","schema_version":1}
```

The real report includes the check objects. Each object has `check_id`,
`status`, `required`, `summary`, `observed`, `cause`, and `action`. Exit codes
are `0` for required checks passing, `1` for a required failure, and `2` when no
required check fails but at least one is blocked.

PostgreSQL reporting reuses the existing quality contract rather than defining
new row-count or year rules. A healthy slice reports 85 rows, 17 industries,
and years 2021-2025. The command uses a bounded connection attempt and never
prints `DATABASE_URL` or credentials.

Docker inspection is optional because the status command may run where the
daemon is inaccessible. Permission or daemon errors are reported as `BLOCKED`,
not as a crash, and no Docker socket is mounted into the pipeline container.
Filesystem usage is reported in bytes and percent; usage at or above 90 percent
is a `FAIL` that recommends inspection but never deletes files.

This repository does not currently define a backup workflow or default backup
directory. Consequently, backup status is optional and reports
`observed=NOT_CONFIGURED` with `status=BLOCKED` unless `PDRB_BACKUP_DIR` is set:

```bash
PDRB_BACKUP_DIR=/absolute/external/path python -m pdrb_pipeline status
```

The configured directory must be outside the repository. Status only reads
metadata for files matching
`pdrb-enrekang-pipeline-YYYYmmddTHHMMSSZ.tar.gz`; it does not infer unrelated
sibling conventions or inspect archive contents.

For diagnostics, run only the command named in the report's `action` field.
The status command intentionally performs no restart, reload, cleanup, or
self-healing action.

## Tests

Run the tests in the reproducible pipeline image:

```bash
docker compose run --rm pipeline python -m unittest discover -s tests -v
```

For a local Python 3.12 environment, install Poppler and the package first:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
python -m unittest discover -s tests -v
```

Run commands from the repository root. For another working directory, set
`PDRB_PROJECT_ROOT` to the absolute repository path.

## Reset local PostgreSQL

This removes the local database volume but does not touch the raw PDF:

```bash
docker compose down --volumes
```
