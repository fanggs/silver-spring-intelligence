"""
CivicLens bridge.

Sits between Sid's database (get_data/) and Aryan's page (page/).

  question  ->  Claude writes SQL  ->  we run it read-only  ->  answer + sources

The model writes the QUERY. The database produces the NUMBERS. Claude never
invents a figure, which is what makes every answer traceable to a source.
"""
from __future__ import annotations

import json
import math
import re
import os
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from answers.schema import SCHEMA_DESCRIPTION, COVERAGE_BLURB
from answers.sql_guard import run, UnsafeQuery
from answers.community_context import catalog, schema_context, used_sources, notes as community_notes
from answers import demographic_context
from answers.response_cache import AnswerCache

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = str(PROJECT_ROOT / "data" / "silverspring.db")
GAP_LOG_PATH = PROJECT_ROOT / "logs" / "question-gaps.jsonl"
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
answer_cache = AnswerCache()
stage_timings = ContextVar('stage_timings', default=None)

app = FastAPI(title="CivicLens")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=['Server-Timing', 'X-CivicLens-Cache', 'X-CivicLens-Revision'],
)


@app.middleware('http')
async def measure_request(request, call_next):
    stages = []
    token = stage_timings.set(stages)
    start = time.perf_counter()
    try:
        response = await call_next(request)
        stages.append(('total', (time.perf_counter() - start) * 1000))
        response.headers['Server-Timing'] = ', '.join(f'{name};dur={duration:.1f}' for name, duration in stages)
        response.headers['X-CivicLens-Revision'] = os.getenv('RENDER_GIT_COMMIT', 'local')
        return response
    finally:
        stage_timings.reset(token)


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
    started = time.perf_counter()
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    finally:
        stages = stage_timings.get()
        if stages is not None:
            stages.append(('query_model' if max_tokens == 1500 else 'summary_model', (time.perf_counter() - started) * 1000))
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


def _run_query(sql, **options):
    start = time.perf_counter()
    try:
        return run(DATABASE_PATH, sql, **options)
    finally:
        stages = stage_timings.get()
        if stages is not None:
            stages.append(('database', (time.perf_counter() - start) * 1000))


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
            'demographic_measures': demographic_context.measures(DATABASE_PATH),
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
        """SELECT business_id, name, category, category_group, latitude, longitude,
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
def ask(q: Question, response: Response = None, request: Request = None):
    # Direct Python checks bypass the HTTP cache. Evaluations can explicitly
    # request fresh model output using Cache-Control: no-cache.
    key = None
    if response is not None:
        try:
            key = answer_cache.key(DATABASE_PATH, MODEL, q.question)
        except OSError:
            pass
        bypass = request is not None and 'no-cache' in request.headers.get('cache-control', '').lower()
        cached = None if bypass or key is None else answer_cache.get(key)
        response.headers['X-CivicLens-Cache'] = 'HIT' if cached is not None else 'MISS'
        if cached is not None:
            return cached
    result = _answer_question(q)
    if key is not None:
        answer_cache.put(key, result)
    return result


def _answer_question(q: Question):
    """Plain-English question -> real answer, with the SQL and sources shown."""
    question = q.question.strip()
    if not question:
        return _cannot("Please ask a question.")

    # 1. Claude writes the query. It never writes the numbers.
    sql_prompt = (
        f"{SCHEMA_DESCRIPTION}\n\n{schema_context(DATABASE_PATH)}\n\n{demographic_context.schema_context(DATABASE_PATH)}\n\n"
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
        _log_gap(question, "model_error", type(exc).__name__)
        return _cannot(f"Could not reach the language model ({type(exc).__name__}).")

    if "CANNOT_ANSWER" in sql.upper():
        # The model judged that no installed table can answer this. This is
        # the clearest sign of a dataset worth adding.
        _log_gap(question, "no_data_for_question")
        return _cannot()

    # 2. Validate and run it read-only against the real database.
    try:
        rows, executed_sql = _run_query(sql)
    except UnsafeQuery as exc:
        _log_gap(question, "unsafe_query", str(exc), sql)
        return _cannot("I couldn't safely prepare an answer. Please rephrase the question; this does not mean the data is missing.", sql=sql)
    except sqlite3.Error as exc:
        _log_gap(question, "query_error", str(exc), sql)
        return _cannot("I couldn't retrieve the requested information. Please try a more specific question; this does not establish a data gap.", sql=sql)

    if not rows:
        _log_gap(question, "no_rows", None, executed_sql)
        return _cannot("No data matched that question.", sql=sql)

    notes = _answer_notes(executed_sql, rows, question)
    # Tell the user plainly when the list was cut off. It goes first so the
    # page always shows it right under the answer.
    hit_limit = _result_limit(rows, executed_sql, question=question)
    if hit_limit:
        notes = [limited_results_note(hit_limit)] + notes

    # Pass display precision to the model while keeping the raw query values.
    display_rows = []
    for row in rows[:25]:
        display_row = dict(row)
        for column, value in row.items():
            if isinstance(value, float):
                if _is_percentage(column):
                    display_row[column] = round(value, 1)
                elif "income" in column:
                    display_row[column] = round(value)
        display_rows.append(display_row)
    listing_counts = _grouped_business_counts(question, executed_sql, rows)
    summary = None
    next_step = None
    calculations = []
    if listing_counts:
        items = [f"{count:,} {label} listings" for label, count in listing_counts]
        totals = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
        summary = f"OpenStreetMap shows {totals} in the selected area."
    if summary is None:
        summary = _place_count(question, executed_sql, rows)
    if summary is None:
        summary = _demographic_count(question, executed_sql, rows)
    if summary is None:
        summary = _single_listing_count(question, executed_sql, rows)
    if summary is None:
        answer_prompt = (
            f"Question: {question}\n\n"
            f"These rows came back from the database:\n"
            f"{json.dumps(display_rows, indent=2)}\n\n"
            f"Notes displayed separately: {' '.join(notes)}\n\n"
            "Lead with the direct answer in everyday language. If the question asks whether data "
            "exists on something specific, answer that first, whatever order the rows are in. "
            "Write category codes as plain words (sports_centre is 'sports centre') and large "
            "numbers with thousands separators. Write every count in digits, never as a word "
            "('6', not 'six'). Never count rows yourself, for example how many of the listed "
            "venues are hotels: state such a count only if a row or the exact totals give it, "
            "otherwise name the items without a number. Never repeat a number from the "
            "question: say 'your event', not 'a 50-person event'. Write the next step without "
            "numbers ('call a few of the hotels', not 'call 2-3'). If the question asks for "
            "something the rows cannot show, such as capacity, say plainly that it is missing "
            "and still name the matching places. For a lookup or trend, use one "
            "short sentence with the requested totals or rates. Keep names, margins of error, "
            "and extra numbers in the supporting data unless asked for them. "
            "If a measure is missing, say what is missing before any related fact. "
            "For a decision or planning question, give a brief assessment with the strongest "
            "relevant finding and the key unknown. Then add a separate line starting with "
            "'Next step:' followed by one specific, doable action that helps resolve that unknown. "
            "Say who to contact or where to look, what to ask or observe, and how it helps decide. "
            "For example, if demand is unknown, name a specific thing to ask potential customers. "
            "If planning an event, give the organizer a specific question to ask prospective "
            "attendees that will help choose the topic, venue, or format. Avoid generic advice "
            "like 'do market research' and avoid assuming the user can get private sales data. "
            "Do not add a next step to an ordinary lookup. Use plain text, under 55 words total. "
            "For a business idea, discuss only the requested business type unless the user asks "
            "about related types. Give the mapped count and leave any rating of competition "
            "out unless the results include a meaningful benchmark. Describe map results "
            "as mapped listings. Counts alone cannot establish demand, "
            "foot traffic, event interest, or whether a business is open. Do not call counts a proxy for those measures, even if a returned label does. Income and renter share "
            "do not establish housing prices. Put boundary and source caveats only in the separate notes. "
            "Those notes describe the source datasets, not these rows: never call a "
            "returned count countywide or unfiltered because a note mentions the source coverage. "
            "Do not restate them "
            "or claim statistical significance unless the query tested it. Use only "
            "numerical values in the display rows, with digits and without further "
            "rounding. Never do arithmetic yourself. When the answer needs any calculation "
            "(a difference, ratio, share, percentage, percentage-point or percent change, "
            "or a per-person rate), write it as [[calc: expression]] using only numbers that "
            "appear in the rows or the exact totals, with + - * / and parentheses, "
            "and code will compute it exactly. Example: '[[calc: 6475 / 46937 * 100]]% of commuters'. "
            "For a decrease, write the calculation so the result is positive and say 'fell by' "
            "(e.g. 'fell by [[calc: 24.6 - 13.8]] percentage points'), never 'dropped by -10.8'. "
            "Only calculate what is meaningful: never add or average medians, rates, "
            "percentages or margins of error. Do not turn missing rows or a limited list into "
            "zero. Do not mention SQL or databases."
        )
        # Code does all arithmetic, exactly. The model may only describe a
        # calculation over returned numbers; a number it works out itself is
        # rejected by the guard below.
        total_facts, total_values = _exact_totals(rows, executed_sql, question=question)
        if hit_limit:
            known = (f" The full number of matches is {hit_limit['total']:,}; state it."
                     if hit_limit.get("total") else
                     " The full number of matches is unknown; do not guess it.")
            answer_prompt += (
                f" Only the first {hit_limit['shown']:,} matching rows were returned, so the list "
                "is not complete." + known + f" The user sees all {hit_limit['shown']:,} returned rows "
                f"in a table; you are shown only the first {len(display_rows):,}, so never say "
                f"{len(display_rows):,} are shown. Still answer the question: "
                "say plainly that these are the first results, then summarise what they show. "
                "Never present the shown rows as everything."
            )
            total_values = set(total_values) | {float(hit_limit["shown"])}
        elif len(rows) > len(display_rows):
            answer_prompt += (
                f" You are shown the first {len(display_rows):,} of {len(rows):,} rows; the user "
                "can see all of them in the table. Do not describe only the rows you see as if "
                "they were everything."
            )
        if total_facts:
            answer_prompt += (
                " Exact totals already computed from every returned row: "
                + " ".join(total_facts)
                + " If you state a total, use one of these exactly."
            )
        # A count that mixes several kinds of place gets its own breakdown
        # from the database, so the answer names what the places are (5
        # hotels, 2 community centres) rather than a label the model invented.
        breakdown_facts, breakdown_values = _category_breakdowns(executed_sql, rows)
        if breakdown_facts:
            answer_prompt += (
                " Breakdowns by type, counted by the database: " + " ".join(breakdown_facts)
                + " When you mention such a count, give it with this breakdown, e.g. "
                "'8 possible venues: 5 hotels, 2 community centres and 1 events venue'. "
                "Name each kind as the data does. The data has no capacity or meeting-room "
                "information, so call hotels and community centres possible venues and never "
                "call a hotel a conference venue."
            )
            total_values = set(total_values) | breakdown_values
        operands = _returned_numbers(rows) | _returned_numbers(display_rows) | set(total_values)
        last_sections = None
        calculations = []
        for attempt in range(2):
            try:
                response = _ask_claude(answer_prompt, max_tokens=450)
            except Exception as exc:
                _log_gap(question, "model_error", type(exc).__name__, executed_sql)
                break
            response, calculations = _apply_calculations(response, operands)
            computed = {c["result"] for c in calculations if "result" in c}
            allowed_extra = set(total_values) | computed
            sections = re.split(r"\s*(?:\*\*)?\bnext steps?(?:\*\*)?\s*:\s*(?:\*\*)?\s*", response, maxsplit=1, flags=re.IGNORECASE)
            candidate = sections[0].strip()
            last_sections = sections
            unverified = []
            if candidate and _uses_returned_numbers(candidate, display_rows, executed_sql, allowed_extra, unverified):
                summary = candidate
                if len(sections) == 2:
                    next_step = sections[1].strip() or None
                break
            if attempt == 0:
                named = ", ".join(_format_calculated(n) for n in unverified)
                answer_prompt += (f" Your last answer stated {named or 'a figure'}, which the rows do not "
                                  "contain (a number repeated from the question counts too). Rewrite it without "
                                  "that number, using only figures exactly as returned, or [[calc: ...]] for "
                                  "any calculation. Your last answer was: " + response)
        if summary is None and last_sections:
            # Both attempts stated at least one figure the database did not
            # return. Keep only the sentences whose every number checks, so
            # the rule still holds and the user still gets the parts that do.
            summary = _verified_sentences(last_sections[0], display_rows, executed_sql, allowed_extra)
            dropped = [s for s in _split_sentences(last_sections[0])
                       if not _uses_returned_numbers(s, display_rows, executed_sql, allowed_extra)]
            _log_gap(question, "trimmed_answer" if summary else "no_verified_answer",
                     {"dropped_sentences": dropped, "calculations": calculations}, executed_sql)
            if summary and len(last_sections) == 2:
                next_step = last_sections[1].strip() or None
        if summary is None:
            summary = "Open the supporting data for the results."
        if next_step and not _uses_returned_numbers(next_step, display_rows, executed_sql, allowed_extra if last_sections else total_values):
            _log_gap(question, "dropped_next_step", next_step, executed_sql)
            next_step = None

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
    geography = _answer_geography(executed_sql, rows, highlights)
    if geography['kind'] == 'census_place':
        highlights, scope = [], 'boundary'

    return {
        "answer": " ".join(filter(None, (summary, next_step, *notes))),
        "summary": summary,
        "next_step": next_step,
        "notes": notes,
        "visual": _answer_visual(executed_sql, rows, listing_counts),
        "statistic": _answer_statistic(rows),
        "supporting_data": _supporting_data(rows),
        "listing_details": _listing_details(executed_sql),
        "highlight_geoids": highlights,
        "highlight_scope": scope,
        "geography": geography,
        "table": _table,
        "table_headers": _headers,
        "rows": rows,
        "sources": _sources(executed_sql, highlights, rows),
        "sql": executed_sql,
        # Every calculation shown in the answer, with the exact expression
        # over returned numbers, so any figure can be re-checked by hand.
        "calculations": calculations,
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


def _grouped_business_counts(question, sql, rows):
    """Read comparable listing counts from the returned rows."""
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
        items.append((label, counts[0]))
    return items


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
    prefix = "Self-identified gender is not measured by this dataset. " if re.search(r'identif|gender', question, re.I) else ''
    return prefix + f"The Census estimates that {estimate} Silver Spring residents are classified as {sex}."


def _demographic_count(question, sql, rows):
    """Format one fully covered returned demographic estimate without inference."""
    if not (re.match(r'^how many\b', question, re.I) and len(rows) == 1
            and 'demographic_estimates' in sql.lower()):
        return None
    row = rows[0]
    value = row.get('estimate')
    if type(value) is not int or value < 0 or not row.get('label') or not row.get('acs_vintage'):
        return None
    if row.get('available_tracts') != row.get('requested_tracts'):
        return None
    if re.search(r'\bsum\s*\(', sql, re.I) and row.get('requested_tracts') is None:
        return None
    if demographic_context.is_place(sql, rows):
        area = 'the official Silver Spring Census place'
    else:
        with sqlite3.connect(f'file:{DATABASE_PATH}?mode=ro', uri=True) as con:
            names = [r[0] for r in con.execute('SELECT DISTINCT name FROM places ORDER BY LENGTH(name) DESC')]
        place = next((name for name in names if name.lower() in question.lower()), None)
        area = 'the tract area used to approximate ' + place if place else 'the selected Census tracts'
    label = ', '.join(reversed(str(row['label']).split(' > ')))
    return f"{row['acs_vintage']} estimates {value:,} residents classified as {label} in {area}."


def _answer_geography(sql, rows, highlights):
    """Separate official place estimates from a place-name point's tract."""
    text = sql.lower()
    point_or_tract = re.search(r'\b(?:businesses|tract_data|tract_history|community_\w+)\b', text)
    official = 'place_data' in text or ('industry_employment' in text and _official_industry(text, rows)) or demographic_context.is_place(sql, rows)
    returned_ids = {str(r['geoid']) for r in rows if r.get('geoid') is not None}
    mixed = re.search(r'\bunion\b', text) or any(r.get('geography_type') not in (None, 'place') for r in rows)
    if official and not point_or_tract and not mixed and returned_ids <= {'2472450'}:
        return {'kind': 'census_place', 'id': '2472450', 'label': 'Silver Spring Census place',
                'approximate': False, 'boundary_url': '/boundary'}
    return {'kind': 'tract_approximation', 'geoids': highlights,
            'label': 'Whole Census tracts', 'approximate': True}


def _single_listing_count(question, sql, rows):
    """Avoid a second model call for an unambiguous single count."""
    if not (re.match(r'^how many\b', question, re.I) and len(rows) == 1
            and re.search(r'\bfrom\s+businesses\b', sql, re.I)
            and not re.search(r'\b(?:union|group\s+by|join)\b', sql, re.I)):
        return None
    counts = [(key, value) for key, value in rows[0].items()
              if (key == 'count' or key.endswith('_count')) and type(value) is int]
    if len(counts) != 1:
        return None
    key, count = counts[0]
    label = key.removesuffix('_count').removesuffix('_listing').replace('_', ' ')
    if label in ('count', 'business', 'listing'):
        # A generic column name: name what the query actually filtered on
        # (osm_amenity = 'restaurant' -> "restaurant"), so 43 restaurants are
        # never described as 43 businesses. Only one exact filter qualifies.
        filters = re.findall(r"\b(?:osm_amenity|osm_shop|category)\s*=\s*'([a-z_]+)'", sql, re.I)
        label = filters[0].replace('_', ' ') if len(set(filters)) == 1 else 'business'
    with sqlite3.connect(f'file:{DATABASE_PATH}?mode=ro', uri=True) as con:
        names = [r[0] for r in con.execute('SELECT DISTINCT name FROM places ORDER BY LENGTH(name) DESC')]
    name = next((n for n in names if n.lower() in question.lower()), None)
    area = f'the tract area used to approximate {name}' if name else 'the selected tract area'
    return f'There are {count:,} mapped {label} listings in {area}.'


def _listing_details(sql):
    """Expose records behind simple aggregates using the original filter."""
    if not re.match(r'^SELECT\s+COUNT\s*\(', sql, re.I) or re.search(r'\b(?:join|union|group\s+by|having)\b', sql, re.I):
        return None
    match = re.search(r'\bFROM\s+businesses\s+WHERE\s+(.+)', sql, re.I | re.S)
    if not match:
        return None
    try:
        records, _ = _run_query('SELECT name, category, geoid, source_url FROM businesses WHERE ' + match[1], max_rows=201)
    except (UnsafeQuery, sqlite3.Error):
        return None
    return {'rows': records[:200], 'truncated': len(records) > 200}


def _is_percentage(column):
    return any(word in column.lower() for word in ("pct", "percent", "share"))


_HIDDEN_FIELDS = {"geoid", "source_url", "state_fips", "county_fips", "tract_code",
                  "latitude", "longitude", "centroid_latitude", "centroid_longitude",
                  "business_id", "geography_type", "duplicate_source_url", "geometry_geojson",
                  "period_start", "period_end"}

_COLUMN_LABELS = {
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
_COLUMN_LABELS.update({
    "acs_vintage": "Census period",
    "listing_count": "Mapped listings",
    "transit_pct": "Public transit commute share",
    "transit_commuters": "Public transit commuters",
    "population_margin_of_error": "Population margin of error",
    "female_margin_of_error": "Female estimate margin of error",
    "male_margin_of_error": "Male estimate margin of error",
})


def _column_label(column):
    return _COLUMN_LABELS.get(column, column.replace("_", " ")).capitalize()


def _display_value(column, value):
    if value is None:
        return "Not available"
    if type(value) not in (int, float):
        text = str(value)
        if column in {'metric', 'label'}:
            text = re.sub(r'\s*\(foot[- ]traffic proxy\)', '', text, flags=re.I).replace('_', ' ')
            text = ', '.join(reversed(text.split(' > ')))
        return text if len(text) <= 180 else text[:180] + '… (full text below)'
    if _is_percentage(column):
        return f"{value:.1f}%"
    if "income" in column:
        return f"${value:,.0f}"
    return f"{value:,.1f}" if isinstance(value, float) else f"{value:,}"


def _supporting_data(rows):
    columns = list(dict.fromkeys(key for row in rows for key in row if key not in _HIDDEN_FIELDS))
    return {
        "columns": [_column_label(column) for column in columns],
        "rows": [[_display_value(column, row.get(column)) for column in columns] for row in rows],
    }


def _answer_statistic(rows):
    if len(rows) != 1:
        return None
    counts = [(key, value) for key, value in rows[0].items()
              if (key in {'count', 'estimate'} or key.endswith('_count')) and type(value) is int and value >= 0
              # How many tracts went into a figure is method, not the answer.
              and not key.startswith('tract')]
    if len(counts) != 1:
        return None
    key, value = counts[0]
    return {'value': value, 'label': _display_value('label', rows[0]['label']) if key == 'estimate' and rows[0].get('label') else _column_label(key)}


def _answer_visual(sql, rows, listing_counts):
    if listing_counts and len(listing_counts) > 1 and any(value for _, value in listing_counts):
        return {
            "title": "Mapped listings by type",
            "scale_max": max(value for _, value in listing_counts),
            "items": [{"label": label.capitalize(), "value": value, "display": f"{value:,}"}
                      for label, value in listing_counts],
        }
    if not ("tract_history" in sql.lower() and 2 <= len(rows) <= 5):
        return None
    labels = [row.get("acs_vintage") for row in rows]
    if not all(isinstance(label, str) and re.fullmatch(r"ACS \d{4}-\d{4}", label) for label in labels):
        return None
    if len(set(labels)) != len(labels):
        return None
    measures = [column for column in rows[0] if _is_percentage(column)]
    if len(measures) != 1:
        return None
    measure = measures[0]
    values = [row.get(measure) for row in rows]
    if not all(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100 for value in values):
        return None
    return {
        "title": _column_label(measure) + " by Census period",
        "scale_max": 100,
        "items": [{"label": label.removeprefix("ACS "), "value": round(value, 1),
                   "display": _display_value(measure, value)} for label, value in sorted(zip(labels, values))],
    }


def _to_label_value(rows):
    """Keep the original two-column response available to existing clients."""
    place_names = _place_names_by_geoid()

    out = []
    header = {"label": "Result", "value": "Value"}
    for r in rows:
        label = None
        value = None
        spare_text = None
        for k, v in r.items():
            if k in _HIDDEN_FIELDS:
                continue
            if isinstance(v, str):
                if label is None:
                    label = v.split(";")[0].strip()      # "Census Tract 7016.02"
                elif spare_text is None:
                    spare_text = v                       # e.g. a business category
            elif value is None and isinstance(v, (int, float)) and not isinstance(v, bool):
                value = _display_value(k, v)
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
                       if k not in _HIDDEN_FIELDS and isinstance(v, (int, float))
                       and not isinstance(v, bool)]
            if numeric:
                col = numeric[0]
                label = (col.replace("_", " ")
                            .replace("min ", "lowest ").replace("max ", "highest ")
                            .replace("avg ", "average ").replace("pct ", "% ")
                            .strip().capitalize())
            else:
                label = next((str(v) for k, v in r.items() if k not in _HIDDEN_FIELDS), "Result")
        out.append({"label": label, "value": value})

    # Name the columns after what the query actually returned, so the table
    # says "Neighborhood / Foreign-born residents" instead of "Measure / Value".
    if rows:
        first = rows[0]
        keys = [k for k in first if k not in _HIDDEN_FIELDS]
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
            header["value"] = _column_label(c)
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
    if 'demographic_estimates' in query:
        if any(row.get('available_tracts') != row.get('requested_tracts') for row in rows):
            notes.append('Some selected tracts have missing estimates; the returned figure is incomplete and is not a full-area total.')
        notes.append('Race and Hispanic-origin estimates use ACS 2020-2024. Race-alone categories include Hispanic residents unless an origin is specified; Hispanic origin overlaps race.')
        if demographic_context.is_place(sql, rows):
            notes.append('The Census estimate uses Silver Spring’s official Census boundary.')
        elif _place_geoids(question):
            notes.append('The mapped Census tracts approximate the named neighborhood or district.')
        else:
            notes.append('Race estimates describe the selected Census tracts, not a precise neighborhood boundary.')
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
    if re.search(r'foot[- ]traffic|demand|proxy', json.dumps(rows), re.I) and 'businesses' in query:
        notes.append('Business listing counts do not measure foot traffic or customer demand.')
    return list(dict.fromkeys(notes + community_notes(DATABASE_PATH, sql)))


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "twenty": 20, "fifty": 50, "hundred": 100}


def _asked_for_count(question):
    """Numbers the user asked for, as in 'top 5', 'five most', 'first 10'."""
    found = set()
    for word in re.findall(r"\b(?:top|first|largest|biggest|smallest|lowest|highest)\s+(\w+)|\b(\w+)\s+(?:most|least|largest|biggest|highest|lowest)\b", question.lower()):
        token = next(w for w in word if w)
        if token.isdigit():
            found.add(int(token))
        elif token in _NUMBER_WORDS:
            found.add(_NUMBER_WORDS[token])
    return found


def _result_limit(rows, sql, max_rows=200, question=""):
    """How the result was cut off, or None if it is the complete answer.

    A limit is a maximum, so getting exactly that many rows back means the
    database may have stopped early. Returns {"shown": n, "total": t}, where
    total is the true number of matches when the query counted it with
    COUNT(*) OVER () (counted before the cut-off), otherwise None.
    Not a cut-off: fewer rows than the limit, a total_count showing nothing
    is missing, or a limit equal to what the user asked for ("top 5").
    """
    totals = {row.get("total_count") for row in rows}
    total = totals.pop() if len(totals) == 1 and type(next(iter(totals), None)) is int else None
    limit = re.search(r"\blimit\s+(\d+)\s*;?\s*$", sql or "", re.I)
    limit = int(limit.group(1)) if limit else None
    hit = (limit is not None and len(rows) >= limit) or len(rows) >= max_rows
    if not hit:
        return None
    if total is not None and total <= len(rows):
        return None
    if limit is not None and len(rows) == limit and limit in _asked_for_count(question) and total is None:
        return None  # The user asked for exactly this many.
    return {"shown": len(rows), "total": total}


def limited_results_note(cut):
    shown, total = cut["shown"], cut.get("total")
    if total:
        return (f"Showing the first {shown:,} of {total:,} results. "
                f"The other {total - shown:,} aren't listed here.")
    return (f"Only the first {shown:,} results are shown. There may be more "
            "that aren't listed here, so don't treat this as the full list.")


def _exact_totals(rows, sql, max_rows=200, question=""):
    """Totals computed by code from ALL returned rows, never by the model.

    Returns (facts, values): readable lines for the prompt, and the numbers
    an answer may state. Only arithmetic that is exact for this result:
    - how many records came back, but only when nothing was cut off (a
      LIMIT or the row cap means the true count may be higher);
    - the sum of each whole-number count column across rows, so two rows
      of 1 bookstore each are honestly "2 bookstores".
    Medians, rates, percentages and margins of error are never summed:
    adding those gives a number that looks precise and means nothing.
    """
    facts, values = [], set()
    cut_off = _result_limit(rows, sql, max_rows, question) is not None
    if rows and not cut_off and len(rows) > 1:
        if re.search(r"\bgroup\s+by\b", sql or "", re.I):
            facts.append(f"The complete result has {len(rows):,} groups (one row per group, "
                         "e.g. per category). That is a number of groups, not of listings or records.")
        else:
            facts.append(f"{len(rows):,} records were returned (the complete result).")
        values.add(float(len(rows)))
    if len(rows) > 1 and not cut_off:
        for column in dict.fromkeys(key for row in rows for key in row):
            is_count = column == "count" or column.endswith(("_count", "_listings")) or column in {"listings", "listing_count"}
            if not is_count or _is_percentage(column):
                continue
            cells = [row.get(column) for row in rows]
            if all(type(cell) is int for cell in cells):
                total = sum(cells)
                facts.append(f"{_column_label(column)} adds up to {total:,} across all {len(rows):,} rows.")
                values.add(float(total))
    return facts, values


CALC_ERROR = "[calculation unavailable]"
_CALC = re.compile(r"\[\[\s*calc\s*:\s*(.*?)\s*\]\]", re.I | re.S)


def _returned_numbers(rows):
    return {float(v) for row in rows for v in row.values() if type(v) in (int, float)}


def _apply_calculations(text, operands):
    """Replace each [[calc: expr]] with its exact result, computed here.

    The model chooses WHAT to calculate; code does the arithmetic. Every
    operand must be a number the database returned (or an exact total
    computed from those rows). The only other constant allowed is 100, for
    percentages. Anything else (an invented operand, a function call,
    division by zero) becomes CALC_ERROR, which the number guard rejects,
    so its sentence is dropped rather than shown.
    Returns (new_text, calculations) where each calculation records the
    expression, the result, or the reason it was refused.
    """
    import ast
    import operator
    ops = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv}
    allowed = set(operands) | {100.0}
    calculations = []

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](evaluate(node.left), evaluate(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -evaluate(node.operand)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if float(node.value) not in allowed:
                raise ValueError(f"{node.value} is not a returned number")
            return float(node.value)
        raise ValueError("only + - * / and parentheses are allowed")

    def replace(match):
        expression = match.group(1).replace(",", "").replace("×", "*").replace("÷", "/").replace("−", "-")
        try:
            result = evaluate(ast.parse(expression, mode="eval"))
        except (ValueError, SyntaxError, ZeroDivisionError, RecursionError) as exc:
            calculations.append({"expression": match.group(1), "error": str(exc)})
            return CALC_ERROR
        shown = _format_calculated(result)
        calculations.append({"expression": match.group(1), "result": float(shown.replace(",", ""))})
        return shown

    return _CALC.sub(replace, text), calculations


def _format_calculated(value):
    """Whole numbers with thousands separators; anything else to 1 decimal."""
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


_CATEGORY_FILTER = re.compile(
    r"((?:\w+\.)?category)\s*(?:IN\s*\(([^)]*)\)|=\s*('[^']*'))", re.I)


def _count_parts(sql):
    """Each single-row COUNT in the query, with its table, filter and categories.

    Only plain parts qualify: one COUNT, no GROUP BY, a WHERE clause and a
    category filter. Anything else is skipped rather than guessed at.
    """
    parts = []
    for text in re.split(r"\bUNION\s+ALL\b", sql or "", flags=re.I):
        if not re.search(r"\bCOUNT\s*\(", text, re.I) or re.search(r"\bGROUP\s+BY\b", text, re.I):
            continue
        clause = re.search(r"\bFROM\s+(.+?)\bWHERE\b(.+)$", text, re.I | re.S)
        category = _CATEGORY_FILTER.search(text)
        if not clause or not category:
            continue
        where = re.sub(r"\b(?:ORDER\s+BY|LIMIT)\b.*$", "", clause.group(2), flags=re.I | re.S).strip()
        values = re.findall(r"'([^']*)'", category.group(2) or category.group(3))
        rest = re.sub(r"\s+", " ", where.replace(category.group(0), "")).strip().lower()
        parts.append({"from": clause.group(1).strip(), "where": where, "column": category.group(1),
                      "categories": set(values), "scope": (clause.group(1).split()[0].lower(), rest)})
    return parts


def _row_label(rows, value):
    """The text label of the one returned row holding this count, if exactly one does."""
    matches = [row for row in rows if any(type(v) is int and v == value for v in row.values())]
    if len(matches) != 1:
        return None
    texts = [v for k, v in matches[0].items() if isinstance(v, str) and "url" not in k and k != "geoid"]
    return texts[0] if texts else None


def _category_breakdowns(sql, rows):
    """Database-counted breakdowns for counts that mix several kinds of place.

    For a COUNT filtered to several categories (category IN ('hotel', ...)),
    re-run the same filter grouped by category, read-only, and keep it only
    if the parts add up to the count the query returned. Also flag a count
    whose categories sit entirely inside another count over the same table
    and area, so a total and its own part are never presented as separate.
    Returns (facts for the prompt, numbers the answer may state).
    """
    facts, values = [], set()
    parts = _count_parts(sql)
    for part in parts:
        if len(part["categories"]) < 2:
            continue
        breakdown_sql = (f"SELECT {part['column']} AS category, COUNT(*) AS n FROM {part['from']} "
                         f"WHERE {part['where']} GROUP BY {part['column']} ORDER BY n DESC, 1")
        try:
            counted, _ = _run_query(breakdown_sql)
        except Exception:
            continue
        total = sum(r["n"] for r in counted)
        if not counted or total not in {v for row in rows for v in row.values() if type(v) is int}:
            continue  # Does not reconcile with the returned count: say nothing.
        label = _row_label(rows, total)
        kinds = ", ".join(f"{r['n']:,} {str(r['category']).replace('_', ' ')}" for r in counted)
        facts.append(f"The {total:,}" + (f" in '{label}'" if label else "") + f" are: {kinds}.")
        values.update(float(r["n"]) for r in counted)
        part["total"], part["label"] = total, label
    for inner in parts:
        for outer in parts:
            if (inner is not outer and outer.get("total") and inner["scope"] == outer["scope"]
                    and inner["categories"] < outer["categories"]):
                name = ", ".join(sorted(c.replace("_", " ") for c in inner["categories"]))
                facts.append(f"The {name} count is already included in the {outer['total']:,}"
                             + (f" ('{outer['label']}')" if outer["label"] else "")
                             + ": never present them as separate groups or add them together.")
    return facts, values


def _split_sentences(text):
    """Split at '. ', '! ', '? ' and line breaks; '3.5' and 'U.S.' stay whole."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _log_gap(question, reason, detail=None, sql=None):
    """Record a question the app could not fully answer, and why.

    Reasons: no_data_for_question (no table covers it: the strongest hint a
    dataset is missing), no_rows, query_error, unsafe_query, model_error,
    trimmed_answer (some sentences dropped), no_verified_answer, and
    dropped_next_step. Printed as one JSON line prefixed CIVICLENS_GAP, which
    shows up in the Render logs, and appended to logs/question-gaps.jsonl
    when running locally. Summarise with: python -m evals.gaps
    """
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "question": question, "reason": reason, "detail": detail, "sql": sql}
    line = json.dumps(record, default=str)
    print("CIVICLENS_GAP " + line, flush=True)
    path = os.environ.get("CIVICLENS_GAP_LOG") or (None if os.environ.get("RENDER") else str(GAP_LOG_PATH))
    if path:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # Logging must never break an answer.


def _verified_sentences(text, rows, sql=None, extra=()):
    """The sentences of `text` that use only returned figures, or None."""
    sentences = _split_sentences(text)
    kept = [s for s in sentences if _uses_returned_numbers(s, rows, sql, extra)]
    # A lone fragment like "Yes." or "Here is what we found:" is not an answer.
    if not kept or not any(len(s.split()) >= 6 for s in kept):
        return None
    return " ".join(kept)


_UNITS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
          "dozen": 12}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_ONES = {"one": 1, **{k: v for k, v in _UNITS.items() if v < 10}}
_NUMBER_WORD = re.compile(
    r"\b(?:(?P<tens>" + "|".join(_TENS) + r")(?:[\s-](?P<ones>" + "|".join(_ONES) + r"))?"
    r"|(?P<unit>" + "|".join(_UNITS) + r"))(?:\s+(?P<scale>hundred|thousand))?\b", re.I)


def _word_numbers(text):
    """Counts written as words ('six hotels', 'twenty-one', 'three hundred').

    A word number is a figure like any other, so it must pass the same check
    as digits. 'one' alone is not read as a count: it is far more often
    'one step' or 'no one' than a statistic.
    """
    found = set()
    for match in _NUMBER_WORD.finditer(text):
        if match.group("tens"):
            value = _TENS[match.group("tens").lower()] + _ONES.get((match.group("ones") or "").lower(), 0)
        else:
            value = _UNITS[match.group("unit").lower()]
        scale = (match.group("scale") or "").lower()
        value *= 100 if scale == "hundred" else 1000 if scale == "thousand" else 1
        found.add(float(value))
    return found


def _uses_returned_numbers(answer, rows, sql=None, extra=(), missing=None):
    """Reject new written numbers, including values copied from the question.

    Numbers count whether written as digits ('6') or as words ('six').
    One exception: a calendar year that the executed query itself filtered on
    (e.g. '2025-01-01' in a date range). That year names the scope the
    database answered for; it is not a statistic, so "In 2025, ..." passes.
    """
    pattern = r"(?<![\w.])[+−-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"

    def numbers(value):
        digits = {float(match.replace(",", "").replace("−", "-")) for match in re.findall(pattern, value)}
        return digits | _word_numbers(value)

    if CALC_ERROR in answer:
        return False
    allowed = {float(value) for row in rows for value in row.values() if type(value) in (float, int)}
    allowed.update(extra)
    if sql:
        for literal in re.findall(r"'([^']*)'", sql):
            allowed.update(float(year) for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?=-\d{2}(?:-\d{2})?(?!\d))", literal))
    for row in rows:
        for key, value in row.items():
            # Dates, source capacity text and other returned descriptive fields
            # are evidence too. Exclude URLs/opaque IDs as numerical claims.
            if isinstance(value, str) and key not in {"source_url", "duplicate_source_url", "geoid", "source_tract_id", "business_id", "osm_id", "incident_id"}:
                allowed.update(numbers(value))
    unsupported = numbers(answer) - allowed
    if missing is not None:
        missing.extend(sorted(unsupported))
    return not unsupported


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

    for code in demographic_context.source_tables(sql, rows):
        title = 'Race' if code == 'B02001' else 'Hispanic or Latino Origin by Race'
        place = demographic_context.is_place(sql, rows)
        census(code, title, CDP_GEO if place else geo,
               'official Silver Spring Census place' if place else geo_note)

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
