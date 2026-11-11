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
from answers.community_context import catalog, schema_context, used_sources, notes as community_notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = str(PROJECT_ROOT / "data" / "silverspring.db")
MODEL = (os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5")


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
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
MODEL = os.getenv("ANTHROPIC_MODEL") or MODEL
client = None

app = FastAPI(title="CivicLens")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Question(BaseModel):
    question: str


class ModelNotConfigured(RuntimeError):
    """The data is available, but no inference provider has been configured."""


def _ask_claude(prompt: str, max_tokens: int = 700) -> str:
    global client
    if client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise ModelNotConfigured("Configure ANTHROPIC_API_KEY to enable natural-language answers")
        client = anthropic.Anthropic(api_key=key, timeout=45.0, max_retries=0)
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
        "what": "Public community data for Silver Spring and Montgomery County, Maryland.",
        "endpoints": {
            "GET  /health": "service + database status",
            "GET  /tracts": "Tract approximation of Silver Spring as GeoJSON (?scope=county for all tracts)",
            "GET  /boundary": "the Silver Spring CDP outline as GeoJSON",
            "GET  /businesses": "Mapped listings in the Silver Spring tract approximation (?scope=county for all county listings)",
            "POST /ask": "ask a question in plain English -> answer, sources, and the SQL we ran",
            "GET  /docs": "interactive API explorer",
            "GET  /coverage": "installed primary and supplementary datasets, dates, and limitations",
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


@app.get('/coverage')
def coverage():
    """Report the installed datasets and honest source limitations without AI."""
    with sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        primary = [dict(r) for r in conn.execute('SELECT * FROM data_sources ORDER BY dataset_id')]
    return {'primary_sources': primary, 'community_sources': catalog(DATABASE_PATH),
            'geography_note': 'Census place, current tract, and original partner tract geographies are distinct.'}


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
def businesses(limit: int = 10000, scope: str = "silver-spring"):
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
        f"{SCHEMA_DESCRIPTION}\n\n{schema_context(DATABASE_PATH)}\n\n"
        f"User question: {question}\n\n"
        "Write ONE SQLite SELECT query that answers it. Output only the SQL, "
        "no explanation and no markdown fences. If the question cannot be "
        "answered from these tables, output exactly: CANNOT_ANSWER"
    )
    try:
        sql = _ask_claude(sql_prompt, max_tokens=1500)
    except ModelNotConfigured:
        return _cannot("AI answers are not configured. The data is available, but an administrator must configure the model key before questions can run.")
    except Exception as exc:
        return _cannot(f"Could not reach the language model ({type(exc).__name__}).")

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

    notes = _answer_notes(executed_sql, rows, question)

    # Round rates for display before passing them to the model. Keep the raw
    # query rows in the response so the underlying estimates remain inspectable.
    display_rows = [
        {column: round(value, 1) if isinstance(value, float) and _is_percentage(column) else value
         for column, value in row.items()}
        for row in rows[:25]
    ]
    summary = _business_counts(question, executed_sql, rows)
    if summary is None:
        summary = _place_count(question, executed_sql, rows)
    if summary is None:
        answer_prompt = (
            f"Question: {question}\n\n"
            f"These rows came back from the database:\n"
            f"{json.dumps(display_rows, indent=2)}\n\n"
            f"Notes displayed separately: {' '.join(notes)}\n\n"
            "Lead with the direct answer. For lookups and trends, use one short sentence "
            "with the requested totals or rates. Omit lists of names and extra figures "
            "unless requested. For planning questions, use up to three short sentences "
            "that separate evidence from a next step. Keep planning answers under 55 words. "
            "Do not infer demand, foot traffic or interest from counts. Do not repeat the notes "
            "or claim statistical significance unless the query tested it. Use only "
            "numerical values in the display rows, with digits and without further "
            "rounding or calculation. Do not turn missing rows or a limited list into "
            "zero. Do not mention SQL or databases."
        )
        try:
            summary = _ask_claude(answer_prompt, max_tokens=450)
        except Exception:
            summary = "See the supporting results below."
        if not _uses_returned_numbers(summary, display_rows):
            summary = "See the supporting results below."

    _table, _headers = _to_label_value(rows)
    # Tracts to light up. Aggregate queries (e.g. GROUP BY category) carry no
    # geoid, so fall back to any place named in the question itself.
    highlights = list(dict.fromkeys(str(r["geoid"]) for r in rows
                                    if re.fullmatch(r"24031\d{6}", str(r.get("geoid")))))
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
        "answer": " ".join((summary, *notes)),
        "summary": summary,
        "notes": notes,
        "highlight_geoids": highlights,
        "highlight_scope": scope,
        "table": _table,
        "table_headers": _headers,
        "rows": rows,
        "sources": _sources(executed_sql, highlights, rows),
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


def _business_counts(question, sql, rows):
    """Render simple grouped listing counts directly from the query result."""
    if not (re.match(r"^how many\b", question, re.IGNORECASE)
            and re.search(r"\bbusinesses\b", sql, re.IGNORECASE)
            and re.search(r"\bcount\s*\(", sql, re.IGNORECASE)
            and 1 <= len(rows) <= 6):
        return None
    if re.search(r"\b(compare|versus|trend|between|which|names?|list them|by tract|by neighborhood)\b", question, re.IGNORECASE):
        return None
    types = [row.get("category") for row in rows]
    if not all(isinstance(category, str) and category.strip() for category in types):
        return None
    if len(set(types)) != len(types) or any("geoid" in row for row in rows):
        return None
    items = []
    for row, category in zip(rows, types):
        counts = [value for key, value in row.items()
                  if (key == "count" or key.endswith("_count")) and type(value) is int and value >= 0]
        if len(counts) != 1:
            return None
        label = "café" if category == "cafe" else category.replace("_", " ")
        items.append(f"{counts[0]:,} {label} listings")
    listing_counts = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
    return f"OpenStreetMap shows {listing_counts} in the selected area."


def _place_count(question, sql, rows):
    """Render a direct Census place lookup without adding a numerical claim."""
    if not (re.match(r"^how many\b", question, re.IGNORECASE) and len(rows) == 1
            and re.search(r"\bfrom\s+place_data\b", sql, re.IGNORECASE)
            and not re.search(r"\b(join|union|tract_data|tract_history|industry_employment)\b", sql, re.IGNORECASE)
            and not re.search(r"\b(compare|versus|between|and|by)\b", question, re.IGNORECASE)):
        return None
    row = rows[0]
    estimates = [column for column in ("female_count", "male_count", "population_count")
                 if type(row.get(column)) is int and row[column] >= 0]
    if len(estimates) != 1:
        return None
    column = estimates[0]
    estimate = f"{row[column]:,}"
    if column == "population_count":
        return f"Silver Spring has an estimated population of {estimate} people."
    sex = column.removesuffix("_count")
    return f"The Census estimates that {estimate} Silver Spring residents are classified as {sex}."


def _is_percentage(column):
    return any(word in column.lower() for word in ("pct", "percent", "share"))


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
            "business_id", "geography_type", "duplicate_source_url"}

    # column name -> (how to say it, how to format it)
    WORDS = {
        "population_count":                    "residents",
        "female_count":                        "residents classified female by ACS",
        "male_count":                          "residents classified male by ACS",
        "employed_residents":                  "employed residents",
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
        if _is_percentage(column):
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
        # homes". Say the neighbourhood instead - but only ever in place of a
        # tract number. A business row already has a better label: its name.
        names = place_names.get(str(r.get("geoid") or ""))
        if names and (label is None or "census tract" in label.lower()):
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
            if _is_percentage(c):
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
# 81,727 people in the original tract snapshot. This is a proxy, not a CDP estimate.
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
    "female_count": ("B01001", "Sex by Age"),
    "male_count": ("B01001", "Sex by Age"),
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
    Point a citation at the same geography the answer covers, and say what
    the reader will find there.

    This matters more than it looks. Our Silver Spring figures are the SUM of
    19 tracts, and no Census page publishes that sum - data.census.gov shows
    one tract at a time. Someone clicking through sees a few hundred people
    where our answer said forty thousand and concludes we made it up. So the
    citation states the arithmetic instead of leaving them to guess.

    We link to the tracts rather than the Silver Spring place page on
    purpose: the place boundary is close to our 19 tracts but not identical,
    so its totals sit a few hundred off ours. A citation that nearly
    reconciles is worse than one that explains itself.
    """
    geoids = [str(g) for g in dict.fromkeys(geoids or []) if g]

    if not geoids:
        return COUNTY_GEO, "Montgomery County, Maryland"

    if set(geoids) == set(SILVER_SPRING_GEOIDS):
        return (",".join(f"1400000US{g}" for g in geoids),
                "whole census tracts used to approximate Silver Spring")

    if len(geoids) > MAX_LINKED_TRACTS:
        return ALL_TRACTS_GEO, f"summed across {len(geoids)} census tracts"

    if len(geoids) == 1:
        return f"1400000US{geoids[0]}", "this census tract"

    return (",".join(f"1400000US{g}" for g in geoids),
            f"summed across these {len(geoids)} census tracts")


CDP_GEO = "160XX00US2472450"


def _official_industry(sql, rows):
    if any(row.get("geography_type") for row in rows):
        return any(row.get("geography_type") == "place" for row in rows)
    return any(str(row.get("geoid")) == "2472450" for row in rows) or bool(
        re.search(r"geography_type\s*=\s*['\"]place['\"]|geoid\s*=\s*['\"]?2472450\b", sql)
    )


def _answer_notes(sql, rows, question):
    query = sql.lower()
    notes = []
    industry_place = "industry_employment" in query and _official_industry(query, rows)
    if "place_data" in query or industry_place:
        notes.append("The Census estimate uses Silver Spring’s official Census boundary.")
    if re.search(r"\b(?:female|male)_count\b", query):
        notes.append("The Census estimate describes sex and does not measure self-identified gender.")
    if "industry_employment" in query:
        notes.append("The industry estimate describes employed residents and does not count jobs located here.")
    if re.search(r"\b(?:tract_data|tract_history|businesses|industry_employment)\b", query):
        if _place_geoids(question) and ("places" in query or "businesses" in query):
            notes.append("The mapped Census tracts approximate the named neighborhood or district.")
        elif "place_data" not in query and not industry_place and not re.search(r"\bcounty\b", question.lower()):
            notes.append("Whole Census tracts approximate the area for this result.")
    if "businesses" in query:
        notes.append("OpenStreetMap listings may be incomplete or out of date.")
    return list(dict.fromkeys(notes + community_notes(DATABASE_PATH, sql)))


def _uses_returned_numbers(answer, rows):
    """Reject new written numbers, including values copied from the question."""
    pattern = r"(?<![\w.])[+−-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"

    def numbers(value):
        return {float(match.replace(",", "").replace("−", "-")) for match in re.findall(pattern, value)}

    allowed = {float(value) for row in rows for value in row.values() if type(value) in (float, int)}
    for row in rows:
        for key, value in row.items():
            # Dates, source capacity text and other returned descriptive fields
            # are evidence too. Exclude URLs/opaque IDs as numerical claims.
            if isinstance(value, str) and key not in {"source_url", "duplicate_source_url", "geoid", "source_tract_id", "business_id", "osm_id", "incident_id"}:
                allowed.update(numbers(value))
    return numbers(answer) <= allowed


def _sources(sql, geoids, rows):
    """Attach Census links for the actual level of geography and each data vintage."""
    text = (sql or "").lower()
    geo, geo_note = _census_geo(geoids)
    out, seen = [], set()

    def add(label, url):
        if url not in seen:
            seen.add(url)
            out.append({"label": label, "url": url})

    def census(code, title, geography, note, year=2024):
        add(f"ACS {year - 4}-{year}, Table {code}, {title} ({note})",
            ACS_TABLE_URL.format(code=code, geo=geography).replace("ACSDT5Y2024", f"ACSDT5Y{year}"))

    is_place = "place_data" in text
    uses_tracts = not is_place or bool(re.search(r"\btract_(?:data|history)\b", text))
    for column, (code, title) in ACS_TABLES.items():
        if not re.search(rf"\b{column}\b", text):
            continue
        if is_place and code in {"B01003", "B01001"}:
            census(code, title, CDP_GEO, "official Silver Spring Census place")
        if uses_tracts:
            census(code, title, geo, geo_note)
            if "tract_history" in text:
                census(code, title, geo, geo_note, 2020)
    if "industry_employment" in text:
        industry_place = _official_industry(text, rows)
        if industry_place:
            census("C24050", "Industry by Occupation", CDP_GEO, "residents of the official Silver Spring Census place")
        if not industry_place or any(row.get("geography_type") == "tract" for row in rows):
            census("C24050", "Industry by Occupation", geo, geo_note)
    if any(column in text for column in GEOMETRY_COLUMNS):
        add(*TIGER_SOURCE)
    if "businesses" in text:
        add(*OSM_SOURCE)
    if re.search(r"\bplaces\b", text):
        add("Downtown district and neighborhood mappings use whole tracts",
            "https://montgomeryplanning.org/planning/communities/east-county/silver-spring/silver-spring-downtown-plan/")
    for source in used_sources(DATABASE_PATH, sql):
        add(source['label']+' (partner snapshot; '+str(source['data_vintage'] or 'source date unconfirmed')+')', source['source_url'])
    if not out:
        census("B01003", "Total Population", CDP_GEO if is_place else geo,
               "official Silver Spring Census place" if is_place else geo_note)
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
