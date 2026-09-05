CREATE SCHEMA IF NOT EXISTS pdrb;

CREATE TABLE IF NOT EXISTS pdrb.source_datasets (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publisher TEXT NOT NULL,
    publication_title TEXT NOT NULL,
    publication_number TEXT NOT NULL UNIQUE,
    publication_url TEXT NOT NULL,
    release_date DATE NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    raw_path TEXT NOT NULL,
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS pdrb.regions (
    bps_code VARCHAR(4) PRIMARY KEY,
    name TEXT NOT NULL,
    province_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pdrb.industries (
    code VARCHAR(7) PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pdrb.observations (
    source_dataset_id BIGINT NOT NULL REFERENCES pdrb.source_datasets(id),
    region_code VARCHAR(4) NOT NULL REFERENCES pdrb.regions(bps_code),
    industry_code VARCHAR(7) NOT NULL REFERENCES pdrb.industries(code),
    year SMALLINT NOT NULL CHECK (year BETWEEN 1900 AND 2100),
    series_code TEXT NOT NULL CHECK (series_code = 'adhb'),
    unit TEXT NOT NULL CHECK (unit = 'billion_idr'),
    value NUMERIC(18, 2) NOT NULL CHECK (value >= 0),
    publication_status TEXT NOT NULL
        CHECK (publication_status IN ('final', 'preliminary', 'very_preliminary')),
    PRIMARY KEY (region_code, industry_code, year, series_code)
);
