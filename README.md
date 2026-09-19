# CivicLens

**Ask Silver Spring a question in plain English, and check the answer yourself.**

A queryable map of Silver Spring, Maryland. Ask *"Who lives in Woodside?"* or
*"What businesses are in Fenton Village?"* and get a real answer, the area
highlighted on a map, the rows behind the number, and a link to the exact
Census table it came from.

Built at Bay Hacks 2026 for the UX University Challenge.

- **Live API:** https://silver-spring-intelligence.onrender.com
  (`/health`, `/tracts`, `/businesses`, `/boundary`, `POST /ask`, `/docs`)
- **Map:** `page/` — open `page/index.html`, or serve it with
  `cd page && python3 -m http.server 5500`

> The API is on Render's free tier and sleeps after 15 minutes idle. The
> first request takes about 30 seconds to wake it.

---

## The rule this is built on

> **The model writes the query. The database produces the numbers.**

Claude never states a figure. It translates English into a SQL `SELECT`. That
query is validated (SELECT-only, single statement, banned keywords rejected),
run against a **read-only** connection with a timeout, and the rows that come
back *are* the answer. Claude then phrases a sentence using only those rows.

A model that writes numbers can hallucinate one. A model that writes a query
cannot — the query either runs or it doesn't, and the result came out of the
database.

## What's in the data

`data/silverspring.db` — SQLite, committed to the repo.

| Table | Rows | What |
| --- | --- | --- |
| `tract_data` | 232 | Current ACS estimates per census tract |
| `tract_history` | 464 | Same tracts at two ACS vintages, for measuring change |
| `businesses` | 5,953 | OpenStreetMap locations, each joined to its tract |
| `places` | 66 | 63 named neighbourhoods and districts → tracts |
| `data_sources` | 6 | Publisher, vintage, coverage, retrieval date |

Silver Spring is the Census Designated Place (GEOID 2472450): **19 tracts,
81,727 residents, 455 businesses**. The county's other tracts stay in the
database so "compared with the rest of the county" is a real query.

### Sources

- **U.S. Census Bureau, ACS 5-Year Estimates** (2016–2020 and 2020–2024) via
  `api.census.gov` — B01003, B01001, B05001, B19013, C16001, B25003, B08301
- **Census TIGER/Line 2024** — tract geometry and the Silver Spring CDP boundary
- **OpenStreetMap** via the Overpass API — businesses and place names
- **Montgomery County Planning (M-NCPPC)** — the official Silver Spring
  Central Business District polygon

Every row stores the source it came from. That is the only reason the
citations in an answer can exist.

## Layout

```
answers/      the API and the text-to-SQL bridge
  main.py       endpoints, prompts, citation building
  schema.py     the schema and rules shown to the model
  sql_guard.py  validation, read-only execution, timeout
get_data/     the ingest pipeline (one script per source)
page/         the map — plain HTML, CSS, Leaflet, no build step
data/         silverspring.db and the Silver Spring boundary
```

## Running it

```bash
pip install -r answers/requirements.txt
echo "ANTHROPIC_API_KEY=sk-..." > .env
uvicorn answers.main:app --reload
```

Then serve the map:

```bash
cd page && python3 -m http.server 5500
```

Point `API_BASE_URL` at the top of `page/script.js` to your API. **No trailing
slash** — it produces `//tracts` and 404s every request.

To rebuild the database from scratch you also need `CENSUS_API_KEY` in `.env`
(free from census.gov/developers), then run `get_data/build_database.py`.

## Things worth knowing

- **`median_income` is top-coded at 250001.** That is the Census ceiling, not
  an income. Every query excludes it or the averages are meaningless.
- **Language data comes from C16001, not B16001.** B16001 isn't published at
  tract level — it returns empty without erroring.
- **Downtown districts have no published boundaries.** Fenton Village,
  Ellsworth, Ripley District and the rest are named in the county sector plan
  but exist as figures in a PDF, not as data. Each is located from its
  namesake street in OpenStreetMap, clipped to the official CBD polygon.
  `places.precision` records how every place was resolved.
- **Trends use 2016–2020 vs 2020–2024.** ACS 2019 predates the 2020 tract
  redraw, so its geoids don't line up.

## Team

Alan Tran (API and query layer) · Sid Valluri (data pipeline) ·
Aryan Nimmagadda (map and interface)
