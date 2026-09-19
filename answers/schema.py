"""
The schema description we hand to Claude so it can write SQL.

This is NOT the database definition — Sid owns that in get_data/database.py.
This is a plain-English description of what exists, written for a model to
read. If Sid adds a column, add a line here.
"""

SCHEMA_DESCRIPTION = """
SQLite database: public community data for Montgomery County, Maryland
(which contains Silver Spring). 232 census tracts, 5,953 businesses.

TABLE tract_data  -- one row per census tract (a neighborhood-sized area)
  geoid                                TEXT, unique tract id, e.g. '24031702502'
  tract_name                           TEXT, e.g. 'Census Tract 7025.02; Montgomery County; Maryland'
  geometry_geojson                     TEXT, GeoJSON polygon of the tract boundary
  land_area_square_meters              INTEGER
  centroid_latitude                    REAL
  centroid_longitude                   REAL
  population_count                     INTEGER, total residents
  age_18_34_count                      INTEGER, residents aged 18-34
  foreign_born_count                   INTEGER, residents born outside the US
  median_income                        INTEGER, median household income in dollars
  english_only_at_home_count           INTEGER, speak only English at home
  non_english_at_home_count            INTEGER, speak another language at home
  occupied_housing_units_count         INTEGER
  renter_occupied_housing_units_count  INTEGER
  public_transit_commuter_count        INTEGER
  total_commuter_count                 INTEGER
  acs_vintage                          TEXT, e.g. '2024'
  source_url                           TEXT, citation link for this row
  retrieved_at                         TEXT

TABLE businesses  -- one row per business, located inside a tract
  business_id    TEXT
  name           TEXT
  category       TEXT, e.g. 'restaurant', 'bank', 'grocery', 'hairdresser'
  address        TEXT
  latitude       REAL
  longitude      REAL
  geoid          TEXT, the tract this business sits in -> joins to tract_data.geoid
  source_name    TEXT
  source_url     TEXT, citation link

TABLE data_sources  -- provenance for each dataset
  dataset_id, dataset_name, publisher, source_url,
  geographic_coverage, data_vintage, retrieved_at, coverage_note

IMPORTANT CONTEXT
- Fenton Village is a business district in Silver Spring. It spans these
  four tracts: '24031702502', '24031702503', '24031702501', '24031702402'.
  When a question mentions Fenton Village, filter to those geoids.
- Silver Spring is part of Montgomery County. Unless a question names a
  specific place, query all tracts.
- Coverage is the whole county, so county-wide comparisons are possible.

DATA QUIRKS YOU MUST HANDLE
- median_income is TOP-CODED at 250001. That is not a real income; it is the
  Census Bureau's ceiling. Never rank or compare tracts by median_income
  without excluding it: add  WHERE median_income < 250001.
  If asked how income varies, show a range or distribution across tracts
  with that filter applied, not just the highest values.
- Some columns are NULL for some tracts. Filter with IS NOT NULL when ranking.

RULES FOR WRITING SQL
- SELECT statements only. Never INSERT, UPDATE, DELETE, DROP, ALTER, PRAGMA.
- ALWAYS include source_url in the columns you select, so answers can cite.
- Never select geometry_geojson; it is huge and not useful in an answer.
- Compute rates explicitly and guard denominators with NULLIF,
  e.g. 100.0 * foreign_born_count / NULLIF(population_count, 0)
- Some columns may be NULL. Use IS NOT NULL filters where it matters.
- Prefer LIMIT 10 or fewer unless the question clearly wants more.
- Return raw counts alongside any percentage you compute.
"""

# Shown to the user when we can't answer. Describes real coverage.
COVERAGE_BLURB = (
    "I can answer questions about population, household income, foreign-born "
    "residents, languages spoken at home, housing, and commuting for any of "
    "the 232 census tracts in Montgomery County, Maryland — plus 5,953 local "
    "business locations, including Fenton Village in Silver Spring."
)
