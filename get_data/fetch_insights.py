"""Add the context tables that let answers go past counts of mapped places.

python -m get_data.fetch_insights [--db PATH] [--only NAME ...]

Each table answers a kind of question the first database could not:

  acs_measures        Census ACS 5-year measures for every county tract, every
                      Maryland place (Silver Spring, Bethesda, Rockville...),
                      every Maryland county and the state, 2016-2020 and
                      2020-2024: rent, home value, rent burden, poverty,
                      unemployment, age, degrees, commuting, cars, broadband,
                      limited English, languages, occupation, industry.
  jobs_by_tract       Jobs located in each tract (LEHD LODES WAC), by sector,
                      monthly earnings band and worker age, 2018 and 2023.
                      "People who work here", not residents.
  business_patterns   Establishments by industry (NAICS) per ZIP code,
                      County Business Patterns 2018-2023, plus the county.
  county_*            Montgomery County open data inside the Silver Spring
                      rectangle (commercial permits, crashes and the people
                      in them), and countywide parking, food businesses,
                      rental survey rents and liquor licenses.

Every row keeps its source URL; every table is registered in insight_sources
with its coverage note, so the answer layer can describe it to the model and
cite it. Published estimates are stored as published. A measure that is a sum
of published cells (for example people 65 and over) says so in its label and
has no margin of error, because Census margins do not add.

Costs nothing: public APIs. The Census API key in .env raises the rate limit.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import time

import requests

from .build_database import assign_business_tracts
from .config import CENSUS_API_KEY, DATABASE_PATH
from .fetch_businesses import HEADERS

STATE, COUNTY = "24", "031"
COUNTY_API = "https://data.montgomerycountymd.gov/resource/"
SOUTH, WEST, NORTH, EAST = 38.965, -77.07, 39.035, -76.97
RECTANGLE = f"Silver Spring rectangle ({SOUTH} to {NORTH} N, {WEST} to {EAST} E)"
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

# ----------------------------------------------------------------- Census ACS

VINTAGES = {2024: "ACS 2020-2024", 2020: "ACS 2016-2020"}


def r(prefix, *numbers):
    return [f"{prefix}_{n:03d}E" for n in numbers]


# measure: (label, universe, variables summed). One variable = published cell.
ACS = {
    "population": ("Total population", "people", ["B01003_001E"]),
    "median_age": ("Median age (years)", "people", ["B01002_001E"]),
    "age_under_5": ("Children under 5 (sum of published age cells)", "people", r("B01001", 3, 27)),
    "age_under_18": ("Under 18 (sum of published age cells)", "people", r("B01001", 3, 4, 5, 6, 27, 28, 29, 30)),
    "age_18_34": ("Aged 18-34 (sum of published age cells)", "people", r("B01001", 7, 8, 9, 10, 11, 12, 31, 32, 33, 34, 35, 36)),
    "age_65_plus": ("Aged 65 and over (sum of published age cells)", "people",
                    r("B01001", 20, 21, 22, 23, 24, 25, 44, 45, 46, 47, 48, 49)),
    "households": ("Households", "households", ["B11001_001E"]),
    "households_with_children": ("Households with one or more people under 18", "households", ["B11005_002E"]),
    "median_household_income": ("Median household income (dollars)", "households", ["B19013_001E"]),
    "per_capita_income": ("Per capita income (dollars)", "people", ["B19301_001E"]),
    "households_income_under_35k": ("Households with income under $35,000 (sum of published cells)", "households",
                                    r("B19001", 2, 3, 4, 5, 6, 7)),
    "households_income_150k_plus": ("Households with income $150,000 or more (sum of published cells)", "households",
                                    r("B19001", 16, 17)),
    "poverty_universe": ("People whose poverty status is determined", "people", ["B17001_001E"]),
    "below_poverty": ("People below the poverty level", "people", ["B17001_002E"]),
    "population_16_plus": ("People 16 and over", "people", ["B23025_001E"]),
    "civilian_labor_force": ("Civilian labor force", "people 16+", ["B23025_003E"]),
    "unemployed": ("Unemployed (civilian labor force)", "people 16+", ["B23025_005E"]),
    "population_25_plus": ("People 25 and over", "people", ["B15003_001E"]),
    "bachelors_or_higher": ("People 25+ with a bachelor's degree or higher (sum of published cells)", "people 25+",
                            r("B15003", 22, 23, 24, 25)),
    "housing_units": ("Housing units", "housing units", ["B25002_001E"]),
    "vacant_units": ("Vacant housing units", "housing units", ["B25002_003E"]),
    "occupied_units": ("Occupied housing units", "housing units", ["B25003_001E"]),
    "owner_occupied": ("Owner-occupied housing units", "housing units", ["B25003_002E"]),
    "renter_occupied": ("Renter-occupied housing units", "housing units", ["B25003_003E"]),
    "median_gross_rent": ("Median gross rent (dollars per month)", "renter units paying cash rent", ["B25064_001E"]),
    "median_home_value": ("Median home value (dollars)", "owner-occupied units", ["B25077_001E"]),
    "renters_for_burden": ("Renter households (rent burden universe)", "renter households", ["B25070_001E"]),
    "renters_burdened_30": ("Renter households paying 30% or more of income on rent (sum of published cells)",
                            "renter households", r("B25070", 7, 8, 9, 10)),
    "renters_burdened_50": ("Renter households paying 50% or more of income on rent", "renter households",
                            ["B25070_010E"]),
    "renters_burden_not_computed": ("Renter households whose rent burden is not computed", "renter households",
                                    ["B25070_011E"]),
    "households_for_vehicles": ("Households (vehicles universe)", "households", ["B08201_001E"]),
    "households_no_vehicle": ("Households with no vehicle", "households", ["B08201_002E"]),
    "workers_16_plus": ("Workers 16 and over", "workers", ["B08301_001E"]),
    "workers_public_transit": ("Workers who commute by public transit", "workers", ["B08301_010E"]),
    "workers_walked": ("Workers who walk to work", "workers", ["B08301_019E"]),
    "workers_from_home": ("Workers who work from home", "workers", ["B08301_021E"]),
    "commuters_not_home": ("Workers who do not work from home (travel time universe)", "workers", ["B08303_001E"]),
    "commute_minutes_total": ("Aggregate travel time to work (minutes)", "workers", ["B08013_001E"]),
    "commute_45_plus": ("Workers with a commute of 45 minutes or more (sum of published cells)", "workers",
                        r("B08303", 11, 12, 13)),
    "foreign_born": ("Foreign-born residents", "people", ["B05002_013E"]),
    "households_for_language": ("Households (language universe)", "households", ["C16002_001E"]),
    "limited_english_households": ("Limited English speaking households (sum of published cells)", "households",
                                   r("C16002", 4, 7, 10, 13)),
    "households_for_internet": ("Households (internet universe)", "households", ["B28002_001E"]),
    "households_broadband": ("Households with a broadband subscription", "households", ["B28002_004E"]),
    "households_no_internet": ("Households with no internet access", "households", ["B28002_013E"]),
}
LOCAL_PLACES = ("Silver Spring", "Bethesda", "Wheaton", "Rockville", "Takoma Park", "Gaithersburg", "Germantown",
                "Aspen Hill", "White Oak", "Olney", "Kensington", "Chevy Chase", "Potomac", "Glenmont", "Hillandale",
                "Colesville", "Four Corners", "Fairland", "Burtonsville", "North Bethesda", "Clarksburg",
                "Montgomery Village", "Langley Park", "College Park", "Hyattsville", "Forest Glen", "Calverton")
# Whole published tables, every cell kept with its Census label.
ACS_GROUPS = {
    "C16001": "Language spoken at home (people 5+), by language group",
    "C24010": "Occupation of employed residents 16+, by sex",
    "C24030": "Industry of employed residents 16+, by sex (more detail than C24050)",
}


def census(url, params, tries=4):
    params = dict(params, key=CENSUS_API_KEY) if CENSUS_API_KEY else params
    for attempt in range(tries):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=120)
            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {response.status_code}")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == tries - 1:
                raise
            time.sleep(2 ** (attempt + 1))


def group_variables(year, group):
    meta = census(f"https://api.census.gov/data/{year}/acs/acs5/groups/{group}.json", {})
    out = {}
    for name, info in meta["variables"].items():
        if name.endswith("E") and name.startswith(group + "_"):
            label = info["label"].replace("Estimate!!", "").replace("!!", " > ").rstrip(":").replace(":", "")
            out[name] = label
    return dict(sorted(out.items()))


def acs_rows(year):
    """Rows for every geography in one vintage."""
    vintage = VINTAGES[year]
    url = f"https://api.census.gov/data/{year}/acs/acs5"
    geographies = [
        ("tract", {"for": "tract:*", "in": f"state:{STATE} county:{COUNTY}"}),
        ("place", {"for": "place:*", "in": f"state:{STATE}"}),
        ("county", {"for": "county:*", "in": f"state:{STATE}"}),
        ("zcta", {"for": "zip code tabulation area:" + ",".join(ZIPS)}),
        ("state", {"for": f"state:{STATE}"}),
        ("nation", {"for": "us:1"}),
    ]
    curated = sorted({v for _, _, variables in ACS.values() for v in variables})
    grouped = {group: group_variables(year, group) for group in ACS_GROUPS}
    wanted = curated + [v for vs in grouped.values() for v in vs]
    moe = {v: v[:-1] + "M" for v in wanted}
    rows, labels = [], {}
    for kind, geo in geographies:
        values, names = {}, {}
        all_vars = wanted + [moe[v] for v in wanted]
        for start in range(0, len(all_vars), 45):
            chunk = all_vars[start:start + 45]
            table = census(url, {"get": ",".join(["NAME"] + chunk), **geo})
            header = table[0]
            for record in table[1:]:
                item = dict(zip(header, record))
                geoid = ("".join(item[k] for k in ("state", "county", "tract") if k in item) if kind == "tract"
                         else item["zip code tabulation area"] if kind == "zcta"
                         else item["state"] + item["place"] if kind == "place"
                         else item["state"] + item["county"] if kind == "county"
                         else item.get("state") or "US")
                names[geoid] = item["NAME"]
                values.setdefault(geoid, {}).update({k: item[k] for k in chunk})
        def number(text):
            try:
                value = float(text)
            except (TypeError, ValueError):
                return None
            # Census uses large negative sentinels for "not available".
            return None if value < -1 else value

        for geoid, item in values.items():
            for measure, (label, universe, variables) in ACS.items():
                parts = [number(item.get(v)) for v in variables]
                estimate = None if any(p is None for p in parts) else sum(parts)
                margin = number(item.get(moe[variables[0]])) if len(variables) == 1 else None
                rows.append((geoid, kind, names[geoid], vintage, measure, estimate, margin))
            if kind == "place" and not any(n in names[geoid] for n in LOCAL_PLACES):
                continue          # detailed tables only where people will ask about them
            for group, variables in grouped.items():
                combined = {}
                for variable, label in variables.items():
                    key = label.replace("Male > ", "").replace("Female > ", "")
                    if key in ("Total > Male", "Total > Female"):
                        continue
                    value = number(item.get(variable))
                    entry = combined.setdefault(key, [variable[:-1], 0.0, number(item.get(moe[variable])), 0])
                    entry[3] += 1
                    entry[1] = None if value is None or entry[1] is None else entry[1] + value
                for key, (variable, estimate, margin, cells) in combined.items():
                    measure = f"{group}:{key}"
                    rows.append((geoid, kind, names[geoid], vintage, measure, estimate, margin if cells == 1 else None))
                    labels.setdefault(measure, (f"{ACS_GROUPS[group]}: {key}" + (" (male + female cells summed)" if cells > 1 else ""),
                                                "see table", group, variable))
        print(f"  ACS {year} {kind}: {len(values)} geographies", flush=True)
    return rows, labels


def build_acs(db):
    db.execute("DROP TABLE IF EXISTS acs_measures")
    db.execute("DROP TABLE IF EXISTS acs_measure_labels")
    db.execute("""CREATE TABLE acs_measures (geoid TEXT, geography_type TEXT, geography_name TEXT,
        acs_vintage TEXT, measure TEXT, estimate REAL, margin_of_error REAL)""")
    db.execute("""CREATE TABLE acs_measure_labels (measure TEXT PRIMARY KEY, label TEXT, universe TEXT,
        table_id TEXT, variables TEXT, source_url TEXT)""")
    count, labels = 0, {measure: (label, universe, variables[0].split("_")[0], ",".join(v[:-1] for v in variables))
                        for measure, (label, universe, variables) in ACS.items()}
    for year in VINTAGES:
        rows, group_labels = acs_rows(year)
        labels.update(group_labels)
        db.executemany("INSERT INTO acs_measures VALUES (?,?,?,?,?,?,?)", rows)
        count += len(rows)
    db.executemany("INSERT INTO acs_measure_labels VALUES (?,?,?,?,?,?)",
                   [(m, label, universe, table, variables, f"https://data.census.gov/table/ACSDT5Y2024.{table}")
                    for m, (label, universe, table, variables) in labels.items()])
    # No indexes: a full scan takes milliseconds, and indexes would add ~20 MB
    # to a database file that is committed to git.
    return register(db, "acs_measures", "acs_measures", "Census ACS 5-year measures",
                    "https://www.census.gov/data/developers/data-sets/acs-5year.html",
                    "ACS 2016-2020 and ACS 2020-2024", "Every Montgomery County tract (2020 boundaries, the same IDs as "
                    "tract_data), every Maryland place, every Maryland county, the Silver Spring-area ZIP codes "
                    "(ZCTAs), Maryland, and the United States. Language, occupation and industry detail only for "
                    "tracts, Montgomery County-area places, counties, Maryland and the US.",
                    "Published 5-year estimates with margins of error. Sums of published cells are labelled and have "
                    "no margin. Median values cannot be summed or averaged across areas.", count)


# --------------------------------------------------------------- LEHD LODES

SECTORS = {
    "CNS01": "agriculture", "CNS02": "mining", "CNS03": "utilities", "CNS04": "construction",
    "CNS05": "manufacturing", "CNS06": "wholesale", "CNS07": "retail", "CNS08": "transportation_warehousing",
    "CNS09": "information", "CNS10": "finance_insurance", "CNS11": "real_estate",
    "CNS12": "professional_scientific_technical", "CNS13": "management_of_companies",
    "CNS14": "administrative_support_waste", "CNS15": "educational_services", "CNS16": "health_care_social_assistance",
    "CNS17": "arts_entertainment_recreation", "CNS18": "accommodation_food_services", "CNS19": "other_services",
    "CNS20": "public_administration",
}
LODES_YEARS = (2018, 2023)


def build_jobs(db):
    columns = (["geoid", "geography_type", "year", "total_jobs", "jobs_earning_under_1251_month",
                "jobs_earning_1251_3333_month", "jobs_earning_over_3333_month", "jobs_workers_29_or_younger",
                "jobs_workers_30_to_54", "jobs_workers_55_or_older"]
               + ["jobs_" + name for name in SECTORS.values()] + ["source_url", "retrieved_at"])
    db.execute("DROP TABLE IF EXISTS jobs_by_tract")
    db.execute("CREATE TABLE jobs_by_tract (" + ", ".join(
        f"{c} {'TEXT' if c in ('geoid', 'geography_type', 'source_url', 'retrieved_at') else 'INTEGER'}"
        for c in columns) + ")")
    count = 0
    for year in LODES_YEARS:
        url = f"https://lehd.ces.census.gov/data/lodes/LODES8/md/wac/md_wac_S000_JT00_{year}.csv.gz"
        raw = requests.get(url, headers=HEADERS, timeout=300).content
        totals = {}
        fields = ["C000", "CE01", "CE02", "CE03", "CA01", "CA02", "CA03"] + list(SECTORS)
        for record in csv.DictReader(io.StringIO(gzip.decompress(raw).decode())):
            block = record["w_geocode"]
            if not block.startswith(STATE + COUNTY):
                continue
            for key in (block[:11], STATE + COUNTY):
                bucket = totals.setdefault(key, dict.fromkeys(fields, 0))
                for field in fields:
                    bucket[field] += int(record[field])
        for geoid, bucket in totals.items():
            db.execute(f"INSERT INTO jobs_by_tract VALUES ({','.join('?' * len(columns))})",
                       [geoid, "tract" if len(geoid) == 11 else "county", year]
                       + [bucket[f] for f in fields] + [url, NOW])
            count += 1
        print(f"  LODES {year}: {len(totals) - 1} tracts", flush=True)
    return register(db, "jobs_by_tract", "jobs_by_tract", "Jobs located here (LEHD LODES workplace area characteristics)",
                    "https://lehd.ces.census.gov/data/", f"LODES 8, {LODES_YEARS[0]} and {LODES_YEARS[1]}",
                    "Montgomery County tracts (2020 boundaries, same IDs as tract_data) and the county total",
                    "Counts jobs by WHERE PEOPLE WORK, not where they live. All private and public primary and "
                    "secondary jobs (JT00); excludes self-employed and most federal military. Small tracts may "
                    "include Census noise.", count)


# ---------------------------------------------------- County Business Patterns

ZIPS = ["20901", "20902", "20903", "20904", "20905", "20906", "20910", "20912",
        "20814", "20815", "20816", "20817", "20850", "20851", "20852", "20853", "20895", "20896"]
CBP_YEARS = range(2018, 2024)


def build_business_patterns(db):
    db.execute("DROP TABLE IF EXISTS business_patterns")
    db.execute("""CREATE TABLE business_patterns (
        geography_type TEXT, geography_id TEXT, geography_name TEXT, year INTEGER, naics TEXT, naics_level INTEGER,
        naics_label TEXT, establishments INTEGER, employees INTEGER, annual_payroll_thousands INTEGER,
        source_url TEXT, retrieved_at TEXT)""")
    count = 0
    for year in CBP_YEARS:
        url = f"https://api.census.gov/data/{year}/cbp"
        code = "NAICS2017"
        for kind, geo in (("zip", {"for": "zip code:" + ",".join(ZIPS)}),
                          ("county", {"for": f"county:{COUNTY}", "in": f"state:{STATE}"})):
            table = census(url, {"get": f"NAME,{code},{code}_LABEL,ESTAB,EMP,PAYANN,EMPSZES", **geo})
            header = table[0]
            rows = []
            for record in table[1:]:
                item = dict(zip(header, record))
                if item.get("EMPSZES") != "001":       # all establishment sizes only
                    continue
                naics = item[code]
                digits = len(naics.replace("-", "")) if naics != "00" else 0
                geography_id = item.get("zip code") or STATE + COUNTY
                employees = int(item["EMP"]) if kind == "county" and item["EMP"] not in ("", None) else None
                payroll = int(item["PAYANN"]) if kind == "county" and item["PAYANN"] not in ("", None) else None
                rows.append((kind, geography_id, item["NAME"], year, naics, digits, item[f"{code}_LABEL"],
                             int(item["ESTAB"]), employees, payroll, f"{url}?get=ESTAB&for={kind}", NOW))
            db.executemany("INSERT INTO business_patterns VALUES (" + ",".join("?" * 12) + ")", rows)
            count += len(rows)
        print(f"  CBP {year}: done", flush=True)
    return register(db, "business_patterns", "business_patterns", "County Business Patterns (establishments by industry)",
                    "https://www.census.gov/programs-surveys/cbp.html", f"{min(CBP_YEARS)}-{max(CBP_YEARS)}",
                    "ZIP codes " + ", ".join(ZIPS) + " (Silver Spring, Takoma Park, Bethesda, Rockville, Kensington) "
                    "and Montgomery County",
                    "Establishments with paid employees, counted by ZIP code, which does not match tract or district "
                    "boundaries. ZIP-level employee counts are not published (NULL). Excludes self-employed and "
                    "most government. naics_level 2 is a sector; 6 is the most detailed industry.", count)


# --------------------------------------------------------- County open data

def county_fetch(dataset, where=None, select=None, group=None, page=50000):
    rows, offset = [], 0
    while True:
        params = {"$order": group.split(",")[0] if group else ":id", "$limit": page, "$offset": offset}
        if group:
            params["$group"] = group
        if where:
            params["$where"] = where
        if select:
            params["$select"] = select
        for attempt in range(4):
            try:
                response = requests.get(f"{COUNTY_API}{dataset}.json", params=params, headers=HEADERS, timeout=180)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {response.status_code}")
                response.raise_for_status()
                batch = response.json()
                break
            except (requests.RequestException, ValueError):
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
        rows.extend(batch)
        if len(batch) < page:
            print(f"  {dataset}: {len(rows)} records", flush=True)
            return rows
        offset += page


def num(value):
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def day(value):
    return str(value)[:10] if value else None


def point(row, *keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, dict):
            if "latitude" in value:
                return num(value.get("latitude")), num(value.get("longitude"))
            coords = value.get("coordinates")
            if coords:
                # GeoJSON is (lon, lat); one county dataset stores (lat, lon).
                a, b = num(coords[0]), num(coords[1])
                return (a, b) if a and a > 0 else (b, a)
    lat = num(row.get("latitude") or row.get("centroid_lat"))
    lon = num(row.get("longitude") or row.get("centroid_lng"))
    return lat, lon


def with_tracts(db, rows):
    shapes = {r[0]: {"geometry_geojson": r[1]} for r in db.execute("SELECT geoid, geometry_geojson FROM tract_data")}
    found = {r["_i"]: r["geoid"] for r in assign_business_tracts(
        [dict(row, _i=i) for i, row in enumerate(rows) if row.get("latitude") and row.get("longitude")], shapes)}
    for i, row in enumerate(rows):
        row["geoid"] = found.get(i)
    return rows


def write(db, table, rows, columns):
    db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute(f"CREATE TABLE {table} (" + ", ".join(f"{name} {kind}" for name, kind in columns) + ")")
    names = [name for name, _ in columns]
    db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(names))})", [[r.get(n) for n in names] for r in rows])
    return len(rows)


BOX = f"within_box({{}}, {NORTH}, {WEST}, {SOUTH}, {EAST})"


def build_commercial_permits(db):
    raw = county_fetch("i26v-w6bd", where=BOX.format("location"))
    rows = []
    for r in raw:
        lat, lon = point(r, "location")
        rows.append({"permitno": r.get("permitno"), "issued": day(r.get("issueddate")), "added": day(r.get("addeddate")),
                     "finaled": day(r.get("finaleddate")), "status": r.get("status"),
                     "application_type": r.get("applicationtype"), "worktype": r.get("worktype"),
                     "usecode": r.get("usecode"), "description": (r.get("description") or "")[:300],
                     "building_area_sqft": num(r.get("buildingarea")), "valuation": num(r.get("declaredvaluation")),
                     "address": " ".join(filter(None, (r.get("stno"), r.get("stname"), r.get("suffix")))),
                     "zip": r.get("zip"), "latitude": lat, "longitude": lon,
                     "source_url": "https://data.montgomerycountymd.gov/d/i26v-w6bd"})
    count = write(db, "county_commercial_permits", with_tracts(db, rows), [
        ("permitno", "TEXT"), ("issued", "TEXT"), ("added", "TEXT"), ("finaled", "TEXT"), ("status", "TEXT"),
        ("application_type", "TEXT"), ("worktype", "TEXT"), ("usecode", "TEXT"), ("description", "TEXT"),
        ("building_area_sqft", "REAL"), ("valuation", "REAL"), ("address", "TEXT"), ("zip", "TEXT"),
        ("latitude", "REAL"), ("longitude", "REAL"), ("geoid", "TEXT"), ("source_url", "TEXT")])
    dated = sorted(r["issued"] for r in rows if r["issued"])
    return register(db, "county_commercial_permits", "county_commercial_permits", "Commercial building permits",
                    "https://data.montgomerycountymd.gov/d/i26v-w6bd", "Montgomery County open data", RECTANGLE,
                    "Permits for commercial buildings, including apartment high-rises, offices, retail and "
                    "restaurants. A permit is approval to build, not completed construction. declared valuation is "
                    "the applicant's estimate. Residential (house) permits are in community_permits.",
                    count, dated[0] if dated else None, dated[-1] if dated else None)


def build_crashes(db):
    raw = county_fetch("bhju-22kf", where=BOX.format("geolocation"))
    rows = []
    for r in raw:
        lat, lon = point(r, "location", "geolocation")
        rows.append({"report_number": r.get("report_number"), "crash_date": day(r.get("crash_date_time")),
                     "crash_hour": int(r["crash_date_time"][11:13]) if r.get("crash_date_time") else None,
                     "report_type": r.get("acrs_report_type"), "collision_type": r.get("collision_type"),
                     "road_name": r.get("road_name"), "cross_street": r.get("cross_street_name"),
                     "light": r.get("light"), "weather": r.get("weather"), "surface": r.get("surface_condition"),
                     "hit_run": r.get("hit_run"), "latitude": lat, "longitude": lon,
                     "source_url": "https://data.montgomerycountymd.gov/d/bhju-22kf"})
    count = write(db, "county_crashes", with_tracts(db, rows), [
        ("report_number", "TEXT"), ("crash_date", "TEXT"), ("crash_hour", "INTEGER"), ("report_type", "TEXT"),
        ("collision_type", "TEXT"), ("road_name", "TEXT"), ("cross_street", "TEXT"), ("light", "TEXT"),
        ("weather", "TEXT"), ("surface", "TEXT"), ("hit_run", "TEXT"), ("latitude", "REAL"), ("longitude", "REAL"),
        ("geoid", "TEXT"), ("source_url", "TEXT")])
    dated = sorted(r["crash_date"] for r in rows if r["crash_date"])
    register(db, "county_crashes", "county_crashes", "Traffic crash reports",
             "https://data.montgomerycountymd.gov/d/bhju-22kf", "Montgomery County open data (ACRS)", RECTANGLE,
             "One row per police-reported crash. report_type says Property Damage, Injury or Fatal Crash. People "
             "outside vehicles (pedestrians, cyclists) are in county_crash_nonmotorists, joined on report_number.",
             count, dated[0] if dated else None, dated[-1] if dated else None)
    people = county_fetch("n7fk-dce5", where=BOX.format("geolocation"),
                          select="report_number,pedestrian_type,injury_severity,pedestrian_location,"
                                 "pedestrian_actions,crash_date_time")
    out = [{"report_number": p.get("report_number"), "person_type": p.get("pedestrian_type"),
            "injury_severity": p.get("injury_severity"), "location": p.get("pedestrian_location"),
            "actions": p.get("pedestrian_actions"), "crash_date": day(p.get("crash_date_time")),
            "source_url": "https://data.montgomerycountymd.gov/d/n7fk-dce5"} for p in people]
    count = write(db, "county_crash_nonmotorists", out, [
        ("report_number", "TEXT"), ("person_type", "TEXT"), ("injury_severity", "TEXT"), ("location", "TEXT"),
        ("actions", "TEXT"), ("crash_date", "TEXT"), ("source_url", "TEXT")])
    dated = sorted(r["crash_date"] for r in out if r["crash_date"])
    return register(db, "county_crash_nonmotorists", "county_crash_nonmotorists",
                    "Pedestrians and cyclists in crashes", "https://data.montgomerycountymd.gov/d/n7fk-dce5",
                    "Montgomery County open data (ACRS)", "Crashes in county_crashes",
                    "One row per pedestrian, cyclist or other non-motorist in a reported crash, with injury severity. "
                    "Join to county_crashes on report_number for place and time.",
                    count, dated[0] if dated else None, dated[-1] if dated else None)


def build_parking(db):
    raw = county_fetch("rd7s-ntxu")
    rows = []
    for r in raw:
        lat, lon = point(r)
        rows.append({"facility_name": r.get("facility_name"), "facility_number": r.get("facilitynumber"),
                     "district": r.get("district"), "address": r.get("address_of_facility"),
                     "total_capacity": num(r.get("total_capacity")), "car_spaces": num(r.get("car_spaces")),
                     "ada_spaces": num(r.get("ada_spaces")), "ev_charging_spaces": num(r.get("pcs")),
                     "hours_requiring_payment": r.get("hours_requiring_payment"),
                     "inactive": r.get("inactive"), "latitude": lat, "longitude": lon,
                     "source_url": "https://data.montgomerycountymd.gov/d/rd7s-ntxu"})
    count = write(db, "county_parking_facilities", with_tracts(db, rows), [
        ("facility_name", "TEXT"), ("facility_number", "TEXT"), ("district", "TEXT"), ("address", "TEXT"),
        ("total_capacity", "REAL"), ("car_spaces", "REAL"), ("ada_spaces", "REAL"), ("ev_charging_spaces", "REAL"),
        ("hours_requiring_payment", "TEXT"), ("inactive", "TEXT"), ("latitude", "REAL"), ("longitude", "REAL"),
        ("geoid", "TEXT"), ("source_url", "TEXT")])
    return register(db, "county_parking_facilities", "county_parking_facilities",
                    "Public parking garages and lots (county Parking Lot Districts)",
                    "https://data.montgomerycountymd.gov/d/rd7s-ntxu", "Montgomery County open data",
                    "Countywide public parking facilities; district names include Silver Spring",
                    "County-run public garages and lots only, not private or street parking. Capacity is spaces, "
                    "not how full they are.", count)


STREET_WORDS = {"STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "LANE": "LN", "DRIVE": "DR", "BOULEVARD": "BLVD",
                "HIGHWAY": "HWY", "PLACE": "PL", "COURT": "CT", "TERRACE": "TER", "PARKWAY": "PKWY", "CIRCLE": "CIR"}


def business_key(name, address):
    """The same business across the two inspection datasets, which spell names
    and addresses differently ('The 4 Corners Pub' / '4 Corners Pub',
    '7-Eleven #22921B' / '7-Eleven #22921'): normalized address plus the first
    significant word of the name. A different business at the same address
    keeps its own row."""
    import re
    street = re.sub(r"[.,#]", " ", address.upper())
    street = " ".join(STREET_WORDS.get(word, word) for word in street.split())
    words = [w for w in re.sub(r"#\S*|[^a-z0-9 ]", " ", name.lower()).split() if w not in ("the", "a", "an")]
    return street, (words[0] if words else name.lower())


def food_category(kind):
    """One vocabulary across the county's old and new inspection datasets."""
    text = (kind or "").lower()
    if any(word in text for word in ("school", "nursing", "assisted", "hospital", "child", "day care", "church",
                                     "non-profit", "nonprofit", "correction", "group home", "institution")):
        return "institution"
    if any(word in text for word in ("market", "store", "grocery", "convenience", "retail")):
        return "food store"
    return "food service" if text else None


def build_food(db):
    """One row per licensed food business, dated by its first and latest inspection.

    The county groups the inspection records on its side, so this downloads
    one row per business instead of every inspection line item."""
    keep = ("20901", "20902", "20903", "20904", "20905", "20906", "20910", "20912")
    zips = "(" + ",".join(f"'{z}'" for z in keep) + ")"
    old = county_fetch("5pue-gfbe", where=f"zip in{zips}", select="name,address1,zip,category,location,"
                       "min(inspectiondate) AS first,max(inspectiondate) AS latest,count(*) AS n",
                       group="name,address1,zip,category,location")
    new = county_fetch("dkrp-gr48", where=f"zip in{zips}", select="business_name,address,zip,business_type,location,"
                       "min(inspection_start_date) AS first,max(inspection_start_date) AS latest,"
                       "count(distinct inspection_number) AS n",
                       group="business_name,address,zip,business_type,location")
    places = {}
    for r in old + new:
        name = (r.get("name") or r.get("business_name") or "").strip()
        address = (r.get("address1") or r.get("address") or "").strip()
        if not name:
            continue
        lat, lon = point(r, "location")
        item = places.setdefault(business_key(name, address), {
            "name": name.title(), "address": address.title(), "zip": r.get("zip"),
            "business_type": r.get("category") or r.get("business_type"), "first_inspection": None,
            "latest_inspection": None, "inspections": 0, "latitude": lat, "longitude": lon,
            "source_url": "https://data.montgomerycountymd.gov/d/dkrp-gr48"})
        first, latest = day(r.get("first")), day(r.get("latest"))
        item["inspections"] += int(num(r.get("n")) or 0)
        if first and (not item["first_inspection"] or first < item["first_inspection"]):
            item["first_inspection"] = first
        if latest and (not item["latest_inspection"] or latest > item["latest_inspection"]):
            item["latest_inspection"] = latest
        if not item["latitude"] and lat:
            item["latitude"], item["longitude"] = lat, lon
    rows = list(places.values())
    for row in rows:
        row["category"] = food_category(row.get("business_type"))
    count = write(db, "county_food_businesses", with_tracts(db, rows), [
        ("name", "TEXT"), ("address", "TEXT"), ("zip", "TEXT"), ("category", "TEXT"), ("business_type", "TEXT"),
        ("first_inspection", "TEXT"), ("latest_inspection", "TEXT"), ("inspections", "INTEGER"),
        ("latitude", "REAL"), ("longitude", "REAL"), ("geoid", "TEXT"), ("source_url", "TEXT")])
    dated = sorted(p["first_inspection"] for p in rows if p["first_inspection"])
    return register(db, "county_food_businesses", "county_food_businesses",
                    "Licensed food businesses (from county health inspections)",
                    "https://data.montgomerycountymd.gov/d/dkrp-gr48", "Montgomery County open data",
                    "ZIP codes " + ", ".join(keep),
                    "One row per food business name and address in county inspection records. The published "
                    f"records start {dated[0] if dated else 'unknown'}, so first_inspection on that date or soon "
                    "after only means the place existed then. A later first_inspection is a rough sign a place "
                    "opened or changed hands, not a confirmed opening date. A latest_inspection long ago suggests "
                    "a place may have closed. The county renamed its business types in July 2024 (older records say "
                    "Restaurant, Carry Out, Market; newer say Food Service Facility), so compare years by category "
                    "('food service', 'food store', 'institution'), never by business_type.",
                    count, dated[0] if dated else None, dated[-1] if dated else None)


def build_rents(db):
    raw = county_fetch("65hk-6zwc")
    rows = []
    for r in raw:
        address = r.get("community_address") or ""
        for year in range(2016, 2023):
            rent = num(r.get(f"average_rent_{year}"))
            if rent:
                rows.append({"community_name": r.get("community_name"), "address": address,
                             "zip": address.strip()[-5:] if address.strip()[-5:].isdigit() else None,
                             "bedroom_type": r.get("bedroom_types"), "year": year, "average_rent": rent,
                             "source_url": "https://data.montgomerycountymd.gov/d/65hk-6zwc"})
    count = write(db, "county_rental_survey", rows, [
        ("community_name", "TEXT"), ("address", "TEXT"), ("zip", "TEXT"), ("bedroom_type", "TEXT"),
        ("year", "INTEGER"), ("average_rent", "REAL"), ("source_url", "TEXT")])
    return register(db, "county_rental_survey", "county_rental_survey",
                    "Rental housing survey: average rent per building and unit type",
                    "https://data.montgomerycountymd.gov/d/65hk-6zwc", "County rental facility occupancy survey, "
                    "2016-2022", "Surveyed rental communities countywide; filter by zip",
                    "Average asking rent reported by surveyed apartment communities, by bedroom type, 2016-2022. "
                    "Not every building is surveyed and not every year is reported. No individual units or "
                    "single-family rentals. Average of averages is not a market average.",
                    count, "2016-01-01", "2022-12-31")


def build_liquor(db):
    raw = county_fetch("c6rw-fazn")
    rows = []
    for r in raw:
        lat, lon = point(r, "location")
        rows.append({"licensee_name": r.get("licensee_name"), "street": r.get("street"), "city": r.get("city"),
                     "zip": r.get("zip"), "license_class": r.get("licenseclass"), "license_type": r.get("licensetype"),
                     "latitude": lat, "longitude": lon, "source_url": "https://data.montgomerycountymd.gov/d/c6rw-fazn"})
    count = write(db, "county_liquor_licenses", with_tracts(db, rows), [
        ("licensee_name", "TEXT"), ("street", "TEXT"), ("city", "TEXT"), ("zip", "TEXT"), ("license_class", "TEXT"),
        ("license_type", "TEXT"), ("latitude", "REAL"), ("longitude", "REAL"), ("geoid", "TEXT"), ("source_url", "TEXT")])
    return register(db, "county_liquor_licenses", "county_liquor_licenses", "Alcohol licensees",
                    "https://data.montgomerycountymd.gov/d/c6rw-fazn", "Montgomery County ABS licensee data",
                    "Countywide", "Current alcohol licenses: restaurants, bars, stores. license_type BWL = beer, wine "
                    "and liquor; BW = beer and wine.", count)


# ------------------------------------------------- Montgomery Planning layers

PLANNING = "https://montgomeryplans.org/server/rest/services/Overlays/"


def arcgis(layer, **params):
    query = {"where": "1=1", "outFields": "*", "outSR": 4326, "f": "json", "returnGeometry": "true", **params}
    response = requests.get(PLANNING + layer + "/query", params=query, headers=HEADERS, timeout=180)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["features"]


def inside_rings(lon, lat, rings):
    """Even-odd test over every ring, which handles multipart areas and holes."""
    inside = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def plan_name(raw):
    import re
    name = re.sub(r"\b(?:REVISED|SECTOR PLAN|MASTER PLAN|MINOR MASTER PLAN AMENDMENT|COMM PLAN|CORRIDOR PLAN|"
                  r"PLAN|FOR)\b|\(?\b(?:19|20)\d{2}\b\)?", " ", raw, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -&").title().replace("Cbd", "CBD").replace(" And ", " and ")
    return name


def build_planning_areas(db):
    """Official county master/sector plan areas near Silver Spring, as named places.

    Each plan area maps to the tracts that hold at least 15% of its area,
    estimated by a grid of points over the plan polygon."""
    features = arcgis("Planning_Areas/MapServer/1", geometry=f"{WEST},{SOUTH},{EAST},{NORTH}",
                      geometryType="esriGeometryEnvelope", inSR=4326, spatialRel="esriSpatialRelIntersects")
    tracts = [(r[0], json.loads(r[1])) for r in db.execute("SELECT geoid, geometry_geojson FROM tract_data")]
    centroids = db.execute("SELECT geoid, centroid_longitude, centroid_latitude FROM tract_data").fetchall()
    from .build_database import point_in_geometry
    existing = {r[0].lower() for r in db.execute("SELECT name FROM places WHERE kind != 'planning area'")}
    db.execute("DELETE FROM places WHERE kind = 'planning area'")
    next_id = (db.execute("SELECT MAX(place_id) FROM places").fetchone()[0] or 0) + 1
    added = 0
    for feature in features:
        name = plan_name(feature["attributes"]["NAME"])
        if not name or name.lower() in existing:
            continue
        rings = feature["geometry"]["rings"]
        lons = [p[0] for ring in rings for p in ring]
        lats = [p[1] for ring in rings for p in ring]
        counts, total = {}, 0
        steps = 40
        for i in range(steps):
            for j in range(steps):
                lon = min(lons) + (max(lons) - min(lons)) * (i + 0.5) / steps
                lat = min(lats) + (max(lats) - min(lats)) * (j + 0.5) / steps
                if not inside_rings(lon, lat, rings):
                    continue
                total += 1
                for geoid, geometry in tracts:
                    if point_in_geometry(lon, lat, geometry):
                        counts[geoid] = counts.get(geoid, 0) + 1
                        break
        # A tract belongs if it holds a real share of the plan area, or if its
        # own center lies inside the plan (large plans span many tracts).
        chosen = {g for g, n in counts.items() if total and n / total >= 0.15}
        chosen |= {g for g, lon, lat in centroids if inside_rings(lon, lat, rings)}
        chosen = sorted(chosen) or ([max(counts, key=counts.get)] if counts else [])
        url = feature["attributes"].get("URL") or PLANNING + "Planning_Areas/MapServer/1"
        for geoid in chosen:
            db.execute("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (next_id, name, "planning area", geoid, None, None,
                        f"tract holding {counts.get(geoid, 0) * 100 // max(total, 1)}% of the official plan area"
                        " or centered inside it",
                        "Montgomery Planning master and sector plan boundaries", url, NOW))
            next_id += 1
        existing.add(name.lower())
        added += 1
    print(f"  planning areas: {added}", flush=True)
    return {"places (planning areas)": added}


def build_pipeline(db):
    features = arcgis("Pipeline/MapServer/1")
    rows = []

    def when(ms):
        return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat() if isinstance(ms, (int, float)) else None

    for feature in features:
        a = feature["attributes"]
        rings = (feature.get("geometry") or {}).get("rings") or []
        points = [p for ring in rings for p in ring]
        lon = sum(p[0] for p in points) / len(points) if points else None
        lat = sum(p[1] for p in points) / len(points) if points else None
        rows.append({"project_name": a.get("PROJNAME"), "application_number": a.get("APNO"),
                     "development_type": a.get("DEVELOPMENT"), "approved_units": a.get("APPROVED_UNITS"),
                     "unbuilt_units": a.get("UNBUILT_UNITS"), "single_family_units": a.get("SINGLE_FAMILY"),
                     "multifamily_units": a.get("MULTIFAMILY"), "approved_nonres_sqft": a.get("APPROVED_GFA"),
                     "unbuilt_nonres_sqft": a.get("UNBUILT_GFA"), "office_sqft": a.get("OFFICE_GFA"),
                     "retail_sqft": a.get("RETAIL_GFA"), "office_jobs": a.get("OFFICE_JOBS"),
                     "retail_jobs": a.get("RETAIL_JOBS"), "other_jobs": a.get("OTHER_JOBS"),
                     "master_plan": a.get("MASTERPLAN"), "policy_area": a.get("POLICY_AREA"),
                     "submitted": when(a.get("SUBMITDATE")), "planning_board_date": when(a.get("PBDATE")),
                     "latitude": lat, "longitude": lon,
                     "source_url": "https://montgomeryplanning.org/tools/research/development-pipeline/"})
    count = write(db, "county_development_pipeline", with_tracts(db, rows), [
        ("project_name", "TEXT"), ("application_number", "TEXT"), ("development_type", "TEXT"),
        ("approved_units", "REAL"), ("unbuilt_units", "REAL"), ("single_family_units", "REAL"),
        ("multifamily_units", "REAL"), ("approved_nonres_sqft", "REAL"), ("unbuilt_nonres_sqft", "REAL"),
        ("office_sqft", "REAL"), ("retail_sqft", "REAL"), ("office_jobs", "REAL"), ("retail_jobs", "REAL"),
        ("other_jobs", "REAL"), ("master_plan", "TEXT"), ("policy_area", "TEXT"), ("submitted", "TEXT"),
        ("planning_board_date", "TEXT"), ("latitude", "REAL"), ("longitude", "REAL"), ("geoid", "TEXT"),
        ("source_url", "TEXT")])
    return register(db, "county_development_pipeline", "county_development_pipeline",
                    "Development pipeline (approved, not yet fully built projects)",
                    "https://montgomeryplanning.org/tools/research/development-pipeline/",
                    "Montgomery Planning pipeline layer, retrieved " + NOW[:10], "Countywide; geoid from the "
                    "project's center point", "Projects approved by the Planning Board that are not finished. "
                    "unbuilt_units and unbuilt_nonres_sqft are what remains to be built. Jobs are the planners' "
                    "estimates for the approved space, not current jobs.", count)


# ---------------------------------------------------------------- registry

def register(db, source_id, table, label, url, vintage, geography, note, count, first=None, last=None):
    db.execute("""CREATE TABLE IF NOT EXISTS insight_sources (
        source_id TEXT PRIMARY KEY, table_name TEXT, label TEXT, source_url TEXT, source_repository TEXT,
        source_sha256 TEXT, imported_at TEXT, retrieved_at TEXT, data_vintage TEXT, geography TEXT,
        geography_vintage TEXT, coverage_note TEXT, row_count INTEGER, first_record TEXT, last_record TEXT,
        undated_rows INTEGER)""")
    digest = hashlib.sha256(json.dumps([table, count, first, last]).encode()).hexdigest()
    db.execute("INSERT OR REPLACE INTO insight_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (source_id, table, label, url, url, digest, NOW, NOW, vintage, geography, "2020 tracts",
                note, count, first, last, None))
    return {table: count}


BUILDERS = {
    "acs": build_acs, "jobs": build_jobs, "business_patterns": build_business_patterns,
    "commercial_permits": build_commercial_permits, "crashes": build_crashes, "parking": build_parking,
    "food": build_food, "rents": build_rents, "liquor": build_liquor,
    "planning_areas": build_planning_areas, "pipeline": build_pipeline,
}


def run(db_path=DATABASE_PATH, only=None):
    results = {}
    with closing(sqlite3.connect(db_path)) as db:
        for name, builder in BUILDERS.items():
            if only and name not in only:
                continue
            print(f"{name}...", flush=True)
            with db:
                results.update(builder(db))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DATABASE_PATH)
    parser.add_argument("--only", nargs="*", choices=list(BUILDERS))
    args = parser.parse_args()
    print(json.dumps(run(args.db, args.only), indent=2))


if __name__ == "__main__":
    main()
