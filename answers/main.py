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
from collections import Counter
import queue
import re
import os
import sqlite3
import threading
import time
from contextlib import closing
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from answers.schema import SCHEMA_DESCRIPTION, COVERAGE_BLURB
from answers.sql_guard import run, UnsafeQuery
from answers.community_context import CRIME_GROUPS, catalog, crime_group, schema_context, used_sources, notes as community_notes
from answers import demographic_context, insight_context
from answers.response_cache import AnswerCache

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = str(PROJECT_ROOT / "data" / "silverspring.db")
GAP_LOG_PATH = PROJECT_ROOT / "logs" / "question-gaps.jsonl"
# Haiku 5.5: measured on 18 test questions (Nov 16, 2026) it stated no wrong
# figure in 36 answers and cost about $0.0013 a question, a tenth of Sonnet 5.
# It is slower (about 8 s), so the page shows progress while it works.
MODEL = (os.getenv("ANTHROPIC_MODEL") or "claude-haiku-5-5")


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
# Set by /ask/stream: called with each step's name so the page can show it.
progress_listener = ContextVar('progress_listener', default=None)


def _progress(stage):
    listener = progress_listener.get()
    if listener is not None:
        listener(stage)

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


class ModelTruncated(RuntimeError):
    """The model hit max_tokens: its text may be cut off and must not be used."""


# The model thinks before it writes (adaptive thinking is on by default for
# this model). Effort sets how much. Measured on the eval questions: "medium"
# wrote correct, runnable SQL every time; "low" failed a two-table query and
# missed venues; the default "high" spent up to 1,570 tokens on one query,
# which overran the old 1,500-token cap and returned no text at all. The cap
# is a ceiling, not a charge: only tokens actually used are billed.
# Rechecked on Haiku 5.5: "low", or thinking turned off, answered faster but
# counted crimes for the wrong area and lost the main number from answers.
SQL_EFFORT, SQL_MAX_TOKENS = "medium", 8000
ANSWER_EFFORT, ANSWER_MAX_TOKENS = "medium", 4000


def _ask_claude(prompt: str, max_tokens: int = 700, *, effort: str | None = None,
                cache_prefix: str | None = None, stage: str = "summary_model") -> str:
    """One model call. Returns the text, or raises; never returns cut-off text.

    cache_prefix: a leading part of `prompt` that is identical on every
    request (the database description, about 9,000 tokens). It is sent as
    its own block marked for prompt caching, so repeat questions within a
    few minutes read it at a tenth of the input price.
    """
    global client
    if client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise ModelNotConfigured("Configure ANTHROPIC_API_KEY to enable natural-language answers")
        client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=0)
    content = prompt
    if cache_prefix and prompt.startswith(cache_prefix) and len(prompt) > len(cache_prefix):
        content = [{"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": prompt[len(cache_prefix):]}]
    options = {"output_config": {"effort": effort}} if effort else {}
    started = time.perf_counter()
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
            **options,
        )
    finally:
        stages = stage_timings.get()
        if stages is not None:
            stages.append((stage, (time.perf_counter() - started) * 1000))
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise ModelTruncated("the model's response hit max_tokens and may be cut off")
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


# Render sets RENDER_GIT_COMMIT on every deploy. Reporting it lets anyone
# confirm which version is live with one request instead of guessing from
# changed behavior.
RUNNING_COMMIT = (os.environ.get("RENDER_GIT_COMMIT") or "local")[:7]


@app.get("/health")
def health():
    """Alive check, the running commit, and whether the database is present."""
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
            "commit": RUNNING_COMMIT,
            "model": MODEL,
            "silver_spring": {"tracts": ss_tracts, "businesses": ss_biz},
            "county": {"tracts": tracts, "businesses": biz},
        }
    except Exception as exc:
        return {"ok": False, "commit": RUNNING_COMMIT, "error": str(exc)}


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
        """SELECT geoid, name FROM places WHERE kind != 'planning area'
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
                "area_label": _area_labels().get(r["geoid"]),
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
    points = [dict(r) for r in rows]
    # Street address for the map popup, from place_locations (OpenStreetMap).
    lookup = _place_lookup(point["source_url"] for point in points)
    for point in points:
        place = lookup.get(_osm_ref(point["source_url"]))
        for key in ("address", "address_basis", "opening_hours", "website", "phone"):
            point[key] = place.get(key) if place else None
    # Returns a plain array so the map can iterate it directly.
    return points


@app.post("/ask")
def ask(q: Question, response: Response = None, request: Request = None):
    # Direct Python checks bypass the HTTP cache. Evaluations can explicitly
    # request fresh model output using Cache-Control: no-cache.
    key = None
    if response is not None:
        key = _cache_key(q)
        cached = None if _bypass_cache(request) or key is None else answer_cache.get(key)
        response.headers['X-CivicLens-Cache'] = 'HIT' if cached is not None else 'MISS'
        if cached is not None:
            return cached
    result = _answer_question(q)
    if key is not None:
        answer_cache.put(key, result)
    return result


def _cache_key(q):
    try:
        return answer_cache.key(DATABASE_PATH, MODEL, q.question)
    except OSError:
        return None


def _bypass_cache(request):
    return request is not None and 'no-cache' in request.headers.get('cache-control', '').lower()


@app.post("/ask/stream")
def ask_stream(q: Question, request: Request = None):
    """The same answer as /ask, sent as newline-delimited JSON.

    While the answer is worked out, one line per step ({"stage": "query"},
    then "database", "answer", and "rewrite" if a draft failed the number
    check), so the page can say what is happening. The last line is
    {"result": <exactly what /ask returns>} or {"error": "..."}.
    """
    key = _cache_key(q)
    cached = None if _bypass_cache(request) or key is None else answer_cache.get(key)
    events = queue.Queue()

    def work():
        progress_listener.set(lambda stage: events.put({"stage": stage}))
        try:
            result = _answer_question(q)
            if key is not None:
                answer_cache.put(key, result)
            events.put({"result": result})
        except Exception as exc:  # the page shows its own message
            print(f"CIVICLENS_STREAM_ERROR {type(exc).__name__}: {exc}", flush=True)
            events.put({"error": "The server could not answer this question right now."})
        finally:
            events.put(None)

    if cached is not None:
        events.put({"result": cached})
        events.put(None)
    else:
        threading.Thread(target=copy_context().run, args=(work,), daemon=True).start()

    def lines():
        while (event := events.get()) is not None:
            yield json.dumps(jsonable_encoder(event)) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        "X-CivicLens-Cache": "HIT" if cached is not None else "MISS"})


def _answer_question(q: Question):
    """Plain-English question -> real answer, with the SQL and sources shown."""
    question = q.question.strip()
    if not question:
        return _cannot("Please ask a question.")

    # 1. Claude writes the query. It never writes the numbers.
    # The database description is the same on every request, so it leads the
    # prompt and is cached; only the question part changes.
    schema_prefix = (
        f"{SCHEMA_DESCRIPTION}\n\n{schema_context(DATABASE_PATH)}\n\n{demographic_context.schema_context(DATABASE_PATH)}\n\n"
        f"{insight_context.schema_context(DATABASE_PATH)}\n\n"
    )
    sql_prompt = (
        schema_prefix +
        f"User question: {question}\n\n"
        "Write the SQLite SELECT query that answers it, plus up to 3 evidence queries when the "
        "guidance above calls for them, each after its own '-- evidence: <label>' line. Output only "
        "the SQL, no explanation and no markdown fences. If the question cannot be "
        "answered from these tables, output exactly: CANNOT_ANSWER"
    )
    _progress("query")
    try:
        sql = _ask_claude(sql_prompt, max_tokens=SQL_MAX_TOKENS, effort=SQL_EFFORT,
                          cache_prefix=schema_prefix, stage="query_model")
    except ModelNotConfigured:
        return _cannot("AI answers are not configured. The data is available, but an administrator must configure the model key before questions can run.")
    except ModelTruncated:
        _log_gap(question, "model_truncated")
        return _cannot("That question needed a longer query than I can write in one go. Try asking for one thing at a time.")
    except anthropic.APIConnectionError as exc:
        _log_gap(question, "model_error", type(exc).__name__)
        return _cannot("Could not reach the language model. Please try again in a moment.")
    except Exception as exc:
        _log_gap(question, "model_error", f"{type(exc).__name__}: {exc}")
        return _cannot("The language model did not return a usable answer. Please try again or rephrase the question.")

    if "CANNOT_ANSWER" in sql.upper():
        # The model judged that no installed table can answer this. This is
        # the clearest sign of a dataset worth adding.
        _log_gap(question, "no_data_for_question")
        return _cannot()

    # Several queries may come back: the main one first, then evidence
    # queries for the other factors a decision needs. Each is validated and
    # run exactly like the main one; the model never sees their numbers
    # until the database has produced them.
    sql, evidence_blocks = _evidence_blocks(sql)

    # 2. Validate and run it read-only against the real database.
    _progress("database")
    for attempt in range(2):
        try:
            rows, executed_sql = _run_query(sql)
            break
        except UnsafeQuery as exc:
            _log_gap(question, "unsafe_query", str(exc), sql)
            return _cannot("I couldn't safely prepare an answer. Please rephrase the question; this does not mean the data is missing.", sql=sql)
        except sqlite3.Error as exc:
            # A query that SQLite rejects (e.g. ORDER BY a name a UNION does
            # not have) gets one repair: the model sees the error and fixes
            # it. A second failure is reported as before.
            _log_gap(question, "query_error" if attempt else "query_error_repairing", str(exc), sql)
            if attempt:
                return _cannot("I couldn't retrieve the requested information. Please try a more specific question; this does not establish a data gap.", sql=sql)
            _progress("repair")
            try:
                sql = _ask_claude(sql_prompt + f"\n\nThis query failed in SQLite with the error "
                                  f"\"{exc}\":\n{sql}\nWrite a corrected query. Output only the SQL.",
                                  max_tokens=SQL_MAX_TOKENS, effort=SQL_EFFORT,
                                  cache_prefix=schema_prefix, stage="query_model")
            except Exception as repair_exc:
                _log_gap(question, "model_error", type(repair_exc).__name__, sql)
                return _cannot("I couldn't retrieve the requested information. Please try a more specific question; this does not establish a data gap.", sql=sql)
            if "CANNOT_ANSWER" in sql.upper():
                _log_gap(question, "no_data_for_question")
                return _cannot()
            sql, repaired_evidence = _evidence_blocks(sql)
            evidence_blocks = evidence_blocks or repaired_evidence

    if not rows:
        _log_gap(question, "no_rows", None, executed_sql)
        return _cannot("No data matched that question.", sql=sql)

    rows = _neutral_union_columns(rows, executed_sql)
    # Each listed place gets its street address and map location from
    # place_locations, in code: the model never selects or joins addresses.
    rows, locations = _attach_places(rows, executed_sql)

    evidence = _run_evidence(question, evidence_blocks)
    evidence_sql = "\n".join(item["sql"] for item in evidence)
    notes = _answer_notes(executed_sql, rows, question)
    for item in evidence:
        notes += [note for note in _answer_notes(item["sql"], item["rows"], question) if note not in notes]
    # Tell the user plainly when the list was cut off. It goes first so the
    # page always shows it right under the answer.
    hit_limit = _result_limit(rows, executed_sql, question=question)
    if hit_limit:
        notes = [limited_results_note(hit_limit)] + notes

    # Pass display precision to the model while keeping the raw query values.
    # A yearly series is short and its last years are the point, so the
    # model sees all of it rather than the first 25 rows.
    trend = _trend_visual(executed_sql, rows)
    display_rows = []
    for row in rows[:60 if trend else 25]:
        display_row = dict(row)
        for column, value in row.items():
            if isinstance(value, float):
                if _is_percentage(column):
                    display_row[column] = round(value, 1)
                elif "income" in column:
                    display_row[column] = round(value)
        display_rows.append(display_row)
    display_rows = _with_area_names(display_rows)
    evidence_display = [dict(item, display=_with_area_names([_display_precision(row) for row in item["rows"][:30]]))
                        for item in evidence]
    # Every row the answer may cite a number from: the main query and the evidence.
    citable_rows = display_rows + [row for item in evidence_display for row in item["display"]]
    citable_sql = executed_sql + ("\n" + evidence_sql if evidence_sql else "")
    listing_counts = _grouped_business_counts(question, executed_sql, rows)
    summary = None
    next_step = None
    calculations = []
    if listing_counts and not evidence:
        items = [f"{count:,} {label} listings" for label, count in listing_counts]
        totals = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
        summary = f"OpenStreetMap shows {totals} in the selected area."
    if summary is None and not evidence:
        summary = _place_count(question, executed_sql, rows)
    if summary is None and not evidence:
        summary = _demographic_count(question, executed_sql, rows)
    if summary is None and not evidence:
        summary = _single_listing_count(question, executed_sql, rows)
    if summary is None:
        answer_prompt = (
            f"Question: {question}\n\n"
            f"These rows came back from the database:\n"
            f"{json.dumps(display_rows, indent=2)}\n\n"
            + "".join(f"Supporting evidence, {item['label']} (another query on the same database):\n"
                      f"{json.dumps(item['display'], indent=2)}\n\n" for item in evidence_display)
            + f"Notes displayed separately: {' '.join(notes)}\n\n"
            + (f"A line chart of these yearly counts is shown under your answer. {trend['note'] or ''} "
               "Never call a change into or out of a year that is not full a rise or a fall; compare "
               "full years. "
               if trend else "")
            + ("Every listed place is shown below your answer in a list with its street address, and "
               "clicking it shows it on the map. Do not repeat addresses in your answer: name a few "
               "places if useful, and say their addresses are in the list below and they are ringed on the map. "
               if locations and sum(1 for place in locations if place) <= MAX_LISTED_PLACES else "") +
            "Never state latitude or longitude coordinates. Name an area only by its 'area' "
            "value, written exactly (e.g. 'Northwest Park / Oakview'); never write 'Census tract', 'tract' or a "
            "tract number. "
            + ("Opening hours are OpenStreetMap text as mapped: hours with no day names mean every "
               "day; a missing value means the hours are not mapped (unknown), never that the place "
               "is closed. Say how many places have no hours mapped, using the exact count given "
               "below, instead of leaving them out; never count them yourself. Say hours should "
               "be checked before visiting. "
               if any("opening_hours" in key.lower() for key in rows[0]) else
               "Do not mention opening hours. ") +
            "Never write an age range or any other number that is not a value in the rows "
            "(say 'employed residents', not 'residents aged 16+'). "
            "Lead with the direct answer in everyday language. If the question asks whether data "
            "exists on something specific, answer that first, whatever order the rows are in. "
            "Write category codes as plain words (sports_centre is 'sports centre') and large "
            "numbers with thousands separators. Write every count in digits, never as a word "
            "('6', not 'six'). Never count rows yourself, for example how many of the listed "
            "venues are hotels: state such a count only if a row or the exact totals give it, "
            "otherwise name the items without a number. Never repeat a number from the "
            "question: say 'your event', not 'a 50-person event'. For a yes/no question "
            "with a number in it, answer with the returned figure alone: for 'more than 60 "
            "restaurants?' write 'No: N mapped restaurant listings' with N from the rows, never 'fewer than 60' or "
            "'not more than 60'. Write the next step without "
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
            "Do not add a next step to an ordinary lookup. "
            + ("Use plain text, under 120 words in total. Supporting evidence is given, so synthesise it: "
               "first a one-sentence direct answer or verdict; then 2-3 short sentences, each giving one "
               "finding that bears on the decision with its comparison (this area vs Silver Spring, the "
               "county or a peer place), using the evidence; then the most important unknown. When the "
               "evidence points both ways, say which way it leans and why. Do not list every number. "
               "Write it as flowing prose: never label the parts ('Verdict:', 'Finding:', 'Unknown:'). "
               if evidence else "Use plain text, under 55 words total. ") +
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
        if set(_sql_geoids(executed_sql)) == set(SILVER_SPRING_GEOIDS):
            # Both models misread these as "tracts that may not be Silver
            # Spring", or picked the middle tract as the area's median.
            answer_prompt += (" These rows are the areas this app uses for Silver Spring: "
                              "call them areas of Silver Spring, never places that may lie elsewhere.")
            if any("income" in key.lower() for key in rows[0]):
                answer_prompt += (" The data has no single median income for Silver Spring as a whole: "
                                  "give the range across its areas, and never present one area's value, "
                                  "the middle area or any average as Silver Spring's median.")
        # Partial-year dates come from the source catalog, so they may be said.
        trend_numbers = {float(n) for n in re.findall(r"\d+", (trend or {}).get("note") or "")}
        operands = (_returned_numbers(rows) | _returned_numbers(display_rows) | set(total_values)
                    | {n for item in evidence for n in _returned_numbers(item["rows"])})
        last_sections = None
        calculations = []
        for attempt in range(2):
            _progress("answer" if attempt == 0 else "rewrite")
            try:
                response = _ask_claude(answer_prompt, max_tokens=ANSWER_MAX_TOKENS, effort=ANSWER_EFFORT)
            except Exception as exc:
                _log_gap(question, "model_error", type(exc).__name__, executed_sql)
                break
            response, calculations = _apply_calculations(_plain_text(response), operands)
            computed = {c["result"] for c in calculations if "result" in c}
            allowed_extra = set(total_values) | computed | trend_numbers
            sections = re.split(r"\s*(?:\*\*)?\bnext steps?(?:\*\*)?\s*:\s*(?:\*\*)?\s*", response, maxsplit=1, flags=re.IGNORECASE)
            candidate = sections[0].strip()
            last_sections = sections
            unverified = []
            if candidate and _uses_returned_numbers(candidate, citable_rows, citable_sql, allowed_extra, unverified):
                summary = candidate
                if len(sections) == 2:
                    next_step = sections[1].strip() or None
                break
            if attempt == 0:
                named = ", ".join(unverified)
                answer_prompt += (f" Your last answer stated {named or 'a figure'}, which the rows do not "
                                  "support (a number repeated from the question counts too). Rewrite it: use "
                                  "only figures exactly as returned or [[calc: ...]], give each kind's real count "
                                  "instead of 'a', and never say counts suggest foot traffic, demand or interest. "
                                  "Write it as a fresh answer for the user: never mention a previous answer, a "
                                  "rewrite, or what you can or cannot see. "
                                  "Your last answer was: " + response)
        if summary is None and last_sections:
            # Both attempts stated at least one figure the database did not
            # return. Keep only the sentences whose every number checks, so
            # the rule still holds and the user still gets the parts that do.
            summary = _verified_sentences(last_sections[0], citable_rows, citable_sql, allowed_extra)
            dropped = [s for s in _split_sentences(last_sections[0])
                       if not _uses_returned_numbers(s, citable_rows, citable_sql, allowed_extra)]
            _log_gap(question, "trimmed_answer" if summary else "no_verified_answer",
                     {"dropped_sentences": dropped, "calculations": calculations}, executed_sql)
            if summary and len(last_sections) == 2:
                next_step = last_sections[1].strip() or None
        if summary is None:
            summary = "Open the supporting data for the results."
        if next_step and not _uses_returned_numbers(next_step, citable_rows, citable_sql, allowed_extra if last_sections else total_values, claims=False):
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

    cleaned = _no_meta_sentences(summary)
    if cleaned != summary:
        _log_gap(question, "dropped_meta", {"before": summary, "after": cleaned}, executed_sql)
    summary, next_step = cleaned, _no_meta_sentences(next_step)
    summary = _anchor_dangling_start(summary, rows)
    summary, next_step = _no_tract_words(summary), _no_tract_words(next_step)
    notes = [_no_tract_words(note) for note in notes]
    return {
        "answer": " ".join(filter(None, (summary, next_step, *notes))),
        "summary": summary,
        "next_step": next_step,
        "notes": notes,
        "visual": _answer_visual(executed_sql, rows, listing_counts),
        "statistic": _answer_statistic(rows),
        "supporting_data": _supporting_data(_with_area_names(rows), locations),
        "listing_details": _listing_details(executed_sql, rows),
        "highlight_geoids": highlights,
        "highlight_scope": scope,
        "geography": geography,
        "table": _table,
        "table_headers": _headers,
        "rows": rows,
        "sources": _sources(citable_sql, highlights, rows),
        "sql": executed_sql,
        # Other queries the answer drew on, each with its own rows, so every
        # figure in the answer can be found in a table below it.
        "evidence": [{"label": item["label"], "sql": item["sql"], "rows": item["rows"],
                      "supporting_data": _supporting_data(_with_area_names(item["rows"]))} for item in evidence],
        # Every calculation shown in the answer, with the exact expression
        # over returned numbers, so any figure can be re-checked by hand.
        "calculations": calculations,
    }


# ---------------------------------------------------------------- helpers

# The rewrite step shows the model its own failed draft, and it sometimes
# answers the correction instead of the user ("My last answer was wrong...",
# "I can see only the first part"). Those sentences are about us, not the
# data, so they are dropped. Dropping a sentence never adds a number.
_META_SENTENCE = re.compile(
    r"\b(?:my|the|your)\s+(?:last|previous|earlier|first)\s+(?:answer|response|draft)\b"
    r"|\bI\s+(?:can|could)\s+(?:only\s+)?see\b|\bI\s+(?:am|was)\s+(?:only\s+)?shown\b"
    r"|\brewrit(?:e|ten|ing)\b",
    re.I)


_EVIDENCE_MARK = re.compile(r"^\s*--\s*evidence\s*:\s*(.*)$", re.I | re.M)
MAX_EVIDENCE = 3


def _evidence_blocks(text):
    """Split the model's output into the main query and labelled evidence queries.

    Text before the first '-- evidence:' line is the main query. A block
    with no SQL in it is dropped; the guard still checks every query."""
    text = re.sub(r"```(?:sql)?", "", text or "")
    marks = list(_EVIDENCE_MARK.finditer(text))
    if not marks:
        return text.strip(), []
    main = text[:marks[0].start()].strip()
    blocks = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip().rstrip(";").strip()
        if re.match(r"(?is)^(?:select|with)\b", body):
            blocks.append((mark.group(1).strip()[:120] or "Supporting evidence", body))
    if not main and blocks:
        # The model labelled its main query too.
        main = blocks.pop(0)[1]
    return main, blocks[:MAX_EVIDENCE]


def _run_evidence(question, blocks):
    found = []
    for label, block in blocks:
        try:
            rows, executed = _run_query(block)
        except UnsafeQuery as exc:
            _log_gap(question, "unsafe_evidence_query", str(exc), block)
            continue
        except sqlite3.Error as exc:
            _log_gap(question, "evidence_query_error", str(exc), block)
            continue
        if rows:
            found.append({"label": _no_tract_words(label), "sql": executed, "rows": _neutral_union_columns(rows, executed)})
    return found


def _display_precision(row):
    out = dict(row)
    for column, value in row.items():
        if isinstance(value, float):
            if _is_percentage(column):
                out[column] = round(value, 1)
            elif "income" in column or "rent" in column or "value" in column:
                out[column] = round(value)
    return out


_DANGLING_START = re.compile(r"^(?:Those|These|They|Them|That|This|It|Its)\b")


def _anchor_dangling_start(text, rows):
    """Give an answer back the total it refers to when trimming removed it.

    Dropping a sentence (an unverified figure, or rewrite chatter) can leave
    "Those include 7-Eleven, Aldi..." with nothing for "those" to mean. The
    total comes from the query's own total_count, so no new number appears.
    """
    if not text or not _DANGLING_START.match(text):
        return text
    total = rows[0].get("total_count") if rows else None
    if type(total) is int and total > 0:
        listings = any(key in rows[0] for key in ("name", "category", "osm_amenity", "osm_shop", "osm_leisure", "osm_office", "osm_tourism", "osm_craft"))
        return f"The data returned {total:,} {'matching mapped listings' if listings else 'matching results'}. {text}"
    # No total to anchor it: the opening sentence points at a figure that was
    # trimmed ("That makes it 1.8 times..."), so it is dropped if others remain.
    sentences = _split_sentences(text)
    return " ".join(sentences[1:]) if len(sentences) > 1 else text


def _no_meta_sentences(text):
    if not text:
        return text
    kept = [s for s in _split_sentences(text) if not _META_SENTENCE.search(s)]
    return " ".join(kept) or text

_PLACE_NAME_CACHE = {}


def _place_names_by_geoid():
    """geoid -> the names people actually use for it, districts first."""
    if _PLACE_NAME_CACHE:
        return _PLACE_NAME_CACHE
    try:
        conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
        for geoid, name in conn.execute(
            """SELECT geoid, name FROM places WHERE kind != 'planning area'
               ORDER BY CASE WHEN kind = 'district' THEN 0 ELSE 1 END, name"""
        ):
            _PLACE_NAME_CACHE.setdefault(geoid, []).append(name)
        conn.close()
    except sqlite3.Error:
        pass
    return _PLACE_NAME_CACHE


_AREA_LABELS = {}
_TRACT_NAME = re.compile(r"^\s*census tract\s+(\d+(?:\.\d+)?)\b", re.I)


def _area_labels():
    """geoid -> what to call a Census tract on screen. Never "Census Tract 7016.02".

    1. The neighbourhoods inside it, districts first ("Northwest Park / Oakview").
    2. Else the closest named place, if within 1 km ("near Oakview").
    3. Else the town most of its mapped business addresses give, with the most
       common street so two areas of one town differ ("Gaithersburg, around
       Quince Orchard Road").
    4. Else "near" the town of the closest area that has one.
    """
    if _AREA_LABELS:
        return _AREA_LABELS
    try:
        with closing(sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)) as conn:
            tracts = conn.execute("SELECT geoid, centroid_latitude, centroid_longitude FROM tract_data").fetchall()
            points = conn.execute("SELECT name, latitude, longitude FROM places "
                                  "WHERE latitude IS NOT NULL AND longitude IS NOT NULL").fetchall()
            addresses = conn.execute(
                "SELECT b.geoid, pl.address FROM businesses b JOIN place_locations pl "
                "ON pl.source_url = b.source_url WHERE b.geoid IS NOT NULL AND pl.address LIKE '%, MD%'").fetchall()
    except sqlite3.Error:
        return _AREA_LABELS

    def km(lat1, lon1, lat2, lon2):
        return math.hypot((lat1 - lat2) * 111.0, (lon1 - lon2) * 111.0 * math.cos(math.radians(lat1)))

    towns, streets = {}, {}
    for geoid, address in addresses:
        parts = [part.strip() for part in address.split(",")]
        town = next((parts[i - 1] for i, part in enumerate(parts) if i and re.match(r"MD\b", part)), None)
        street = re.sub(r"^\d+[A-Za-z-]*\s+", "", parts[0]).replace("_", " ")
        if town:
            towns.setdefault(geoid, Counter())[town] += 1
        if street and street != parts[0]:
            streets.setdefault(geoid, Counter())[street] += 1

    names = _place_names_by_geoid()
    labels = {}
    for geoid, lat, lon in tracts:
        if names.get(geoid):
            # Slashes keep one area one unit in a list of several areas.
            labels[geoid] = " / ".join(list(dict.fromkeys(names[geoid]))[:3])
            continue
        if lat is not None and points:
            name, plat, plon = min(points, key=lambda p: km(lat, lon, p[1], p[2]))
            if km(lat, lon, plat, plon) <= 1.0:
                labels[geoid] = f"near {name}"
                continue
        if towns.get(geoid):
            town = towns[geoid].most_common(1)[0][0]
            street = streets.get(geoid) and streets[geoid].most_common(1)[0][0]
            labels[geoid] = f"{town}, around {street}" if street else f"{town} area"
    for geoid, lat, lon in tracts:
        if geoid not in labels and lat is not None:
            known = [(g, la, lo) for g, la, lo in tracts if towns.get(g) and la is not None]
            if known:
                g = min(known, key=lambda t: km(lat, lon, t[1], t[2]))[0]
                labels[geoid] = f"near {towns[g].most_common(1)[0][0]}"
    # Long roads cross several areas ("Silver Spring, around Georgia
    # Avenue"). Tell repeats apart by their second street, then by direction
    # from the middle of the areas that share the name.
    centres = {g: (la, lo) for g, la, lo in tracts if la is not None}
    for attempt in ("street", "direction"):
        groups = {}
        for geoid, label in labels.items():
            groups.setdefault(label, []).append(geoid)
        for label, geoids in groups.items():
            if len(geoids) < 2:
                continue
            for geoid in geoids:
                if attempt == "street" and ", around " in label and len(streets.get(geoid, ())) > 1:
                    labels[geoid] = f"{label} and {streets[geoid].most_common(2)[1][0]}"
                elif attempt == "direction" and geoid in centres:
                    mid_lat = sum(centres[g][0] for g in geoids if g in centres) / len(geoids)
                    mid_lon = sum(centres[g][1] for g in geoids if g in centres) / len(geoids)
                    north = (centres[geoid][0] - mid_lat) * 111.0
                    east = (centres[geoid][1] - mid_lon) * 111.0 * math.cos(math.radians(mid_lat))
                    heading = ["east", "north-east", "north", "north-west", "west",
                               "south-west", "south", "south-east"][round(math.degrees(math.atan2(north, east)) / 45) % 8]
                    labels[geoid] = f"{label} ({heading})"
    _AREA_LABELS.update(labels)
    return _AREA_LABELS


def _tract_geoids_by_name():
    """"7016.02" -> geoid, for rows that carry a tract name but no geoid."""
    return {str(int(geoid[5:9])) + ("." + geoid[9:] if geoid[9:] != "00" else ""): geoid
            for geoid in _area_labels()}


def _with_area_names(rows):
    """Rows as the model sees them: each tract named by its area, no tract numbers.

    "Census Tract 7016.02; Montgomery County; Maryland" becomes "Northwest Park
    / Oakview", so the answer can only name the area. A row with a geoid
    but no tract name gets an "area" value instead.
    """
    labels, by_number = _area_labels(), _tract_geoids_by_name()
    out = []
    for row in rows:
        new, named = {}, False
        for key, value in row.items():
            if key in ("tract_code", "source_tract_id"):
                continue
            match = _TRACT_NAME.match(value) if isinstance(value, str) else None
            if match:
                number = match.group(1)
                # A 2010 tract ("7016") was later split ("7016.01", "7016.02"):
                # name it by the first current area it became.
                parts = [g for n, g in sorted(by_number.items()) if n.startswith(number + ".")]
                geoid = (str(row.get("geoid") or "") or by_number.get(number)
                         or next((g for g in parts if not labels.get(g, "near").startswith("near")), "")
                         or next(iter(parts), ""))
                if labels.get(geoid):
                    new["area" if key == "tract_name" else key] = labels[geoid]
                    named = True
                    continue
            new[key] = value
        geoid = str(row.get("geoid") or "")
        if not named and labels.get(geoid):
            new = {"area": labels[geoid], **new}
        out.append(new)
    return out


_TRACT_WORDS = [(re.compile(r"\bcensus tracts\b", re.I), "Census areas"),
                (re.compile(r"\bcensus tract\b", re.I), "Census area"),
                (re.compile(r"\btracts\b"), "areas"), (re.compile(r"\btract\b"), "area"),
                (re.compile(r"\bTracts\b"), "Areas"), (re.compile(r"\bTract\b"), "Area")]


def _no_tract_words(text):
    """Last line of defence: an answer never says "tract" to a reader."""
    if not text:
        return text
    for pattern, plain in _TRACT_WORDS:
        text = pattern.sub(plain, text)
    return text


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
        area = 'the area used to approximate ' + place if place else 'the selected areas'
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
            'label': 'Whole Census areas', 'approximate': True}


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
        filters = re.findall(r"\b(?:osm_amenity|osm_shop|osm_leisure|osm_tourism|osm_office|osm_craft|category)\s*=\s*'([a-z_]+)'", sql, re.I)
        label = filters[0].replace('_', ' ') if len(set(filters)) == 1 else 'business'
    with sqlite3.connect(f'file:{DATABASE_PATH}?mode=ro', uri=True) as con:
        names = [r[0] for r in con.execute('SELECT DISTINCT name FROM places ORDER BY LENGTH(name) DESC')]
    name = next((n for n in names if n.lower() in question.lower()), None)
    area = f'the area used to approximate {name}' if name else 'the selected areas'
    return f'There are {count:,} mapped {label} listings in {area}.'


# The page lists an answer's places only up to this many (page/script.js
# MAX_ANSWER_PLACES); past it the answer must not point at a list.
MAX_LISTED_PLACES = 60


def _listing_details(sql, rows=None):
    """Expose the records behind a simple count, using the count's own filter.

    "How many restaurants are in Fenton Village?" returns one number. The
    same WHERE clause, run again, lists the places that number counted, so
    the page can name them and ring them on the map. Only a single,
    ungrouped COUNT(*) over businesses or venues qualifies: anything else
    (COUNT(DISTINCT), GROUP BY, JOIN, UNION, several counts) could list a
    different set than the figure on screen.
    """
    text = sql.strip().rstrip(';')
    if (len(re.findall(r'\bCOUNT\s*\(', text, re.I)) != 1
            or not re.search(r'\bCOUNT\s*\(\s*\*\s*\)', text, re.I)
            or not re.match(r'^SELECT\b', text, re.I)
            or re.search(r'\bCOUNT\s*\(\s*DISTINCT\b', text, re.I)
            or re.search(r'\b(?:join|union|group\s+by|having)\b', text, re.I)):
        return None
    match = re.search(r'\bFROM\s+(businesses|community_venues)(?:\s+(?:AS\s+)?(?!WHERE\b)(\w+))?\s+WHERE\s+(.+)',
                      text, re.I | re.S)
    if not match or len(re.findall(r'\bFROM\b', text[:match.start()], re.I)):
        return None
    table, alias = match[1].lower(), match[2] or ''
    where = re.sub(r'\s+(?:ORDER\s+BY|LIMIT)\b.*$', '', match[3], flags=re.I | re.S)
    extra = ', website AS venue_website' if table == 'community_venues' else ''
    try:
        records, _ = _run_query(f'SELECT name, category, geoid, source_url, latitude, longitude{extra} FROM {table} {alias} WHERE ' + where, max_rows=201)
    except (UnsafeQuery, sqlite3.Error):
        return None
    # The list must be the set the answer counted. If the returned count
    # and the re-run disagree, show nothing rather than a different set.
    counts = [value for key, value in (rows[0].items() if rows and len(rows) == 1 else ())
              if type(value) is int and (key == 'count' or key.endswith('_count'))]
    if len(counts) == 1 and len(records) <= 200 and len(records) != counts[0]:
        return None
    lookup = _place_lookup(record.get('source_url') for record in records[:200])
    for record in records[:200]:
        place = lookup.get(_osm_ref(record.get('source_url')))
        for key in ('address', 'address_basis', 'opening_hours', 'website', 'phone'):
            record[key] = place.get(key) if place else None
        venue_website = record.pop('venue_website', None)
        if not record.get('website') and venue_website:
            record['website'] = venue_website
    return {'rows': records[:200], 'truncated': len(records) > 200}


def _is_percentage(column):
    return any(word in column.lower() for word in ("pct", "percent", "share"))


_HIDDEN_FIELDS = {"geoid", "source_url", "state_fips", "county_fips", "tract_code",
                  "latitude", "longitude", "centroid_latitude", "centroid_longitude",
                  "business_id", "geography_type", "duplicate_source_url", "geometry_geojson",
                  "period_start", "period_end", "address_basis",
                  # The same list total on every row; the answer states it once.
                  "total_count"}

_COLUMN_LABELS = {
    "population_count":                    "residents",
    "female_count":                        "female residents (Census sex)",
    "male_count":                          "male residents (Census sex)",
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
    "acs_vintage": "Census survey years",
    "listing_count": "Mapped listings",
    "measure_label": "Measure",
    "estimate": "Estimate",
    "margin_of_error": "Margin of error (±)",
    "transit_pct": "Public transit commute share",
    "transit_commuters": "Public transit commuters",
    "population_margin_of_error": "Margin of error (±)",
    "female_margin_of_error": "Margin of error (±)",
    "male_margin_of_error": "Margin of error (±)",
})


def _column_label(column):
    # Upper-case the first letter only: str.capitalize() would turn "ACS"
    # into "acs" and "OpenStreetMap" into "Openstreetmap".
    label = _COLUMN_LABELS.get(column, re.sub(r"\bpct\b", "%", column.replace("_", " "))).strip()
    return label[:1].upper() + label[1:]


# Query-written labels carry database jargon ("Restaurants (OSM
# amenity=restaurant)", "Silver Spring CDP"). Say it in words instead.
_JARGON = [
    (re.compile(r"\s*\(OSM\s+(?:amenity|shop|leisure|tourism|office|craft)\s*=\s*[\w;|_-]+\)", re.I), " (OpenStreetMap listings)"),
    (re.compile(r"\bOSM\b"), "OpenStreetMap"),
    (re.compile(r"\bCDP\b"), "(official Census boundary)"),
]


def _plain_label(text):
    for pattern, replacement in _JARGON:
        text = pattern.sub(replacement, text)
    return text


def _display_value(column, value):
    if value is None:
        return "Not available"
    if type(value) not in (int, float):
        text = str(value)
        if column in {'metric', 'label'}:
            text = re.sub(r'\s*\(foot[- ]traffic proxy\)', '', text, flags=re.I).replace('_', ' ')
            text = ', '.join(reversed(text.split(' > ')))
        if column in {'metric', 'label', 'measure', 'measure_label'}:
            text = _plain_label(text)
        return text if len(text) <= 180 else text[:180] + '… (full text below)'
    if _is_percentage(column):
        return f"{value:.1f}%"
    if "income" in column:
        return f"${value:,.0f}"
    return f"{value:,.1f}" if isinstance(value, float) else f"{value:,}"


def _supporting_data(rows, locations=None):
    columns = list(dict.fromkeys(key for row in rows for key in row
                                 if key not in _HIDDEN_FIELDS and "geoid" not in key.lower()))

    def shown(column, value):
        value = _display_value(column, value)
        # Labels the query wrote ("Bookstores in Fenton Village mapped tracts").
        # Names, addresses, hours and links are left exactly as published.
        own_text = re.search(r"name|address|street|hours|website|phone|url|cuisine", column, re.I)
        return _no_tract_words(value) if isinstance(value, str) and not own_text else value

    data = {
        "columns": [_no_tract_words(_column_label(column)) for column in columns],
        "rows": [[shown(column, row.get(column)) for column in columns] for row in rows],
    }
    # One entry per row, aligned with "rows": where that place is, so the page
    # can show it on the map. None for rows that are not a single place.
    if locations and any(locations):
        data["locations"] = locations
    return data


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
    return {'value': value, 'label': _no_tract_words(_display_value('label', rows[0]['label']) if key == 'estimate' and rows[0].get('label') else _column_label(key))}


_YEAR_COLUMN = re.compile(r"(?:^|_)(?:year|yr)$", re.I)
_DATE_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _year_of(value):
    if type(value) is int:
        year = value
    elif isinstance(value, str) and re.fullmatch(r"\d{4}", value.strip()):
        year = int(value)
    else:
        return None
    return year if 1900 <= year <= 2100 else None


def _short_date(text):
    year, month, day = (int(part) for part in text[:10].split("-"))
    return f"{_MONTHS[month - 1]} {day}, {year}"


def _partial_years(sql):
    """Years a yearly count only partly covers, and why, from the source's own
    first/last record dates and any date bounds written into the query.

    A year that only has April-December, or January-November, looks like a
    drop on a chart. Every date here comes from the database or the SQL."""
    partial = {}
    for source in used_sources(DATABASE_PATH, sql):
        first, last = source.get("first_record"), source.get("last_record")
        if first and _DATE_TEXT.match(first) and first[5:7] != "01":
            partial.setdefault(int(first[:4]), f"records start {_short_date(first)}")
        if last and _DATE_TEXT.match(last) and last[5:7] != "12":
            partial.setdefault(int(last[:4]), f"records end {_short_date(last)}")
    for op, date in re.findall(r"(>=|<=|>|<)\s*'(\d{4}-\d{2}-\d{2})'", sql or ""):
        month_day = date[5:]
        if op == ">=" and month_day != "01-01" or op == ">" and month_day != "12-31":
            partial.setdefault(int(date[:4]), f"the question's range starts {_short_date(date)}")
        if op == "<=" and month_day != "12-31" or op == "<" and month_day != "01-01":
            partial.setdefault(int(date[:4]), f"the question's range ends {_short_date(date)}")
    return partial


def _trend_visual(sql, rows):
    """One count per calendar year -> a line chart.

    Only drawn for a single series: a year column, one count, and each year
    once (a repeated year means several lines mixed together).
    The points are the returned rows themselves; nothing is computed."""
    if not rows or len(rows) > 60 or "tract_history" in (sql or "").lower():
        return None
    columns = [key for key in rows[0] if key not in _HIDDEN_FIELDS and "geoid" not in key.lower()]
    year_columns = [key for key in columns if _YEAR_COLUMN.search(key)]
    if len(year_columns) != 1:
        return None
    year_column = year_columns[0]

    def numeric(value):
        return type(value) in (int, float) and math.isfinite(value)

    measures = [key for key in columns if key != year_column
                and all(numeric(row.get(key)) or row.get(key) is None for row in rows)
                and any(numeric(row.get(key)) for row in rows)]
    counts = [key for key in measures if "count" in key.lower()]
    if counts:
        measure = counts[0]
    elif len(measures) == 1:
        measure = measures[0]
    else:
        return None
    if _is_percentage(measure) or "income" in measure.lower():
        return None

    # Several series (thefts and assaults by year) repeat each year, which
    # the duplicate check below rejects. A text column that only annotates
    # each year ("partial year") is fine.
    points = {}
    for row in rows:
        year, value = _year_of(row.get(year_column)), row.get(measure)
        if year is None or not numeric(value):
            continue          # undated rows; the answer notes already say so
        if year in points or value < 0:
            return None
        points[year] = value
    if len(points) < 3:
        return None

    partial = _partial_years(sql)
    label = _no_tract_words(_column_label(measure))
    shown = sorted(points)
    partial_shown = [year for year in shown if year in partial]
    note = " ".join(f"{year} is not a full year: {partial[year]}." for year in partial_shown) or None
    return {
        "kind": "line",
        "title": f"{label} by year",
        "measure": label,
        "scale_max": max(points.values()),
        "points": [{"label": str(year), "value": points[year], "display": _display_value(measure, points[year]),
                    "partial": year in partial} for year in shown],
        "note": note,
    }


def _answer_visual(sql, rows, listing_counts):
    trend = _trend_visual(sql, rows)
    if trend:
        return trend
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
        "title": _no_tract_words(_column_label(measure)) + " by Census period",
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
            if k in _HIDDEN_FIELDS or "geoid" in k.lower():
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
        if label is None or "census tract" in label.lower():
            match = _TRACT_NAME.match(label or "")
            geoid = (str(next((v for k, v in r.items() if "geoid" in k.lower() and v), "") or "")
                     or (match and _tract_geoids_by_name().get(match.group(1))) or "")
            label = _area_labels().get(geoid) or label

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
        out.append({"label": _no_tract_words(label), "value": value})

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
            notes.append('Some selected areas have missing estimates; the returned figure is incomplete and is not a full-area total.')
        notes.append('Race and Hispanic-origin estimates use ACS 2020-2024. Race-alone categories include Hispanic residents unless an origin is specified; Hispanic origin overlaps race.')
        if demographic_context.is_place(sql, rows):
            notes.append('The Census estimate uses Silver Spring’s official Census boundary.')
        elif _place_geoids(question):
            notes.append('The mapped areas approximate the named neighborhood or district.')
        else:
            notes.append('Race estimates describe the selected areas, not a precise neighborhood boundary.')
    if "place_data" in query or industry_place:
        notes.append("The Census estimate uses Silver Spring’s official Census boundary.")
    if re.search(r"\b(?:female|male)_count\b", query):
        notes.append("The Census estimate describes sex and does not measure self-identified gender.")
    if "industry_employment" in query:
        notes.append("The industry estimate describes employed residents and does not count jobs located here.")
    if re.search(r"\b(?:tract_data|tract_history|businesses|industry_employment)\b", query):
        if _place_geoids(question) and ("places" in query or "businesses" in query):
            notes.append("The mapped areas approximate the named neighborhood or district.")
        elif "place_data" not in query and not industry_place and not re.search(r"\bcounty\b", question.lower()):
            notes.append("This result uses whole Census areas, so its boundary is approximate.")
    if "businesses" in query:
        notes.append("OpenStreetMap listings may be incomplete or out of date.")
    if re.search(r'foot[- ]traffic|demand|proxy', json.dumps(rows), re.I) and 'businesses' in query:
        notes.append('Business listing counts do not measure foot traffic or customer demand.')
    group = crime_group(sql)
    if group:
        notes.append(CRIME_GROUPS[group]["note"])
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
    hours = [key for key in dict.fromkeys(k for row in rows for k in row) if "opening_hours" in key.lower()]
    if rows and not cut_off and hours and not re.search(r"\bgroup\s+by\b", sql or "", re.I):
        unmapped = sum(1 for row in rows if not any(str(row.get(key) or "").strip() for key in hours))
        facts.append(f"{unmapped:,} of the {len(rows):,} listed places have no opening hours mapped "
                     "(unknown, not closed).")
        values |= {float(unmapped), float(len(rows))}
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


SCALE_FACTORS = {100.0, 1000.0, 10000.0, 100000.0}
# "per 1,000 residents" names a unit, not a figure.
_PER_SCALE = re.compile(r"\bper\s+(?:1,000|10,000|100,000|1000|10000|100000)\b", re.I)


def _apply_calculations(text, operands):
    """Replace each [[calc: expr]] with its exact result, computed here.

    The model chooses WHAT to calculate; code does the arithmetic. Every
    operand must be a number the database returned (or an exact total
    computed from those rows). The only other constants allowed are scale
    factors: 100 for percentages and 1,000 / 10,000 / 100,000 for rates
    per resident ("cafes per 1,000 residents"). Anything else (an invented operand, a function call,
    division by zero) becomes CALC_ERROR, which the number guard rejects,
    so its sentence is dropped rather than shown.
    Returns (new_text, calculations) where each calculation records the
    expression, the result, or the reason it was refused.
    """
    import ast
    import operator
    ops = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv}
    allowed = set(operands) | SCALE_FACTORS
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


def _plain_text(text):
    """Strip markdown the page would show literally: **Yes** -> Yes, # headings, bullets."""
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    return re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*•]\s+)", "", text)


_OSM_REF = re.compile(r"(?:^|openstreetmap\.org/)((?:node|way|relation)/\d+)$")
# A query that groups places, or keeps one sample link per group, returns rows
# that each stand for many places: no single address is true for them.
_GROUPED_PLACES = re.compile(r"\bGROUP\s+BY\b|\b(?:MIN|MAX|GROUP_CONCAT)\s*\(\s*(?:DISTINCT\s+)?"
                             r"(?:\w+\.)?(?:source_url|osm_id|name)\b", re.I)


def _osm_ref(value):
    match = _OSM_REF.search(str(value or "").strip())
    return match.group(1) if match else None


def _place_lookup(urls):
    """place_locations rows for these OSM links or ids, keyed by osm_ref."""
    refs = sorted({ref for ref in map(_osm_ref, urls) if ref})
    found = {}
    if not refs:
        return found
    try:
        with closing(sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            for start in range(0, len(refs), 500):
                chunk = refs[start:start + 500]
                for row in con.execute("SELECT * FROM place_locations WHERE osm_ref IN (%s)" % ",".join("?" * len(chunk)), chunk):
                    found[row["osm_ref"]] = dict(row)
    except sqlite3.Error:
        return {}  # An older database without place_locations: answer without addresses.
    return found


def _attach_places(rows, sql):
    """Add each listed place's street address and map location, in code.

    Only when every row is one place (no grouping, no sample link per
    group). The address comes from place_locations (OpenStreetMap: the
    place's own tags, or the building it sits inside); a place with no
    mapped address keeps None and is still shown on the map by its
    coordinates. Null placeholder columns the query invented are dropped.
    Returns (rows, locations) with locations aligned to rows.
    """
    if not rows or _GROUPED_PLACES.search(sql or ""):
        return rows, None

    def ref_of(row):
        return next(filter(None, (_osm_ref(row.get(key)) for key in ("source_url", "duplicate_source_url", "osm_id"))), None)

    lookup = _place_lookup(ref_of(row) for row in rows)
    enriched, locations = [], []
    for row in rows:
        new = {key: value for key, value in row.items() if "placeholder" not in key.lower()}
        place = lookup.get(ref_of(row))
        if place:
            for key in [k for k in new if "address" in k.lower() and new[k] in (None, "")]:
                del new[key]
            new["address"] = place["address"] or new.get("address")
            new["address_basis"] = place["address_basis"] if place["address"] else None
        latitude = row.get("latitude") if isinstance(row.get("latitude"), (int, float)) else (place or {}).get("latitude")
        longitude = row.get("longitude") if isinstance(row.get("longitude"), (int, float)) else (place or {}).get("longitude")
        name = next((v for k, v in row.items() if "name" in k.lower() and isinstance(v, str) and v.strip()), None)
        located = isinstance(latitude, (int, float)) and isinstance(longitude, (int, float))
        locations.append({
            "name": name or (place or {}).get("name"),
            "address": new.get("address"),
            "address_basis": new.get("address_basis"),
            "latitude": latitude, "longitude": longitude,
            "source_url": "https://www.openstreetmap.org/" + ref_of(row) if ref_of(row) else None,
            **{key: (place or {}).get(key) for key in ("opening_hours", "website", "phone")},
        } if located else None)
        enriched.append(new)
    return enriched, (locations if any(locations) else None)


def _neutral_union_columns(rows, sql):
    """Rename a measure column that holds different measures to 'value'.

    A UNION ALL takes its column names from its first part, so venue counts
    stacked under a retail-employment row come back named
    'employed_residents', and 496 retail workers under bookstores come back
    named 'listing_count'. When the parts read different tables, a numeric
    column filled in every row is a mixed measure: name it 'value' so the
    model and the table on screen don't mislabel it, and so code never adds
    the rows up (2 bookstores + 496 residents is not a total of anything).
    """
    parts = re.split(r"\bUNION\s+ALL\b", sql or "", flags=re.I)
    tables = {match.group(1).lower() for part in parts
              for match in [re.search(r"\bFROM\s+(\w+)", part, re.I)] if match}
    if len(parts) < 2 or len(tables) < 2 or not rows:
        return rows
    mixed = [key for key in rows[0]
             if key != "value" and "url" not in key
             and all(type(row.get(key)) in (int, float) for row in rows)]
    if len(mixed) != 1 or "value" in rows[0]:
        return rows
    return [{("value" if key == mixed[0] else key): v for key, v in row.items()} for row in rows]


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


_DEFINITIONS = None


def _definition_numbers(sql):
    """Digits in the names and Census labels of real columns and measures the SQL uses."""
    global _DEFINITIONS
    if _DEFINITIONS is None:
        _DEFINITIONS = {}
        try:
            with closing(sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)) as con:
                tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                for table in tables:
                    for column in con.execute(f'PRAGMA table_info("{table}")'):
                        if re.search(r"\d", column[1]):
                            _DEFINITIONS[column[1]] = column[1]
                if "acs_measure_labels" in tables:
                    for measure, label in con.execute("SELECT measure, label FROM acs_measure_labels WHERE measure NOT LIKE '%:%'"):
                        _DEFINITIONS[measure] = f"{measure} {label}"
        except sqlite3.Error:
            pass
    found = set()
    for name, text in _DEFINITIONS.items():
        if re.search(rf"\b{re.escape(name)}\b", sql or ""):
            found.update(float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text.replace("_", " ")))
    return found


def _uses_returned_numbers(answer, rows, sql=None, extra=(), missing=None, claims=True):
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
        if missing is not None:
            missing.append("a [[calc: ...]] that used a number not in the rows (use only returned numbers)")
        return False
    allowed = {float(value) for row in rows for value in row.values() if type(value) in (float, int)}
    allowed.update(extra)
    if sql:
        for literal in re.findall(r"'([^']*)'", sql):
            allowed.update(float(year) for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?=-\d{2}(?:-\d{2})?(?!\d))", literal))
            # ZIP codes the query filtered on name its scope, like a year.
            allowed.update(float(zip_code) for zip_code in re.findall(r"^(20[89]\d{2})$", literal.strip()))
        # Numbers that define a measure the query used ("children under 5",
        # "65 and over", "30% or more of income on rent"): they come from the
        # schema and the Census labels, not from the model.
        allowed.update(_definition_numbers(sql))
        # A year the query filtered on as a number (year = 2023, IN (2018, 2023)).
        allowed.update(float(year) for year in re.findall(
            r"\byear\s*(?:=|>=|<=|>|<|IN\s*\(|BETWEEN)\s*((?:19|20)\d{2})(?:\s*(?:,|AND)\s*((?:19|20)\d{2}))?", sql, re.I)
            for year in year if year)
    for row in rows:
        for key, value in row.items():
            # Dates, source capacity text and other returned descriptive fields
            # are evidence too. Exclude URLs/opaque IDs as numerical claims.
            if isinstance(value, str) and key not in {"source_url", "duplicate_source_url", "geoid", "source_tract_id", "business_id", "osm_id", "incident_id"}:
                allowed.update(numbers(value))
    unsupported = numbers(_PER_SCALE.sub("per", answer)) - allowed
    problems = [_format_calculated(n) for n in sorted(unsupported)]
    if claims:
        # A next step is advice on what to find out ("ask how busy foot
        # traffic is"), not a claim, so only its numbers are checked.
        problems += _understated_counts(answer, rows) + _unsupported_inferences(answer)
    if missing is not None:
        missing.extend(problems)
    return not problems


def _understated_counts(answer, rows):
    """'a hotel' when a returned row says there are 5 hotels.

    No number appears, so the digit check cannot see it, but "a hotel"
    tells the reader there is one. Each row with one count above 1 and a
    text label gives (kind, count); writing that kind with 'a', 'an',
    'one' or 'a single' is reported.
    """
    found = []
    for row in rows:
        # total_count (COUNT(*) OVER ()) is the size of the whole list, not of
        # this row's kind: a list of 7 venues has one community centre in it.
        counts = [v for k, v in row.items() if type(v) is int and "id" not in k
                  and k != "geoid" and not k.startswith("total")]
        # Judge by value, not column name: a query may put a category in a
        # column it called geoid. Skip only URLs and ID-like digit strings.
        labels = [v for k, v in row.items() if isinstance(v, str) and "url" not in k
                  and not v.startswith("http") and not v.strip().isdigit()]
        if len(counts) != 1 or counts[0] < 2 or not labels:
            continue
        for label in labels:
            kind = re.sub(r"\(.*?\)", "", label.rsplit(":", 1)[-1]).replace("_", " ").strip().lower()
            if len(kind) < 3 or len(kind.split()) > 3:
                continue
            if re.search(r"\b(?:an?|one|a single)\s+" + re.escape(kind) + r"\b", answer, re.I):
                found.append(f"'a {kind}' (the data has {counts[0]:,})")
    return found


_INFERS = re.compile(r"\b(?:suggest\w*|indicat\w*|impl(?:y|ies|ied|ying)|signs? of|points? to|"
                     r"reflect\w*|evidence of|proxy|shows? (?:strong|solid|high|healthy))\b", re.I)
_UNMEASURED = re.compile(r"\b(?:foot traffic|pedestrian traffic|demand|busy|busier|popular\w*|"
                         r"vibrant|thriving|strong market|healthy market|customer base|appetite|"
                         r"interest in|attendance|turnout)\b", re.I)
_ASSERTED = re.compile(r"\b(?:strong|solid|high|heavy|good|healthy|steady|robust|significant|great|"
                       r"plenty of|lots of|ample|busy)\s+(?:\w+\s+){0,2}?(?:foot traffic|pedestrian traffic|"
                       r"demand|customer base|interest|attendance|turnout)", re.I)
_EXISTS = re.compile(r"\b(?:foot traffic|pedestrian traffic|demand|customer base|interest)\s+"
                     r"(?:\w+\s+){0,2}?(?:exists|is (?:there|present|high|strong|solid|good|healthy|steady))", re.I)
_HEDGED = re.compile(r"\b(?:unknown|not|no|cannot|can't|without|unclear|isn't|aren't|doesn't|"
                     r"don't|won't|unmeasured|missing|n't)\b", re.I)


def _unsupported_inferences(answer):
    """Sentences that treat counts as proof of foot traffic, demand or interest.

    Listing counts cannot show any of those, and the prompt says so. A
    sentence saying they are unknown passes; one saying 78 restaurants
    'suggest solid foot traffic' is reported.
    """
    found = []
    for sentence in _split_sentences(answer):
        # Judge each clause on its own: "foot traffic exists, but demand is
        # unknown" still claims foot traffic in its first half.
        for clause in re.split(r",?\s+(?:but|while|although|though|whereas|yet)\s+|;|\s[—–-]\s|—", sentence):
            if (((_INFERS.search(clause) and _UNMEASURED.search(clause)) or _ASSERTED.search(clause)
                    or _EXISTS.search(clause)) and not _HEDGED.search(clause)):
                found.append(f"an unsupported claim ('{clause.strip()[:80]}')")
                break
    return found


def _sources(sql, geoids, rows):
    """Attach Census links for the actual level of geography and each data vintage."""
    text = (sql or "").lower()
    geo, geo_note = _census_geo(geoids)
    out, seen = [], set()

    def add(label, url):
        if url not in seen:
            seen.add(url)
            out.append({"label": _no_tract_words(label), "url": url})

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
        if source['retrieved_at']:
            # Refreshed straight from the publisher (get_data.refresh_county).
            add(f"{source['label']} ({source['data_vintage'] or 'source data'}, retrieved {str(source['retrieved_at'])[:10]})", source['source_url'])
        else:
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
