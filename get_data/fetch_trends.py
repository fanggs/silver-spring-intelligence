"""Build comparable 2016–2020 and 2020–2024 tract history."""
from .fetch_census import NATIVITY_FIELDS, TRACT_METRICS, fetch_rows, tract_metrics


def fetch_earlier_tracts():
    return [
        {"geoid": raw["state"] + raw["county"] + raw["tract"], **tract_metrics(raw)}
        for raw in fetch_rows(2020, [*TRACT_METRICS.values(), *NATIVITY_FIELDS])
    ]


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS tract_history (
    geoid TEXT NOT NULL REFERENCES tract_data(geoid),
    acs_vintage TEXT NOT NULL,
    period_start INTEGER,
    period_end INTEGER,
    population_count INTEGER,
    median_income INTEGER,
    foreign_born_count INTEGER,
    language_population_age_5_plus_count INTEGER,
    english_only_at_home_count INTEGER,
    non_english_at_home_count INTEGER,
    occupied_housing_units_count INTEGER,
    renter_occupied_housing_units_count INTEGER,
    total_commuter_count INTEGER,
    public_transit_commuter_count INTEGER,
    source_url TEXT,
    retrieved_at TEXT,
    PRIMARY KEY (geoid, acs_vintage)
)
"""
