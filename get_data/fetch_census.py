"""Download Census estimates for county tracts and the official Silver Spring CDP."""
from __future__ import annotations

import requests

from .config import CENSUS_API_KEY

YEAR = 2024
CDP_GEOID = "2472450"
ACS_URL = "https://api.census.gov/data/{year}/acs/acs5"
TRACT_SOURCE_URL = "https://data.census.gov/table/ACSDT5Y2024.B01003"
AGE_FIELDS = [f"B01001_{i:03}E" for i in (*range(7, 13), *range(31, 37))]
SEX_METRICS = {
    "population_margin_of_error": "B01003_001M",
    "female_count": "B01001_026E",
    "female_margin_of_error": "B01001_026M",
    "male_count": "B01001_002E",
    "male_margin_of_error": "B01001_002M",
}
TRACT_METRICS = {
    "population_count": "B01003_001E",
    "median_income": "B19013_001E",
    "language_population_age_5_plus_count": "C16001_001E",
    "english_only_at_home_count": "C16001_002E",
    "occupied_housing_units_count": "B25003_001E",
    "renter_occupied_housing_units_count": "B25003_003E",
    "total_commuter_count": "B08301_001E",
    "public_transit_commuter_count": "B08301_010E",
}
NATIVITY_FIELDS = ("B05001_005E", "B05001_006E")
FIELDS = ["NAME", *TRACT_METRICS.values(), *NATIVITY_FIELDS, *AGE_FIELDS, *SEX_METRICS.values()]
INDUSTRIES = (
    "All industries", "Agriculture, forestry, fishing, hunting and mining", "Construction",
    "Manufacturing", "Wholesale trade", "Retail trade", "Transportation, warehousing and utilities",
    "Information", "Finance, insurance, real estate, rental and leasing",
    "Professional, scientific, management, administrative and waste management services",
    "Educational services, health care and social assistance",
    "Arts, entertainment, recreation, accommodation and food services",
    "Other services, except public administration", "Public administration",
)


def fetch_rows(year, fields, geography="tract"):
    if not CENSUS_API_KEY:
        raise RuntimeError("Set CENSUS_API_KEY in the project .env file.")
    scope = {"for": "tract:*", "in": "state:24 county:031"} if geography == "tract" else {
        "for": "place:72450", "in": "state:24",
    }
    try:
        response = requests.get(ACS_URL.format(year=year), params={
            "get": ",".join(fields), **scope, "key": CENSUS_API_KEY,
        }, timeout=90)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        # Exception URLs can include the Census key. Keep them out of logs.
        raise RuntimeError(f"Census {year} {geography} request failed or returned non-JSON.") from None
    geo_fields = {"state", "county", "tract"} if geography == "tract" else {"state", "place"}
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[0], list):
        raise RuntimeError(f"Census {year} {geography} response contains no rows.")
    header, *rows = payload
    if not set(fields).union(geo_fields).issubset(header) or any(
        not isinstance(row, list) or len(row) != len(header) for row in rows
    ):
        raise RuntimeError(f"Census {year} {geography} response does not match the requested fields.")
    return [dict(zip(header, row)) for row in rows]


def estimate(raw, field):
    """Census negative sentinels are missing estimates, never zero or negative counts."""
    value = raw[field]
    return None if value is None or int(value) < 0 else int(value)


def tract_metrics(raw):
    metrics = {name: estimate(raw, code) for name, code in TRACT_METRICS.items()}
    language = metrics["language_population_age_5_plus_count"]
    english = metrics["english_only_at_home_count"]
    nativity = [estimate(raw, code) for code in NATIVITY_FIELDS]
    return {**metrics,
            "non_english_at_home_count": language - english if None not in (language, english) else None,
            "foreign_born_count": sum(nativity) if None not in nativity else None}


def fetch_census_tracts():
    result = []
    for raw in fetch_rows(YEAR, FIELDS):
        ages = [estimate(raw, code) for code in AGE_FIELDS]
        result.append({
            "geoid": raw["state"] + raw["county"] + raw["tract"],
            "tract_name": raw["NAME"], "state_fips": raw["state"],
            "county_fips": raw["county"], "tract_code": raw["tract"],
            **tract_metrics(raw), **{name: estimate(raw, code) for name, code in SEX_METRICS.items()},
            "age_18_34_count": sum(ages) if None not in ages else None,
        })
    return result


def fetch_place_data():
    rows = fetch_rows(YEAR, ["NAME", "B01003_001E", *SEX_METRICS.values()], "place")
    if len(rows) != 1 or rows[0]["state"] + rows[0]["place"] != CDP_GEOID:
        raise RuntimeError("Expected the official Silver Spring CDP Census record.")
    raw = rows[0]
    return {"geoid": CDP_GEOID, "place_name": raw["NAME"],
            "population_count": estimate(raw, "B01003_001E"),
            **{name: estimate(raw, code) for name, code in SEX_METRICS.items()}}


def fetch_industry_employment():
    fields = [f"C24050_{i:03}{suffix}" for i in range(1, len(INDUSTRIES) + 1) for suffix in "EM"]
    result = []
    for geography in ("tract", "place"):
        for raw in fetch_rows(YEAR, fields, geography):
            geoid = (raw["state"] + raw["county"] + raw["tract"] if geography == "tract"
                     else raw["state"] + raw["place"])
            for i, industry in enumerate(INDUSTRIES, 1):
                result.append({
                    "geoid": geoid, "geography_type": geography, "industry": industry,
                    "employed_residents": estimate(raw, f"C24050_{i:03}E"),
                    "margin_of_error": estimate(raw, f"C24050_{i:03}M"),
                    "source_url": "https://data.census.gov/table/ACSDT5Y2024.C24050?g="
                    + ("1400000US" if geography == "tract" else "160XX00US") + geoid,
                })
    return result
