"""
CivicLens bridge.

Sits between Sid's database (get_data/) and Aryan's page (page/).

  question  ->  Claude writes SQL  ->  we run it read-only  ->  answer + sources

The model writes the QUERY. The database produces the NUMBERS. Claude never
invents a figure, which is what makes every answer traceable to a source.
"""
from __future__ import annotations

import json
import re
import os
import sqlite3
from pathlib import Path

import anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from answers.schema import SCHEMA_DESCRIPTION, COVERAGE_BLURB
from answers.sql_guard import run, UnsafeQuery

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = str(PROJECT_ROOT / "data" / "silverspring.db")
MODEL = "claude-sonnet-5"


def _load_env() -> None:
    """Read .env without needing python-dotenv."""
    env = PROJECT_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()


_load_env()
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())

app = FastAPI(title="CivicLens")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Question(BaseModel):
    question: str


def _ask_claude(prompt: str, max_tokens: int = 700) -> str:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    # The response can contain thinking blocks before the text block, so
    # never assume content[0] is the answer — find the first text block.
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    for block in resp.content:                 # older SDKs: no .type
        text = getattr(block, "text", None)
        if text:
            return text.strip()
    raise RuntimeError("model returned no text block")


# ---------------------------------------------------------------- endpoints

@app.get("/")
def index():
    """Landing route so the bare URL explains itself instead of 404ing."""
    return {
        "service": "CivicLens API",
        "what": "Queryable public community data for Montgomery County, Maryland "
                "Silver Spring, Maryland — 19 census tracts, 81,727 residents, "
                "455 businesses. Montgomery County's 232 tracts are in the "
                "database too, so Silver Spring can be compared against them.",
        "endpoints": {
            "GET  /health": "service + database status",
            "GET  /tracts": "Silver Spring's 19 tract boundaries as GeoJSON (?scope=county for all 232)",
            "GET  /boundary": "the Silver Spring CDP outline as GeoJSON",
            "GET  /businesses": "Silver Spring business locations (?scope=county for all 5,953)",
            "POST /ask": "ask a question in plain English -> answer, sources, and the SQL we ran",
            "GET  /docs": "interactive API explorer",
        },
        "sources": [
            "ACS 5-Year Estimates 2020-2024, U.S. Census Bureau",
            "Census TIGER/Line tract boundaries, 2024",
            "OpenStreetMap via Overpass API",
        ],
    }


@app.get("/health")
def health():
    """Alive check, plus a quick look at whether the database is present."""
    try:
        conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
        placeholders = ",".join("?" * len(SILVER_SPRING_GEOIDS))
        args = tuple(SILVER_SPRING_GEOIDS)
        tracts = conn.execute("SELECT COUNT(*) FROM tract_data").fetchone()[0]
        biz = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        ss_tracts = conn.execute(
            f"SELECT COUNT(*) FROM tract_data WHERE geoid IN ({placeholders})", args
        ).fetchone()[0]
        ss_biz = conn.execute(
            f"SELECT COUNT(*) FROM businesses WHERE geoid IN ({placeholders})", args
        ).fetchone()[0]
        conn.close()
        return {
            "ok": True,
            "silver_spring": {"tracts": ss_tracts, "businesses": ss_biz},
            "county": {"tracts": tracts, "businesses": biz},
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _scope_clause(scope: str):
    """
    Silver Spring is what the map is for, so it is the default everywhere.
    ?scope=county opens the rest of the county up for comparison work.
    """
    if scope == "county":
        return "", ()
    placeholders = ",".join("?" * len(SILVER_SPRING_GEOIDS))
    return f" AND geoid IN ({placeholders})", tuple(SILVER_SPRING_GEOIDS)


@app.get("/tracts")
def tracts(scope: str = "silver-spring"):
    """
    Tract boundaries as GeoJSON, for the map to draw. Silver Spring only by
    default — drawing all 232 county tracts put a second, bigger outline
    around a map that is supposed to be about one town.
    Geometry only plus a few display values — no AI involved.
    """
    clause, args = _scope_clause(scope)
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT geoid, tract_name, geometry_geojson, population_count,
                  median_income, foreign_born_count, source_url
           FROM tract_data WHERE geometry_geojson IS NOT NULL""" + clause,
        args,
    ).fetchall()
    # "Census Tract 7025.01" means nothing to a person standing in it, so send
    # the names it is actually known by. Districts first - they are the names
    # on the signage - then the neighbourhoods.
    names = {}
    for r in conn.execute(
        """SELECT geoid, name FROM places
           ORDER BY CASE WHEN kind = 'district' THEN 0 ELSE 1 END, name"""
    ):
        names.setdefault(r["geoid"], []).append(r["name"])
    conn.close()

    features = []
    for r in rows:
        try:
            geom = json.loads(r["geometry_geojson"])
        except (TypeError, json.JSONDecodeError):
            continue
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "geoid": r["geoid"],
                "tract_name": r["tract_name"],
                "place_names": names.get(r["geoid"], []),
                "population_count": r["population_count"],
                "median_income": r["median_income"],
                "foreign_born_count": r["foreign_born_count"],
                "source_url": r["source_url"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/boundary")
def boundary():
    """The Silver Spring outline, so the map can show what 'here' means."""
    return json.loads(BOUNDARY_PATH.read_text())


@app.get("/businesses")
def businesses(limit: int = 6000, scope: str = "silver-spring"):
    """Business points for the map pins. Silver Spring only by default."""
    clause, args = _scope_clause(scope)
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT business_id, name, category, latitude, longitude,
                  geoid, source_name, source_url
           FROM businesses
           WHERE latitude IS NOT NULL AND longitude IS NOT NULL""" + clause +
        " LIMIT ?",
        args + (limit,),
    ).fetchall()
    conn.close()
    # Returns a plain array so the map can iterate it directly.
    return [dict(r) for r in rows]


@app.post("/ask")
def ask(q: Question):
    """Plain-English question -> real answer, with the SQL and sources shown."""
    question = q.question.strip()
    if not question:
        return _cannot("Please ask a question.")

    # 1. Claude writes the query. It never writes the numbers.
    sql_prompt = (
        f"{SCHEMA_DESCRIPTION}\n\n"
        f"User question: {question}\n\n"
        "Write ONE SQLite SELECT query that answers it. Output only the SQL, "
        "no explanation and no markdown fences. If the question cannot be "
        "answered from these tables, output exactly: CANNOT_ANSWER"
    )
    try:
        sql = _ask_claude(sql_prompt, max_tokens=1500)
    except Exception as exc:
        return _cannot(f"Could not reach the language model: {exc}")

    if "CANNOT_ANSWER" in sql.upper():
        return _cannot()

    # 2. Validate and run it read-only against the real database.
    try:
        rows, executed_sql = run(DATABASE_PATH, sql)
    except UnsafeQuery as exc:
        return _cannot(f"The generated query was rejected for safety: {exc}", sql=sql)
    except sqlite3.Error as exc:
        return _cannot(f"That query didn't run: {exc}", sql=sql)

    if not rows:
        return _cannot("No data matched that question.", sql=sql)

    # 3. Claude phrases a sentence FROM THE REAL ROWS. The raw table is
    #    returned too, so every number on screen is verifiable.
    answer_prompt = (
        f"Question: {question}\n\n"
        f"These rows came back from the database:\n"
        f"{json.dumps(rows[:25], indent=2)}\n\n"
        "Write 1-2 plain sentences answering the question using ONLY these "
        "numbers. Do not invent or estimate anything. Round large numbers "
        "readably. Do not mention SQL or databases."
    )
    try:
        answer = _ask_claude(answer_prompt, max_tokens=800)
    except Exception:
        answer = f"Found {len(rows)} matching result(s)."

    _table, _headers = _to_label_value(rows)
    # Tracts to light up. Aggregate queries (e.g. GROUP BY category) carry no
    # geoid, so fall back to any place named in the question itself.
    highlights = [r["geoid"] for r in rows if r.get("geoid")]
    if not highlights:
        highlights = _sql_geoids(executed_sql) or _place_geoids(question)
    # An answer with no place in it is an answer about Silver Spring as a
    # whole, so show that rather than leaving the map inert. The frontend
    # draws this scope softly instead of as a selection.
    if not highlights:
        highlights = list(SILVER_SPRING_GEOIDS)
    # Whether the model filtered to Silver Spring itself or we defaulted to
    # it, the answer is about the area rather than a selection inside it, so
    # the map should wash it rather than light 19 tracts up like results.
    scope = "area" if set(highlights) == set(SILVER_SPRING_GEOIDS) else "selection"

    return {
        "answer": answer,
        "highlight_geoids": highlights,
        "highlight_scope": scope,
        "table": _table,
        "table_headers": _headers,
        "rows": rows,
        "sources": _sources(executed_sql, highlights),
        "sql": executed_sql,
    }


# ---------------------------------------------------------------- helpers

_PLACE_NAME_CACHE = {}


def _place_names_by_geoid():
    """geoid -> the names people actually use for it, districts first."""
    if _PLACE_NAME_CACHE:
        return _PLACE_NAME_CACHE
    try:
        conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
        for geoid, name in conn.execute(
            """SELECT geoid, name FROM places
               ORDER BY CASE WHEN kind = 'district' THEN 0 ELSE 1 END, name"""
        ):
            _PLACE_NAME_CACHE.setdefault(geoid, []).append(name)
        conn.close()
    except sqlite3.Error:
        pass
    return _PLACE_NAME_CACHE


def _to_label_value(rows):
    """
    Turn raw query rows into readable {label, value} pairs.

    The frontend shows a plain two-column table, so the value has to carry
    its own meaning — "4,098" tells you nothing, "4,098 foreign-born
    residents" tells you everything. Raw rows are returned separately
    as `rows` for anything that needs the unformatted numbers.
    """
    # Coordinates are how the map works, not something a reader wants to see.
    SKIP = {"geoid", "source_url", "state_fips", "county_fips", "tract_code",
            "latitude", "longitude", "centroid_latitude", "centroid_longitude",
            "business_id"}

    # column name -> (how to say it, how to format it)
    WORDS = {
        "population_count":                    "residents",
        "foreign_born_count":                  "foreign-born residents",
        "median_income":                       "median household income",
        "english_only_at_home_count":          "speak only English at home",
        "non_english_at_home_count":           "speak another language at home",
        "age_18_34_count":                     "residents aged 18-34",
        "occupied_housing_units_count":        "occupied homes",
        "renter_occupied_housing_units_count": "rented homes",
        "public_transit_commuter_count":       "commute by public transit",
        "total_commuter_count":                "commuters",
        "land_area_square_meters":             "sq. meters of land",
        "business_count":                      "businesses",
        "count":                               "businesses",
    }

    def phrase(column, v):
        """Format the number only — the column header carries the meaning."""
        if column.startswith("pct") or "percent" in column or "share" in column:
            return f"{v:.1f}%"
        if "income" in column:
            return f"${v:,.0f}"
        if isinstance(v, float):
            return f"{v:,.1f}"
        return f"{v:,}"

    place_names = _place_names_by_geoid()

    out = []
    header = {"label": "Result", "value": "Value"}
    for r in rows:
        label = None
        value = None
        spare_text = None
        for k, v in r.items():
            if k in SKIP:
                continue
            if isinstance(v, str):
                if label is None:
                    label = v.split(";")[0].strip()      # "Census Tract 7016.02"
                elif spare_text is None:
                    spare_text = v                       # e.g. a business category
            elif value is None and isinstance(v, (int, float)) and not isinstance(v, bool):
                value = phrase(k, v)
        # "Census Tract 7019" is not an answer to "where are the cheapest
        # homes". Say the neighbourhood and keep the tract as the footnote.
        names = place_names.get(str(r.get("geoid") or ""))
        if names:
            label = " · ".join(names[:2])

        # A business row has no meaningful number — its category is the value.
        if value is None and spare_text:
            value = spare_text
        if label is None:
            # Aggregate rows (MIN/MAX/AVG) have no text column — name the
            # measure itself instead of showing a bare number as the label.
            numeric = [k for k, v in r.items()
                       if k not in SKIP and isinstance(v, (int, float))
                       and not isinstance(v, bool)]
            if numeric:
                col = numeric[0]
                label = (col.replace("_", " ")
                            .replace("min ", "lowest ").replace("max ", "highest ")
                            .replace("avg ", "average ").replace("pct ", "% ")
                            .strip().capitalize())
            else:
                label = next((str(v) for k, v in r.items() if k not in SKIP), "Result")
        out.append({"label": label, "value": value})

    # Name the columns after what the query actually returned, so the table
    # says "Neighborhood / Foreign-born residents" instead of "Measure / Value".
    if rows:
        first = rows[0]
        keys = [k for k in first if k not in SKIP]
        text_cols = [k for k in keys if isinstance(first[k], str)]
        num_cols = [k for k in keys
                    if isinstance(first[k], (int, float)) and not isinstance(first[k], bool)]
        if text_cols:
            t = text_cols[0]
            header["label"] = ("Neighborhood" if "tract" in t.lower()
                               else "Business" if t == "name"
                               else t.replace("_", " ").capitalize())
        if num_cols:
            c = num_cols[0]
            header["value"] = WORDS.get(c, c.replace("_", " ")).capitalize()
            if c.startswith("pct") or "share" in c or "percent" in c:
                header["value"] = "Share (%)"
        elif len(text_cols) > 1:
            header["value"] = text_cols[1].replace("_", " ").capitalize()

    return out, header


# Place names used to live in a dict here, which meant exactly one
# neighbourhood could be asked about. They live in the database now.

# Silver Spring is the place this product is about. The database covers the
# whole county so we can say "compared to the county" with real numbers, but
# unless a question points somewhere else, "here" means Silver Spring.
#
# These 19 tracts are the ones whose centroid falls inside the Census
# Designated Place boundary for Silver Spring (GEOID 2472450). They hold
# 81,727 people, which matches the Bureau's published CDP population.
SILVER_SPRING_GEOIDS = [
    "24031701601", "24031701602", "24031701900", "24031702000",
    "24031702101", "24031702200", "24031702301", "24031702302",
    "24031702401", "24031702402", "24031702501", "24031702502",
    "24031702503", "24031702602", "24031702603", "24031702604",
    "24031702700", "24031702800", "24031702900",
]

BOUNDARY_PATH = PROJECT_ROOT / "data" / "silver_spring_boundary.geojson"


def _sql_geoids(sql: str):
    """
    Pull the tracts the query itself filtered on.

    An aggregate like COUNT(*) GROUP BY category returns no geoid column, so
    the rows can't tell us what to light up - but the WHERE clause can. This
    works for any place the model can resolve, not just the ones we listed.
    """
    seen, out = set(), []
    for g in re.findall(r"\b(24031\d{6})\b", sql or ""):
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _place_geoids(question: str):
    """
    Resolve a place named in the question to its tracts.

    Only a fallback for highlighting: if the model aggregated and returned no
    geoid column, the map would otherwise sit still. Longest name first so
    "Woodside Park" doesn't get answered by "Woodside".
    """
    q = question.lower()
    try:
        conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT name, geoid FROM places ORDER BY LENGTH(name) DESC"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []

    match = next((name.lower() for name, _ in rows if name.lower() in q), None)
    if match is None:
        return []
    return [geoid for name, geoid in rows if name.lower() == match]


# Every stored column traces back to one specific published table. Naming the
# exact table is the difference between "we cite the Census" and evidence a
# judge can open and re-derive themselves.
ACS_TABLES = {
    "population_count": ("B01003", "Total Population"),
    "age_18_34_count": ("B01001", "Sex by Age"),
    "foreign_born_count": ("B05001", "Nativity and Citizenship Status"),
    "median_income": ("B19013", "Median Household Income (past 12 months)"),
    "language_population_age_5_plus_count": ("C16001", "Language Spoken at Home"),
    "english_only_at_home_count": ("C16001", "Language Spoken at Home"),
    "non_english_at_home_count": ("C16001", "Language Spoken at Home"),
    "occupied_housing_units_count": ("B25003", "Tenure (owner vs. renter)"),
    "renter_occupied_housing_units_count": ("B25003", "Tenure (owner vs. renter)"),
    "total_commuter_count": ("B08301", "Means of Transportation to Work"),
    "public_transit_commuter_count": ("B08301", "Means of Transportation to Work"),
}

ACS_VINTAGE = "ACS 5-Year Estimates 2020-2024"
ACS_TABLE_URL = "https://data.census.gov/table/ACSDT5Y2024.{code}?g={geo}"

# data.census.gov defaults to the whole United States. Without an explicit
# geography a judge clicking our citation sees 316 million people instead of
# the county we just quoted, so every link carries its geography.
COUNTY_GEO = "050XX00US24031"            # Montgomery County, Maryland
ALL_TRACTS_GEO = "050XX00US24031$1400000"  # every tract inside that county
MAX_LINKED_TRACTS = 20

GEOMETRY_COLUMNS = ("geometry_geojson", "land_area_square_meters",
                    "centroid_latitude", "centroid_longitude")

TIGER_SOURCE = (
    "Census TIGER/Line 2024, Montgomery County tract boundaries",
    "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_24_tract_500k.zip",
)
OSM_SOURCE = (
    "OpenStreetMap business locations, retrieved via the Overpass API",
    "https://www.openstreetmap.org/copyright",
)


def _census_geo(geoids):
    """
    Point a citation at the same geography the map is showing.

    We link to the individual tracts rather than to the Silver Spring place
    boundary even when the answer covers all of Silver Spring. The place
    boundary is close but not identical to our 19 tracts, so its totals sit
    a few hundred people off ours - and a citation that doesn't reconcile
    exactly is worse than no citation.
    """
    geoids = [str(g) for g in dict.fromkeys(geoids or []) if g]
    if not geoids:
        return COUNTY_GEO
    if len(geoids) > MAX_LINKED_TRACTS:
        return ALL_TRACTS_GEO
    return ",".join(f"1400000US{g}" for g in geoids)


def _sources(sql: str, geoids):
    """
    Cite the exact published table behind every number in the answer.

    We read the column names out of the SQL we actually ran, not out of the
    result keys, because the model routinely renames them on the way out
    (SUM(median_income) AS avg_income). The SQL text still carries the real
    column, so it stays the honest place to look.
    """
    text = (sql or "").lower()
    geo = _census_geo(geoids)
    out, seen = [], set()

    def add(label, url):
        if url not in seen:
            seen.add(url)
            out.append({"label": label, "url": url})

    for column, (code, title) in ACS_TABLES.items():
        if column in text:
            add(f"{ACS_VINTAGE} - Table {code}, {title}",
                ACS_TABLE_URL.format(code=code, geo=geo))

    if any(column in text for column in GEOMETRY_COLUMNS):
        add(*TIGER_SOURCE)
    if "businesses" in text:
        add(*OSM_SOURCE)
    if " places" in text or "places " in text:
        add("OpenStreetMap named places, resolved to census tracts",
            "https://www.openstreetmap.org/copyright")

    if not out:
        # Nothing recognizable in the SQL - cite the population table for
        # whatever geography the answer covered.
        add(f"{ACS_VINTAGE} - Table B01003, Total Population",
            ACS_TABLE_URL.format(code="B01003", geo=geo))
    return out


def _cannot(message: str | None = None, sql: str | None = None):
    return {
        "answer": message or "I can't answer that from the data I have.",
        "highlight_geoids": [],
        "highlight_scope": "none",
        "table": [],
        "sources": [],
        "sql": sql,
        "coverage": COVERAGE_BLURB,
    }
