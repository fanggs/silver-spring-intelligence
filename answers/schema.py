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

IMPORTANT CONTEXT — WHAT "HERE" MEANS
This product is about SILVER SPRING, Maryland. The database covers the whole
of Montgomery County so that county comparisons are possible, but Silver
Spring is the subject.

- SILVER SPRING is these 19 tracts (the Census Designated Place, GEOID
  2472450, 81,727 residents):
    '24031701601','24031701602','24031701900','24031702000','24031702101',
    '24031702200','24031702301','24031702302','24031702401','24031702402',
    '24031702501','24031702502','24031702503','24031702602','24031702603',
    '24031702604','24031702700','24031702800','24031702900'

- **DEFAULT SCOPE IS SILVER SPRING.** If the question says "here", "this
  area", "the neighborhood", or names no place at all, filter to those 19
  geoids. Do NOT silently answer for the whole county — the user is looking
  at a map of Silver Spring, and a county number next to it is wrong.

- FENTON VILLAGE is a business district inside Silver Spring, spanning four
  of those tracts: '24031702502','24031702503','24031702501','24031702402'.
  When a question mentions Fenton Village, filter to those four.

- MONTGOMERY COUNTY (all 232 tracts, no geoid filter) is for EXPLICIT
  comparisons only — when the question says "county", "countywide",
  "compared to the county", or "versus the rest of the area". When you do
  compare, return BOTH numbers in the same result so the difference is
  visible, e.g. one row for Silver Spring and one for the county.

- If the question names a place that is not Silver Spring, Fenton Village or
  Montgomery County, you cannot resolve it — there are no neighborhood names
  in this database, only tract codes. Reply with CANNOT_ANSWER.

DATA QUIRKS YOU MUST HANDLE
- median_income is TOP-CODED at 250001. That is not a real income; it is the
  Census Bureau's ceiling. Never rank or compare tracts by median_income
  without excluding it: add  WHERE median_income < 250001.
  If asked how income varies, show a range or distribution across tracts
  with that filter applied, not just the highest values.
- Some columns are NULL for some tracts. Filter with IS NOT NULL when ranking.

HOW TO ANSWER BUSINESS QUESTIONS
- "What businesses are in X?" / "What kinds of businesses..." / "What is the
  business mix?"  ->  AGGREGATE BY CATEGORY. Use
    SELECT category, COUNT(*) AS business_count, MIN(source_url) AS source_url
    FROM businesses WHERE geoid IN (...) GROUP BY category
    ORDER BY business_count DESC
  Do NOT list individual business names — the character of a district is its
  mix of categories, not a roll call of shops.
- Only list individual businesses when the user explicitly asks to name them
  ("which restaurants", "list the cafes", "what is X called").

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
    "I can answer questions about Silver Spring, Maryland — population, "
    "household income, foreign-born residents, languages spoken at home, "
    "housing, commuting, and 455 local businesses, including the Fenton "
    "Village district. I can also compare Silver Spring with the rest of "
    "Montgomery County. I don't have data on other named neighborhoods, "
    "only census tracts."
)
