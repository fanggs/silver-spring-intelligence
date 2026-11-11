"""
The schema description we hand to Claude so it can write SQL.

This is NOT the database definition — Sid owns that in get_data/database.py.
This is a plain-English description of what exists, written for a model to
read. If Sid adds a column, add a line here.
"""

SCHEMA_DESCRIPTION = """
SQLite database: public community data for Montgomery County, Maryland
(which contains Silver Spring).

TABLE tract_data  -- one row per census tract (a neighborhood-sized area)
  geoid                                TEXT, unique tract id, e.g. '24031702502'
  tract_name                           TEXT, e.g. 'Census Tract 7025.02; Montgomery County; Maryland'
  geometry_geojson                     TEXT, GeoJSON polygon of the tract boundary
  land_area_square_meters              INTEGER
  centroid_latitude                    REAL
  centroid_longitude                   REAL
  population_count                     INTEGER, estimated total residents
  population_margin_of_error           INTEGER
  female_count, male_count             INTEGER, ACS sex estimates, not gender identity
  female_margin_of_error, male_margin_of_error INTEGER
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

TABLE businesses  -- named OpenStreetMap listings, including businesses and amenities
  business_id    TEXT
  name           TEXT
  category       TEXT, e.g. 'restaurant', 'fast food', 'cafe', 'bar', 'pub', 'books'
  category_group TEXT, e.g. 'food and drink', 'retail', 'other amenity'
  osm_amenity    TEXT, original OSM amenity tag, e.g. 'restaurant' or 'fast_food'
  osm_shop       TEXT, original OSM shop tag, e.g. 'books' for bookstores
  duplicate_source_url TEXT, alternate OSM record when a matching node and area were merged
  address        TEXT
  latitude       REAL
  longitude      REAL
  geoid          TEXT, the tract this business sits in -> joins to tract_data.geoid
  source_name    TEXT
  source_url     TEXT, citation link

TABLE place_data  -- official Census Designated Place estimates, separate from tracts
  geoid TEXT ('2472450'), place_name TEXT, population_count INTEGER,
  population_margin_of_error INTEGER, female_count INTEGER, female_margin_of_error INTEGER,
  male_count INTEGER, male_margin_of_error INTEGER, acs_vintage TEXT, source_url TEXT, retrieved_at TEXT

TABLE industry_employment  -- current ACS C24050 industry estimates, one row per industry and geography
  geoid TEXT, geography_type TEXT ('place' or 'tract'), industry TEXT,
  employed_residents INTEGER, margin_of_error INTEGER, acs_vintage TEXT,
  source_url TEXT, retrieved_at TEXT
  industry='Retail trade' counts residents age 16+ employed in retail, not jobs located there.
  industry='All industries' is the total. Do not double count it in an industry sum.
  Join to tract_data or places only for geography_type='tract'.

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
           ROUND(100.0 * SUM(public_transit_commuter_count)
                 / NULLIF(SUM(total_commuter_count), 0), 1) AS transit_pct,
           SUM(public_transit_commuter_count) AS transit_commuters,
           SUM(total_commuter_count) AS commuters,
           MIN(source_url) AS source_url
    FROM tract_history
    WHERE geoid IN (<the tracts in scope>)
    GROUP BY acs_vintage ORDER BY acs_vintage
- For a general question about changes, compare two measures. If no topic is
  named, select population and public transit commute share only, with
  transit counts and denominators as supporting values.
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
  One place can map to multiple whole tracts.

IMPORTANT CONTEXT — WHAT "HERE" MEANS
This product is about SILVER SPRING, Maryland. The database covers the whole
of Montgomery County so that county comparisons are possible, but Silver
Spring is the subject.

- For population or ACS sex estimates for Silver Spring as a whole, use place_data
  WHERE geoid='2472450'. For the Silver Spring industry estimate (including "around here"
  without a named district), use industry_employment with geography_type='place'
  AND geoid='2472450'. Return only the official place row unless the question
  asks for smaller areas or a geographic comparison. Select the estimate,
  margin of error, and source_url.
- A question about residents who identify as female can be answered with the
  closest available measure, female_count, clearly labeled as Census sex.
  Say gender identity is not measured by that estimate.
- For other metrics available only at tract level, the map uses these 19 whole
  tracts as an approximation of Silver Spring. They do not match the official place boundary:
    '24031701601','24031701602','24031701900','24031702000','24031702101',
    '24031702200','24031702301','24031702302','24031702401','24031702402',
    '24031702501','24031702502','24031702503','24031702602','24031702603',
    '24031702604','24031702700','24031702800','24031702900'
- Use the Silver Spring scope for unqualified "here" or "around here". Only use
  the county for an explicit county question. Never add the CDP row to tract rows.

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

  places.kind = 'district' means a downtown sector-plan district. These are tract
  approximations. Use the distinct mapped geoids with IN (SELECT geoid FROM places ...)
  so a row cannot be counted twice. Always distinguish the approximation from
  the actual district boundary. This applies to all counts, including businesses.

  places.kind = anything else came from OpenStreetMap and is located in the
  single tract containing that place's point.

- MONTGOMERY COUNTY (all 232 tracts, no geoid filter) is for EXPLICIT
  comparisons only — when the question says "county", "countywide",
  "compared to the county", or "versus the rest of the area". When you do
  compare, return BOTH numbers in the same result so the difference is
  visible, e.g. one row for Silver Spring and one for the county.

- Only reply CANNOT_ANSWER when the requested information cannot be established
  from the installed tables. Check the actual available geography and fields
  first; do not reject a place by a fixed list of names. A question
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
- For a specific count, apply the exact tag first and return a single aggregate for
  the requested area. For counts of multiple venue types in one area, return
  one total per type across all mapped tracts. Group only by type and omit
  names and geoid unless requested. Return category and COUNT(*) AS listing_count.
  Restaurants are osm_amenity='restaurant'. A request for
  food and drink broadly can use category_group='food and drink', with a breakdown
  by category. Do not silently include cafes, bars, pubs or fast food in restaurant counts.
- For a business decision, use the requested business type as the filter.
  Coffee shops are osm_amenity='cafe'. Bookstores are osm_shop='books' (which
  also includes specialty book shops). Return the matching count and names,
  e.g. COUNT(*) and GROUP_CONCAT(name, ', '). Include another category or
  measure only if the question asks for it. A limited list of top categories
  never proves a type is absent.
- Counts reflect mapped OpenStreetMap listings, not a complete operating-business
  registry. Listings alone do not verify opening status, foot traffic, or demand.
- For broad mix, group by category. For an event planning question, gather any
  requested industry estimate and use it to offer limited, practical suggestions.
  Do not imply retail residents work locally or infer interest in the event.

RULES FOR WRITING SQL
- Retrieve only evidence directly relevant to the question. For a business
  decision, count the requested business type and expose its mapped names;
  do not dump all business categories or unrelated resident employment.
  Include other measures when they answer a requested part of the question.
- Listing counts do not measure foot traffic, demand, event interest or
  competition intensity. Never label them as proxies for those measures.
  Industry counts measure employed residents, not local workplace jobs.
- Give result columns readable, factual aliases. Do not GROUP_CONCAT every
  unrelated category. Do not invent missing capacities, availability, rents,
  gender identity counts, or precise district boundaries.
- HOUSING AFFORDABILITY: there is no rent or house-price column. Answer with
  median_income (excluding the 250001 top-code) plus the renter share for
  context. Return at most three tracts with the lowest median income. Income
  and renter share cannot establish where housing costs the least.
- Return geoids for individual rows or geographic comparisons. For totals
  across a named area, do not group by geoid. The map can use the place name.
- SELECT statements only. Never INSERT, UPDATE, DELETE, DROP, ALTER, PRAGMA.
- ALWAYS include source_url in the columns you select, so answers can cite.
- Avoid selecting large geometry_geojson values unless geometry itself is requested.
- Compute rates explicitly and guard denominators with NULLIF,
  e.g. 100.0 * foreign_born_count / NULLIF(population_count, 0)
- Some columns may be NULL. Use IS NOT NULL filters where it matters.
- Use a sensible LIMIT for an exploratory list; return the requested scope for explicit lists. Do not mistake a limited list for the full count.
- Include `pct` in percentage aliases. Return raw counts alongside percentages.
"""

# Shown to the user when we can't answer. Describes real coverage.
COVERAGE_BLURB = (
    "I can answer questions about Silver Spring population, Census sex estimates, "
    "industry of employed residents, household income, language, housing, commuting, "
    "mapped businesses and amenities, venues, transit stops, reported incidents, "
    "permits, parcels, and additional community measures when installed. Geography "
    "and snapshot dates vary by source."
)
