"""SQLite schema and connection helpers for CivicLens data acquisition."""
from __future__ import annotations

import sqlite3
from .config import DATA_DIRECTORY, DATABASE_PATH

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
    foreign_born_count INTEGER,
    median_income INTEGER,
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
    address TEXT,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    geoid TEXT,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
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
"""


def connect() -> sqlite3.Connection:
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)
