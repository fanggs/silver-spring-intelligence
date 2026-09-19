"""Download raw 2024 ACS 5-year estimates for every Montgomery County tract."""
from __future__ import annotations
import json
import requests
from .config import CENSUS_API_KEY

ACS_URL = "https://api.census.gov/data/2024/acs/acs5"
FIELDS = ["NAME", "B01003_001E", "B05001_006E", "B19013_001E", "B16001_002E", "B16001_003E",
          "B25003_001E", "B25003_003E", "B08301_001E", "B08301_010E",
          "B01001_007E", "B01001_008E", "B01001_009E", "B01001_010E", "B01001_011E", "B01001_012E",
          "B01001_031E", "B01001_032E", "B01001_033E", "B01001_034E", "B01001_035E", "B01001_036E"]

def number(value):
    try: return int(value)
    except (TypeError, ValueError): return None

def fetch_census_tracts():
    if not CENSUS_API_KEY: raise RuntimeError("Set CENSUS_API_KEY in the project .env file.")
    response = requests.get(ACS_URL, params={"get": ",".join(FIELDS), "for": "tract:*", "in": "state:24 county:031", "key": CENSUS_API_KEY}, timeout=60)
    response.raise_for_status()
    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        preview = response.text[:500].replace("\n", " ")
        raise RuntimeError(
            f"Census API returned non-JSON content (HTTP {response.status_code}): {preview}"
        ) from error
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"Census API returned an unexpected response: {payload}")
    header, *rows = payload
    result = []
    for raw in map(lambda row: dict(zip(header, row)), rows):
        age_18_34 = sum(number(raw[field]) or 0 for field in FIELDS[10:])
        result.append({"geoid": f"{raw['state']}{raw['county']}{raw['tract']}", "tract_name": raw["NAME"], "state_fips": raw["state"], "county_fips": raw["county"], "tract_code": raw["tract"], "population_count": number(raw["B01003_001E"]), "age_18_34_count": age_18_34, "foreign_born_count": number(raw["B05001_006E"]), "median_income": number(raw["B19013_001E"]), "english_only_at_home_count": number(raw["B16001_002E"]), "non_english_at_home_count": number(raw["B16001_003E"]), "occupied_housing_units_count": number(raw["B25003_001E"]), "renter_occupied_housing_units_count": number(raw["B25003_003E"]), "total_commuter_count": number(raw["B08301_001E"]), "public_transit_commuter_count": number(raw["B08301_010E"])})
    return result
