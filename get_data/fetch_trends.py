"""
Pull an earlier ACS vintage so the data can answer "what's changing?".

The brief asks three questions - who lives here, what's changing, and where
the answer came from - and a single snapshot can only answer two of them.

We use the 2020 5-year estimates (covering 2016-2020) against the 2024
5-year estimates (2020-2024). Those are the two most recent windows that
share tract boundaries, so a geoid means the same place in both. The 2019
vintage was tried first and rejected: it predates the 2020 boundary
redraw, so its tract codes do not line up.
"""
from __future__ import annotations

ACS_URL_TEMPLATE = "https://api.census.gov/data/{year}/acs/acs5"
FIELDS = [
    "NAME", "B01003_001E", "B19013_001E", "C16001_001E", "C16001_002E",
    "B05001_005E", "B05001_006E", "B25003_001E", "B25003_003E",
    "B08301_001E", "B08301_010E",
]

VINTAGES = [
    # api year, label shown to people, the window it actually covers
    (2020, "ACS 2016-2020", 2016, 2020),
    (2024, "ACS 2020-2024", 2020, 2024),
]


def number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def row_from(raw, vintage_label, period_start, period_end):
    """One tract, one vintage, in the same plain-English shape as tract_data."""
    total_language = number(raw["C16001_001E"])
    english_only = number(raw["C16001_002E"])
    non_english = (
        total_language - english_only
        if total_language is not None and english_only is not None
        else None
    )
    foreign_born = sum(
        number(raw[f]) or 0 for f in ("B05001_005E", "B05001_006E")
    )
    income = number(raw["B19013_001E"])
    return {
        "geoid": f"{raw['state']}{raw['county']}{raw['tract']}",
        "acs_vintage": vintage_label,
        "period_start": period_start,
        "period_end": period_end,
        "population_count": number(raw["B01003_001E"]),
        # Negative values are Census suppression flags, not incomes.
        "median_income": income if income is not None and income > 0 else None,
        "foreign_born_count": foreign_born,
        "language_population_age_5_plus_count": total_language,
        "english_only_at_home_count": english_only,
        "non_english_at_home_count": non_english,
        "occupied_housing_units_count": number(raw["B25003_001E"]),
        "renter_occupied_housing_units_count": number(raw["B25003_003E"]),
        "total_commuter_count": number(raw["B08301_001E"]),
        "public_transit_commuter_count": number(raw["B08301_010E"]),
        "source_url": (
            f"https://data.census.gov/table/ACSDT5Y{period_end}.B01003"
        ),
    }


CREATE_TABLE = """
CREATE TABLE tract_history (
    geoid TEXT NOT NULL,
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
