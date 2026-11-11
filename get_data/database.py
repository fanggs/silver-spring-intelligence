"""SQLite schema and connection helpers for CivicLens data acquisition."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from .config import DATABASE_PATH
from .fetch_trends import CREATE_TABLE as HISTORY_SCHEMA

SCHEMA = """
CREATE TABLE IF NOT EXISTS tract_data (
    geoid TEXT PRIMARY KEY,
    tract_name TEXT NOT NULL,
    state_fips TEXT NOT NULL,
    county_fips TEXT NOT NULL,
    tract_code TEXT NOT NULL,
    geometry_geojson TEXT,
    land_area_square_meters INTEGER,
    centroid_latitude REAL,
    centroid_longitude REAL,
    population_count INTEGER,
    age_18_34_count INTEGER,
    population_margin_of_error INTEGER,
    female_count INTEGER,
    female_margin_of_error INTEGER,
    male_count INTEGER,
    male_margin_of_error INTEGER,
    foreign_born_count INTEGER,
    median_income INTEGER,
    language_population_age_5_plus_count INTEGER,
    english_only_at_home_count INTEGER,
    non_english_at_home_count INTEGER,
    occupied_housing_units_count INTEGER,
    renter_occupied_housing_units_count INTEGER,
    public_transit_commuter_count INTEGER,
    total_commuter_count INTEGER,
    acs_vintage TEXT,
    source_url TEXT,
    retrieved_at TEXT
);

CREATE TABLE IF NOT EXISTS businesses (
    business_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    category_group TEXT NOT NULL,
    osm_amenity TEXT,
    osm_shop TEXT,
    address TEXT,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    geoid TEXT NOT NULL REFERENCES tract_data(geoid),
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    duplicate_source_url TEXT,
    last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_sources (
    dataset_id TEXT PRIMARY KEY,
    dataset_name TEXT NOT NULL,
    publisher TEXT NOT NULL,
    source_url TEXT NOT NULL,
    geographic_coverage TEXT NOT NULL,
    data_vintage TEXT,
    retrieved_at TEXT NOT NULL,
    coverage_note TEXT
);

CREATE TABLE IF NOT EXISTS places (
    place_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    geoid TEXT NOT NULL REFERENCES tract_data(geoid),
    latitude REAL,
    longitude REAL,
    precision TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    UNIQUE (name, kind, geoid)
);

CREATE TABLE IF NOT EXISTS place_data (
    geoid TEXT PRIMARY KEY,
    place_name TEXT NOT NULL,
    population_count INTEGER NOT NULL,
    population_margin_of_error INTEGER,
    female_count INTEGER NOT NULL,
    female_margin_of_error INTEGER,
    male_count INTEGER NOT NULL,
    male_margin_of_error INTEGER,
    acs_vintage TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS industry_employment (
    geoid TEXT NOT NULL,
    geography_type TEXT NOT NULL CHECK (geography_type IN ('tract', 'place')),
    industry TEXT NOT NULL,
    employed_residents INTEGER,
    margin_of_error INTEGER,
    acs_vintage TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (geography_type, geoid, industry)
);

CREATE INDEX IF NOT EXISTS businesses_geoid_category ON businesses (geoid, category);
""" + HISTORY_SCHEMA + ";"


def connect(path=DATABASE_PATH, *, readonly=False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_database(path=DATABASE_PATH) -> None:
    with closing(connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(tract_data)")}
        if "language_population_age_5_plus_count" not in existing_columns:
            connection.execute(
                "ALTER TABLE tract_data ADD COLUMN language_population_age_5_plus_count INTEGER"
            )
