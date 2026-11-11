# Combined data: handoff and verification

Branch: combined-community-data, based on upstream main cccd44e.
The earlier database-integration draft was not merged. The existing SQLite API,
original Silver Spring boundary, dated Census place/sex/industry estimates,
county coverage, primary OSM deduplication, and history remain the foundation.

## What is combined

Partner input is retained under data/partner_snapshot for reproducibility. Its
DuckDB is a build input only. Serving remains a single read-only SQLite database.
Each imported community_* table is catalogued in community_sources, with the
input SHA-256, original source, import time, scope and limitations. Retrieval
and source vintage are NULL when unknown; import time is not a retrieval date.

All original primary rows were compared with the upstream snapshot and are
unchanged: tract_data, businesses, places, place_data, industry_employment,
tract_history, data_sources. No partner business counts are added to primary
business counts.

Imported source tables:

| Table | Purpose and scope |
| --- | --- |
| community_venues | Names, source capacities, websites and venue categories |
| community_bus_stops | Stop inventory; wider county download |
| community_stations | Mapped rail/Metro stations |
| community_permits | Permit types, issue dates and declared valuations |
| community_ped_struck | Pedestrian-struck dispatch records |
| community_crime | Report rows; COUNT DISTINCT incident_id for distinct reports |
| community_parcels | Partner rectangular parcel download; no coordinates supplied |
| community_businesses | Supplementary OSM fields such as cuisine; overlapping coverage |
| community_tract_metrics | Extra, undated demographic metrics on original 2010 tracts |
| community_tract_geometry | Matching original 2010 geometry; not a current-tract crosswalk |

Point coordinates were independently assigned to the primary current tract
polygons. Unmatched points are retained with NULL geoid. Original 2010 tract
metrics use source_tract_id, never a misleading current geoid. Parcel assessments
are not rents; resident employment is not locally located jobs; ACS sex is not
self-identified gender. Missing venue capacity is not zero or verified capacity.

## Questions and API

The normal text-to-SQL model flow handles all questions. The prompt includes a
catalog generated from actual installed tables, columns and sampled categories.
There are no question keyword gates, fixed answer values, or fixed business
lists. Varied wording and list queries reach the same model entry point. Known
place mappings remain data in places. Standard business queries use the primary
deduplicated listings; supplementary fields remain queryable with their scope.

GET /coverage exposes both source catalogs. Answers cite the sources actually
queried. Supplemental-only answers do not receive a fabricated Census citation.
The original answer/summary/notes/rows contract is retained, and the frontend
can expand all returned fields so names, capacities, dates and other selected
fields do not disappear behind the simplified two-column summary.

SQL remains SELECT-only, read-only and resource limited. Those are safety
boundaries, not question-category restrictions. Query deadlines now measure
elapsed time. No document RAG was added because an approved text corpus has not
been provided. No model-generated claim can establish actual local demand,
commercial viability, venue availability, or a precise unprovided district boundary.

## Reproduce and refresh

Install build dependencies:

```bash
pip install requests pyshp -r get_data/requirements-community.txt
```

Add the bundled partner snapshot to an existing primary snapshot safely:

```bash
python -m get_data.import_community \
  --warehouse data/partner_snapshot/community.duckdb \
  --geometry data/partner_snapshot/tracts.geojson \
  --output /tmp/combined-review.db
python -m get_data.validate_database /tmp/combined-review.db
```

The importer writes a temporary copy and validates before replacing its output.
It does not edit the original DuckDB. Supplemental businesses that OpenStreetMap maps twice, as a point and as its building (same name, category and tract, within 50 m), are merged with the same rule as the primary businesses table; the building link is kept in duplicate_source_url. This removed 2 of 877 rows, including The Big Greek Cafe, so both tables now count 43 Fenton Village restaurants. Reimporting replaces only the supplementary
source tables; it does not duplicate them or overwrite primary tables.

The existing get_data.build_database command now reimports the bundled partner
snapshot after a Census/OSM rebuild, avoiding silently losing supplemental data.
Use --partner-warehouse and --partner-geometry together to supply a newer pair.
A failed primary or supplemental build leaves the active snapshot untouched.
Reusing the bundled partner snapshot does not refresh the original partner
sources. Obtain a rebuilt partner snapshot to refresh these records; no nightly
scheduler or metadata polling has been configured. DuckDB is not needed by the
API runtime, only the import step.

## Validation limits

Offline checks exercise the actual database and API with explicitly simulated
provider responses; they do not prove how a live model will choose SQL or phrase
its answer. The original four upstream evaluation questions remain unchanged.
Checks include source-specific queries for venues, transit, crime, permits,
parcels, cuisine and old-geography demographics, mixed industry/venue evidence,
wording variations reaching the model, import rollback/repeatability, and
elapsed-time interruption.

A valid ANTHROPIC_API_KEY and optional ANTHROPIC_MODEL are needed for live
question testing. The map and /coverage can start without a key. No key was
added, no live AI calls were made, and no external deployment was changed.
After configuration, run evals/run.py against the local API and review the SQL,
figures, scope, source notes and UX together before release. Automated structural
passes cannot prove the real-world accuracy of unverified source records.

## Results of this offline verification

- All 25 automated tests passed, including the unchanged upstream four-question
  data/API checks and additional source integration, wording, rollback, import,
  missing-model and timeout checks.
- Primary rows were independently compared with the upstream Git snapshot:
  all seven original tables are unchanged, including 5,989 business listings,
  232 current tracts, official place/industry estimates and 464 history rows.
- All nine partner source tables are present, with validated row counts and
  foreign keys; original demographic geometry is preserved separately.
- A browser rendering check used real returned venue rows and a simulated model
  summary. All five selected fields (name, category, capacity, website, source
  URL) and seven returned venue rows were accessible in the expanded table.
  This was an explicitly controlled test, not a live model response.
- Map and coverage requests used the actual local API. /coverage returned eight
  primary sources and nine supplemental sources. Original boundary file is
  unchanged. Homepage and app checked at 320, 390, 768 and 1440 pixels without
  horizontal page overflow. The mock browser request handler was removed.
- Dependency consistency, Python compilation, JavaScript syntax and whitespace
  checks passed. No external deployment or GitHub push was performed.
