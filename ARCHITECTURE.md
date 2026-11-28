# CivicLens — architecture

How this system works and, more importantly, **why it is arranged this way**.

Split into two parts on purpose:

- **Principles** are load-bearing. Change them deliberately or not at all.
- **Current implementation** is expected to change. Last true: **Nov 2026**.

---

# Part 1 — Principles

## 1. The model never states a number it did not get from the data

This is the whole product. Everything else is detail.

A language model asked "how many businesses are in Fenton Village" will answer
from its weights, and a wrong number looks exactly like a right one. So the
model is never allowed to produce the figure. It produces the *instructions for
finding* the figure, the database executes them, and the returned rows are the
answer.

> A model that writes numbers can hallucinate one.
> A model that writes a query cannot — the query either runs or it doesn't.

**What this constrains, regardless of implementation:**

- Whatever generates the answer text may only see values that came back from
  the data layer in that same request.
- No step may invent, estimate, round into existence, or infer a figure.
- If the data can't answer the question, the correct output is a refusal, not a
  best guess.

**What it does NOT constrain:** which model, which provider, how many calls,
what query language, what database. All of that is free to change. The
guarantee is about information flow, not technology.

**If you change the pipeline, ask this one question:** *can the text the user
reads contain a number that did not come out of the database in this request?*
If yes, the change broke the product, however well it works otherwise.

## 2. Every answer traces back to a published source

Each answer names the exact table it came from, on the geography it covers.
Not "the Census" — *ACS 5-Year 2020-2024, Table B19013, for these 19 tracts.*

This only works because **provenance is recorded at ingest**. Every row in the
database carries its source name, source URL, vintage, and retrieval date. That
cannot be bolted on later: if the pipeline doesn't record where a row came
from, nothing downstream can reconstruct it.

A citation must also **state its arithmetic**. An answer summing 19 tracts and
a source page showing one tract look like a contradiction unless the citation
says "summed across Silver Spring's 19 census tracts." This cost us a judge's
confidence once; it is not cosmetic.

## 3. Generated queries are untrusted input

The model writes code that runs against the database. Treat it as hostile,
regardless of how reasonable it looks.

Validation lives in `answers/sql_guard.py` and is deliberately layered, so no
single check is the only thing standing between a generated string and the
data. If the storage layer changes, the validation moves with it.

---

# Part 2 — Current implementation

*Everything below is how it works as of Nov 2026 and is expected to change.*

## The request path

```
question
   |
   v
[1] Claude writes SQL        model: claude-haiku-5-5, effort medium, max_tokens 8000
   |                         input: schema prompt (cached) + question
   |                         output: the main SELECT, plus up to 3 labelled
   |                         "-- evidence:" SELECTs for decision questions
   |                         (demand, supply, access, risk, trend, benchmark),
   |                         or "CANNOT_ANSWER"
   v
[2] sql_guard.validate()     model is now OUT of the loop
   |
   v
[3] read-only execution      file:...?mode=ro, 8s timeout, max 200 rows
   |                         a query SQLite rejects gets ONE repair: the model
   |                         sees the error and rewrites it
   v
[4] Claude phrases it        effort medium, max_tokens 4000
   |                         input: question + up to 25 returned rows as JSON
   |                         + exact totals computed by code
   |                         instruction: use ONLY these numbers
   |                         a draft with an unreturned number is rewritten once
   v
answer + sources + the SQL itself
```

Where the time goes (Nov 27, 20 questions, warm server): the database takes
milliseconds; the schema prompt (~24,500 tokens) is read from the prompt
cache. Almost all of the wait is the model writing: the query step writes
1,000-5,600 tokens (thinking plus SQL, 4-21 s) and the writing step
400-2,000 (2-10 s). Typical answer ~11 s, slowest one in ten ~22 s.

Time limits: the query call gets 40 s and the writing call 25 s. A repair
only starts within 20 s of the question and a rewrite within 30 s;
otherwise the first draft is used, with sentences whose numbers fail the
check removed. This caps the worst case at about 85 seconds
instead of four 60 s calls in a row.

When the data is back and the answer still has to be written, `/ask/stream`
sends everything code builds from the rows (map selection, chart, statistic,
tables, sources) with the `answer` step, with no summary. The page shows it
under "Here is the data. Writing a short summary…" while the sentence is
written (Nov 28: first data on screen at a median of 10.9 s against 15.9 s
for the full answer). Every number in the preview is a database value.

Two calls, with the database between them. The call that sees the question
never sees a number; the call that writes the sentence only sees numbers the
database produced.

`CANNOT_ANSWER` is a sentinel, not a vibe. Refusal is a code path the first
call can take, and step 2 checks for it explicitly.

## The guard (`answers/sql_guard.py`, 80 lines)

Seven checks, in order:

1. Strip markdown fences the model sometimes wraps SQL in
2. Reject empty queries
3. Must start with `SELECT` or `WITH` (CTEs are legitimate)
4. Reject any `;` — blocks `SELECT 1; DROP TABLE x`
5. Banned-keyword regex: INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE,
   TRUNCATE, ATTACH, DETACH, PRAGMA, VACUUM, REINDEX, GRANT, EXEC
6. Connection opened read-only at the SQLite level (`mode=ro`), so writes are
   physically impossible even if a check were bypassed
7. Progress-handler timeout (8s) and a 200-row cap, so a runaway query can't
   hang the service

## Stack

| Layer | What | Owner |
|---|---|---|
| Ingest | `get_data/` — Census ACS, TIGER, OpenStreetMap, M-NCPPC | Siddharth |
| Storage | `data/silverspring.db` — SQLite, provenance on every row | Siddharth |
| API + AI | `answers/` — FastAPI, two-call bridge, guard, citations | Alan |
| Frontend | `page/` — Leaflet map, no build step | Aryan |

## Deployment

Both services on the Render account `tools@uxuniversity.io`, free tier.

```
Frontend   https://civiclens-pzpt.onrender.com       static site, publish dir: page
API        https://civiclens-api-ack0.onrender.com   web service
Domain     uxucivic.com                              added, pending DNS
```

The frontend selects the local API on port 8000 when served on localhost.
The public frontend uses the Render URL configured in `page/script.js`.

Free-tier API sleeps after 15 minutes idle and takes ~45 seconds to wake
(measured Nov 27). The API keeps itself awake: on Render (`RENDER=true`) a
background thread calls its own public `/health` every 10 minutes, which
Render counts as a visit. `.github/workflows/keep-awake.yml` does the same
from outside as a backup, but GitHub ran none of its scheduled pings in the
first hour, so it can't be relied on alone. One always-on free service uses
~720 of the account's 750 free hours a month. A paid plan or an always-on
host makes both unnecessary.

**Answer cache.** Answers are kept for a day, keyed on the question, the
model and the database file's identity, so a data refresh or deploy starts
fresh and any repeated question is instant. Nothing is pre-answered: every
question takes the same path. Release checks send `Cache-Control: no-cache`. If the server is asleep anyway, the page says it is
starting instead of looking stuck, and waits up to 90 s for map layers and
120 s for an answer.

## Data

Sources and vintages are recorded in the `data_sources` table:

```
ACS 2020-2024          tract and official place estimates, sex, industry,
                       income, language, housing, commuting
ACS 2016-2020          same fields, earlier vintage — this is what makes
                       "what's changing" possible
Census TIGER 2024      tract boundary geometry
OpenStreetMap          mapped businesses and amenities by original tag
OpenStreetMap          neighborhood place points
M-NCPPC                official Silver Spring CBD boundary
```

The official Silver Spring Census Designated Place (GEOID 2472450) has a
published population estimate in `place_data`. The map's selected whole tracts
approximate that area. `industry_employment` identifies its place and tract
estimates separately. Industry figures describe employed residents. Census sex
estimates do not measure self-identified gender.

**Places.** OpenStreetMap place points resolve to their containing tract. The
seven downtown planning districts retain the project's reviewed tract mappings
and their method. These mappings approximate district geography. Counts over
those mappings describe whole tracts and depend on OpenStreetMap coverage.

A complete rebuild writes and validates a separate database file before
replacing the output. See [the data pipeline](get_data/README.md).

## Map highlighting

`highlight_geoids` in the response, resolved in this order:

1. `geoid` column in the returned rows
2. geoid literals found in the executed SQL (aggregates return no geoid column,
   but the WHERE clause knows)
3. a place named in the question, matched longest-name-first
4. fall back to all 19 Silver Spring tracts

## Yearly trend chart

When the returned rows are one count per calendar year (a `year` column,
3+ years, each year once), `_trend_visual` turns them into `visual.kind =
"line"` and the page draws it. The points are the returned rows; nothing is
computed. The chart has no axis numbers, only the returned values for the
highest and latest years, because a rounded axis step is a number the
database didn't produce.

Years a source only partly covers are marked from `community_sources`
first/last record dates and from date bounds in the SQL (records ending
Nov 19 make that year partial). The phrasing step is told which years are
partial and sees all the yearly rows, not just the first 25.

---

## Evidence queries and context data (Nov 23)

A decision question ("should I open a gym", "where should the county put a
pantry") needs several facts side by side and a benchmark, which one query
rarely gives. So the query step may return up to four SELECTs: the main one
first (it drives the map, chart and places exactly as before) and up to three
labelled evidence queries. Each goes through the same guard and read-only
execution; a failing evidence query is logged and skipped. The answer step
sees all their rows, and the number check accepts a figure only if it is in
one of them. Principle 1 is unchanged: every number still comes out of the
database in that request. Each evidence table is shown under the answer.

Comparisons are fair by construction (Nov 27): a rate divides a count by the
population of the same tracts it counted; a district inside Silver Spring is
compared with the *rest* of Silver Spring, not the whole that contains it;
rates use one unit per answer (per 1,000, or per 10,000 when smaller), so none
rounds to 0; and the answer compares areas of different size by rate, never
by raw count. Measured on 10 comparison questions, twice: mismatched
denominators 2 -> 0; blind judge query 3.0-3.1 -> 3.6-4.0, insight
2.7-2.8 -> 3.1-3.3; the 20 general questions unchanged.

Context tables, built by `get_data/fetch_insights.py` and registered in
`insight_sources` (same columns as `community_sources`, so the prompt
describes them automatically; guidance in `answers/insight_context.py`):

| Table | Source | Answers |
|---|---|---|
| `acs_measures` + `acs_measure_labels` | Census API, ACS 2016-2020 and 2020-2024, every county tract, every Maryland place, ZIP areas, counties, state, US | rent, home value, rent burden, poverty, unemployment, age, degrees, commute, cars, broadband, limited English, languages, occupation, industry; comparisons with Bethesda, Rockville, the county |
| `jobs_by_tract` | LEHD LODES 8 workplace data, 2018 and 2023 | jobs located here by sector, pay and age (daytime population) |
| `business_patterns` | County Business Patterns 2018-2023 by ZIP and county | establishments by detailed industry, change over time, per resident |
| `county_commercial_permits` | dataMontgomery i26v-w6bd | big projects, new construction |
| `county_crashes`, `county_crash_nonmotorists` | dataMontgomery bhju-22kf, n7fk-dce5 | crashes by time, light, severity, pedestrians and cyclists |
| `county_parking_facilities` | dataMontgomery rd7s-ntxu | public garage and lot capacity |
| `county_food_businesses` | dataMontgomery inspections (5pue-gfbe, dkrp-gr48) | food businesses with first/latest inspection (openings, closures) |
| `county_rental_survey` | dataMontgomery 65hk-6zwc | average rent by building and unit type, 2016-2022 |
| `county_liquor_licenses` | dataMontgomery c6rw-fazn | licensed restaurants, bars, stores |
| `county_development_pipeline` | Montgomery Planning pipeline layer | approved, unbuilt homes and space |
| `places` (kind 'planning area') | Montgomery Planning master/sector plan boundaries | named areas such as Long Branch, Takoma/Langley |

These are loaded once, not by the weekly refresh. Rerun with
`python -m get_data.fetch_insights [--only NAME ...]`.

---

# Part 3 — Things we tried that don't work

Permanent. These cost hours; don't rediscover them.

**Thinking off for the writing step is faster and wrong** (Nov 27). It cut
the step from ~800 output tokens to ~180, but answers said things like
"violent crime has risen, from 189 to 188" and opened with a misplaced
"No:". The number check can't catch a wrong direction. Effort "low" for
either step saved 1-2 s and scored slightly lower in three blind rounds.

**Merging the two model calls is not possible** without breaking Principle 1:
the call that writes the sentence must see the rows, which don't exist until
the query has run. Lookups that code can phrase (counts) already skip the
second call.

**Thinking off for the query step** (Nov 27) cut the typical answer from
18 s to 7 s and gave wrong numbers: the female-population question returned
the total population, violent crime was counted with no area filter. The
query step needs its thinking.

**Writing the main query and the evidence queries in two parallel calls**
saved nothing: the evidence call does all the same thinking and takes as
long as the original single call.

**Two quick drafts without thinking, used when they agree** (Nov 28), raced
against the usual query. Agreement caught real errors (42 vs 43 restaurants),
but "one draft's numbers inside the other's" let a narrower draft drop part
of the question (Bethesda in a Silver Spring vs Bethesda question), and with
exact agreement required it fired on 4 of 28 questions, did not move the
median, and dropped a caveat the usual path gave. Removed.

**Pre-answering the page's example questions** made those five instant but
nothing else; removed in favour of the general day-long cache.

**Prompt-cache misses are not a speed cost.** A miss on the ~24,800-token
prompt starts within ~0.5-0.8 s, the same as a hit.

**Streaming the answer text** would show sentences before the number check
has passed them. The page shows each step and the map selection instead.

**Shorthand for the 19 Silver Spring tract ids** (model writes one word, code
expands it) cut query output by only ~8% and scored slightly lower; reverted.

**ACS 2019 cannot be used for trends.** It predates the 2020 tract redraw, so
geoids don't line up across it. 2016-2020 and 2020-2024 share boundaries.

**B16001 is not published at tract level.** We pulled it for language data and
got 232 empty rows with no error anywhere. Use **C16001**. The general lesson:
*an empty column is more dangerous than an error* — write invariant checks on
ingest rather than trusting that success means success.

**`median_income = 250001` is not a number.** It's the Census top-code for
"higher than we publish." Excluding it revealed the real spread: $47,227 to
$201,591.

**Census citation links default to the United States.** A link like
`data.census.gov/table/ACSDT5Y2024.C16001` opens on the whole country and is
useless as evidence. Add the geography parameter: `?g=050XX00US24031` for the
county, `?g=1400000US<geoid>` for a tract.

**Fenton Village is not the four tracts you'd guess.** The hand-drawn version
included a tract contributing 4.4% of the district and missed three worth ~25%.
Use the namesake-street-clipped-to-CBD method.

**Manual Render setup does not apply `render.yaml`.** A static site created
through the dashboard needs the `/` → `/index.html` rewrite added by hand, or
the root 404s while every other path works.

**The Census API wants the 5-digit place code.** `for=place:72450&in=state:24`,
not the 7-digit GEOID `2472450`; the wrong form returns an empty body, not an
error.

**County inspection records changed vocabulary in July 2024.** The older
dataset says Restaurant / Carry Out / Market, the newer says Food Service
Facility, and the same business is spelled differently across the two. Match
on normalized address plus first name word, and compare by `category`.

**Every ACS cell for every Maryland place made the database 194 MB.** The
file is committed to git (GitHub's hard limit is 100 MB). Detail tables are
kept only for local places, labels live in `acs_measure_labels`, and there are
no indexes on `acs_measures` (a scan takes milliseconds). It is 66 MB now.
