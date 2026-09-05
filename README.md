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
