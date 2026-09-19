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

TABLE tract_history
  Same tracts, two points in time, so questions about CHANGE can be answered.
  geoid                                TEXT    join to tract_data / places
  acs_vintage                          TEXT    'ACS 2016-2020' or 'ACS 2020-2024'
  period_start, period_end             INTEGER e.g. 2016 and 2020
  population_count                     INTEGER
  median_income                        INTEGER nominal dollars, NOT adjusted
  foreign_born_count                   INTEGER
  language_population_age_5_plus_count INTEGER
  english_only_at_home_count           INTEGER
  non_english_at_home_count            INTEGER
  occupied_housing_units_count         INTEGER
  renter_occupied_housing_units_count  INTEGER
  total_commuter_count                 INTEGER
  public_transit_commuter_count        INTEGER
  source_url                           TEXT
  464 rows: 232 tracts x 2 vintages.

HOW TO ANSWER "WHAT'S CHANGING?"
- Use tract_history, not tract_data. Aggregate each vintage separately and
  return BOTH rows so the change is visible, e.g.
    SELECT acs_vintage,
           SUM(public_transit_commuter_count) AS transit_commuters,
           SUM(total_commuter_count) AS commuters,
           100.0 * SUM(public_transit_commuter_count)
                 / NULLIF(SUM(total_commuter_count), 0) AS transit_pct,
           MIN(source_url) AS source_url
    FROM tract_history
    WHERE geoid IN (<the tracts in scope>)
    GROUP BY acs_vintage ORDER BY acs_vintage
- The two windows are 2016-2020 and 2020-2024. Say the windows, not "2020"
  and "2024" - a 5-year estimate is an average over its window.
- median_income is in the dollars of its own period and is NOT inflation
  adjusted, so a rise is partly prices. If you report an income change, say
  that plainly. Never call it a real-terms gain.
- tract_data holds only the current vintage. Never mix a tract_data figure
  with a tract_history figure in the same comparison.

TABLE places
  place_id     INTEGER  primary key
  name         TEXT     e.g. 'Woodside', 'Lyttonsville', 'Fenton Village'
  kind         TEXT     'neighbourhood', 'suburb', 'district', 'locality'...
  geoid        TEXT     the tract this place sits in; join to tract_data
  latitude     REAL     place point (NULL for districts)
  longitude    REAL
  precision    TEXT     how the tract was assigned
  source_name  TEXT     'OpenStreetMap' or 'Montgomery County Planning...'
  source_url   TEXT
  63 named places across Silver Spring, 84 rows (a place can span tracts).

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

- People write the name loosely. "Silver Springs", "silver spring md",
  "downtown Silver Spring", "SS" and similar ALL mean Silver Spring. Never
  refuse a question because the spelling is off — read what they meant.

- NEIGHBOURHOODS AND DISTRICTS live in the `places` table. Do not guess a
  place's tracts and do not refuse a named place before checking it. Join:

    SELECT ... FROM tract_data t
    JOIN places p ON p.geoid = t.geoid
    WHERE LOWER(p.name) = LOWER('Woodside')

  Match names case-insensitively, and allow a LIKE match when the user's
  wording is close ("Fenton" for "Fenton Village"). A place can map to more
  than one tract, so aggregate across them.

  places.kind = 'district' means a downtown sector-plan district (Fenton
  Village, Ellsworth, Ripley District, Metro Center, Downtown North, South
  Silver Spring, Falklands). No agency publishes their boundaries, so each
  was located from its namesake street clipped to the official Silver Spring
  CBD - places.precision records exactly how. Adjacent districts can share a
  tract (Ripley District and Metro Center are both in 7025.01), so they will
  return the same demographic figures. Say so plainly when it happens: the
  Census publishes no smaller than a tract. Business counts can still differ
  between districts that span different tracts.

  places.kind = anything else came from OpenStreetMap and is located in the
  single tract containing that place's point.

- MONTGOMERY COUNTY (all 232 tracts, no geoid filter) is for EXPLICIT
  comparisons only — when the question says "county", "countywide",
  "compared to the county", or "versus the rest of the area". When you do
  compare, return BOTH numbers in the same result so the difference is
  visible, e.g. one row for Silver Spring and one for the county.

- Only reply CANNOT_ANSWER when the question is about somewhere genuinely
  outside our coverage (Bethesda, Rockville, Wheaton, another county or
  state), or about something the columns simply do not hold. A question
  about Silver Spring that you can answer with the columns listed above is
  ALWAYS answerable — including loose wording, misspellings, and everyday
  phrasing like "the cheapest housing" or "where should I open a cafe".

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
- HOUSING AFFORDABILITY: there is no rent or house-price column. Answer with
  median_income (excluding the 250001 top-code) plus the renter share, and
  say which measure you used. Do not refuse the question.
- When a question is about a named place, SELECT the geoid column too. The
  map highlights whatever geoids come back, so an answer without them
  leaves the map showing nothing.
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
    "Village district and 60-odd named neighbourhoods. I can also compare "
    "Silver Spring with the rest of "
    "Montgomery County. I don't have data on other named neighborhoods, "
    "only census tracts."
)
