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
[1] Claude writes SQL        model: claude-sonnet-5, max_tokens 1500
   |                         input: schema prompt + question
   |                         output: ONE SQLite SELECT, or "CANNOT_ANSWER"
   v
[2] sql_guard.validate()     model is now OUT of the loop
   |
   v
[3] read-only execution      file:...?mode=ro, 8s timeout, max 200 rows
   |
   v
[4] Claude phrases it        max_tokens 800
   |                         input: question + up to 25 returned rows as JSON
   |                         instruction: use ONLY these numbers
   v
answer + sources + the SQL itself
```

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

Free-tier API sleeps after 15 minutes idle and takes ~1 minute to wake. The
static site never sleeps, so the page loads instantly and the map sits empty
during the wake. Starter plan ($7/mo) removes this.

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

---

# Part 3 — Things we tried that don't work

Permanent. These cost hours; don't rediscover them.

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
