"""
CivicLens bridge.

Sits between Sid's database (get_data/) and Aryan's page (page/).

  question  ->  Claude writes SQL  ->  we run it read-only  ->  answer + sources

The model writes the QUERY. The database produces the NUMBERS. Claude never
invents a figure, which is what makes every answer traceable to a source.
"""
from __future__ import annotations

import json
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
                "(which contains Silver Spring). 232 census tracts, 5,953 businesses.",
        "endpoints": {
            "GET  /health": "service + database status",
            "GET  /tracts": "all 232 census tract boundaries as GeoJSON",
            "GET  /businesses": "business locations with category and coordinates",
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
        tracts = conn.execute("SELECT COUNT(*) FROM tract_data").fetchone()[0]
        biz = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        conn.close()
        return {"ok": True, "tracts": tracts, "businesses": biz}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/tracts")
def tracts():
    """
    Every tract boundary as GeoJSON, for the map to draw.
    Geometry only plus a few display values — no AI involved.
    """
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT geoid, tract_name, geometry_geojson, population_count,
                  median_income, foreign_born_count, source_url
           FROM tract_data WHERE geometry_geojson IS NOT NULL"""
    ).fetchall()
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
                "population_count": r["population_count"],
                "median_income": r["median_income"],
                "foreign_born_count": r["foreign_born_count"],
                "source_url": r["source_url"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/businesses")
def businesses(limit: int = 6000):
    """Business points for the map pins."""
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT business_id, name, category, latitude, longitude,
                  geoid, source_name, source_url
           FROM businesses
           WHERE latitude IS NOT NULL AND longitude IS NOT NULL
           LIMIT ?""",
        (limit,),
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

    return {
        "answer": answer,
        "highlight_geoids": [r["geoid"] for r in rows if r.get("geoid")],
        "table": _to_label_value(rows),
        "rows": rows,
        "sources": _sources(rows),
        "sql": executed_sql,
    }


# ---------------------------------------------------------------- helpers

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
        name = WORDS.get(column, column.replace("_", " ").strip())
        if column.startswith("pct") or "percent" in column or "share" in column:
            base = name.replace("pct ", "").replace("percent ", "").strip()
            return f"{v:.1f}% {base}".strip()
        if "income" in column:
            return f"${v:,.0f}"
        if isinstance(v, float):
            return f"{v:,.1f} {name}"
        return f"{v:,} {name}"

    out = []
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
    return out


def _sources(rows):
    """Collect the citation links carried on the rows themselves."""
    seen, out = set(), []
    for r in rows:
        url = r.get("source_url")
        if url and url not in seen:
            seen.add(url)
            out.append({"label": "U.S. Census Bureau / OpenStreetMap", "url": url})
    if not out:
        out.append({
            "label": "ACS 5-Year Estimates 2024, U.S. Census Bureau",
            "url": "https://data.census.gov/",
        })
    return out


def _cannot(message: str | None = None, sql: str | None = None):
    return {
        "answer": message or "I can't answer that from the data I have.",
        "highlight_geoids": [],
        "table": [],
        "sources": [],
        "sql": sql,
        "coverage": COVERAGE_BLURB,
    }
