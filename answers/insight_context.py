"""Describe the context tables (get_data.fetch_insights) and how to use them.

This is guidance on reasoning, not a question router: it tells the query
model what each table measures, how geographies line up, and what a useful
answer for this audience contains. No question text is matched here.
"""
from contextlib import closing
import sqlite3

SILVER_SPRING_TRACTS = ("24031701601", "24031701602", "24031701900", "24031702000", "24031702101",
                        "24031702200", "24031702301", "24031702302", "24031702401", "24031702402",
                        "24031702501", "24031702502", "24031702503", "24031702602", "24031702603",
                        "24031702604", "24031702700", "24031702800", "24031702900")

# Short notes shown under an answer that used the table. The full coverage
# notes go to the query model; readers get one plain sentence.
SHORT_NOTES = {
    "acs_measures": "Census figures are 5-year survey estimates with margins of error.",
    "jobs_by_tract": "Jobs are counted where people work (Census LEHD), not where they live.",
    "business_patterns": "Business counts are establishments with employees, by ZIP code, which does not match neighborhood boundaries.",
    "county_commercial_permits": "Permits are approvals to build, not completed construction.",
    "county_crashes": "Crash records are police reports.",
    "county_crash_nonmotorists": "Crash records are police reports.",
    "county_parking_facilities": "Parking covers county-run public garages and lots only.",
    "county_food_businesses": "Food businesses come from county inspection records; first inspection is not a confirmed opening date.",
    "county_rental_survey": "Rents are averages reported by surveyed apartment communities, 2016-2022.",
    "county_liquor_licenses": "Alcohol licenses are current county records.",
    "county_development_pipeline": "Pipeline projects are approved by the Planning Board but not finished; job figures are planners' estimates.",
}


def _installed(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()


def schema_context(path):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
        if not _installed(con, "acs_measures"):
            return ""
        curated = con.execute("SELECT measure, label FROM acs_measure_labels WHERE measure NOT LIKE '%:%' ORDER BY rowid").fetchall()
        grouped = con.execute("SELECT measure FROM acs_measure_labels WHERE measure LIKE '%:%' ORDER BY measure").fetchall()
    tracts = ", ".join(f"'{g}'" for g in SILVER_SPRING_TRACTS)
    measures = "\n".join(f"  {m}: {label}" for m, label in curated)
    groups = "\n".join(f"  {m[0]}" for m in grouped)
    return f"""
CONTEXT TABLES FOR INSIGHT (Census, jobs, business patterns, county records).

TABLE acs_measures (geoid, geography_type, geography_name, acs_vintage, measure, estimate, margin_of_error)
 (acs_measures has NO source_url column: its sources are attached automatically, so never select one from it.)
TABLE acs_measure_labels (measure, label, universe, table_id, variables, source_url)
 geography_type: 'tract' (Montgomery County tracts, same geoid as tract_data), 'place' (every Maryland
 place: Silver Spring CDP geoid '2472450', Bethesda CDP, Rockville city, Wheaton CDP, Takoma Park city...;
 match with geography_name LIKE 'Bethesda CDP%'), 'zcta' (ZIP code areas; geoid is the 5-digit ZIP),
 'county' (geoid '24031' is Montgomery County), 'state' ('24'), 'nation' ('US').
 acs_vintage: 'ACS 2016-2020' or 'ACS 2020-2024'. Both use 2020 tract boundaries, so tracts compare across them.
 Curated measures (counts unless the label says median or dollars):
{measures}
 Detailed tables (measure is 'TABLE:label path'; male and female cells are already summed):
{groups}
 Rates: compute in SQL from a count and its universe in the SAME geography and vintage, e.g.
 100.0*below_poverty/poverty_universe, 100.0*unemployed/civilian_labor_force,
 100.0*renters_burdened_30/(renters_for_burden-renters_burden_not_computed),
 100.0*bachelors_or_higher/population_25_plus, 100.0*age_65_plus/population,
 mean commute minutes = commute_minutes_total/commuters_not_home. Pivot measures with
 MAX(CASE WHEN measure='x' THEN estimate END). Medians (income, rent, home value, age) can never be
 summed or averaged: for Silver Spring as a whole use the place row '2472450'; for a district give the
 range of its tracts' medians and say so. For a district, SUM counts over its tracts
 (geoid IN (SELECT geoid FROM places WHERE LOWER(name)=LOWER('...'))) before dividing.
 Silver Spring's 19 tracts: ({tracts}).

TABLE jobs_by_tract: jobs located in each tract (where people WORK), year 2018 and 2023,
 geography_type 'tract' or 'county' (geoid '24031'). Columns total_jobs, jobs_<sector> for 20 sectors
 (retail, accommodation_food_services, health_care_social_assistance, professional_scientific_technical,
 public_administration, educational_services, ...), jobs_earning_under_1251_month /
 _1251_3333_month / _over_3333_month, jobs_workers_29_or_younger / _30_to_54 / _55_or_older.
 Use it for daytime population, "people who work around here", lunch and weekday customers, the
 local job base and which industries employ people HERE. Residents' own jobs are industry_employment
 and the C24010/C24030 measures; say which one you used.

TABLE business_patterns: establishments by NAICS industry per ZIP code (geography_type 'zip') and for
 Montgomery County ('county', geography_id '24031', which also has employees and payroll), years
 2018-2023. naics '00' is all industries; naics_level 2 = sector, 6 = detailed industry (e.g. 722511
 full-service restaurants, 812111 barber shops, 812112 beauty salons, 713940 fitness centers,
 624410 child day care, 812310 laundromats, 311811 retail bakeries). Use LIKE on naics_label when unsure.
 Silver Spring ZIPs: 20910 (downtown), 20901, 20902, 20903, 20904, 20905, 20906; 20912 is Takoma Park.
 For "per resident", divide by acs_measures population for geography_type='zcta' and the same ZIP
 (use ACS 2020-2024 with business year 2023), and compare with the county.

TABLE county_commercial_permits: commercial building permits (offices, retail, restaurants, apartment
 high-rises) with issued date, worktype (CONSTRUCT = new building, ADD, ALTER...), usecode, description,
 building_area_sqft, valuation, address. Big projects: ORDER BY valuation DESC.
TABLE county_crashes and county_crash_nonmotorists (join on report_number): every police-reported crash
 with date, crash_hour, report_type (Property Damage Crash / Injury Crash / Fatal Crash), light
 (Daylight, Dark - Lighted...), road_name, cross_street; nonmotorists has person_type (Pedestrian,
 Bicyclist...) and injury_severity. Prefer these over community_ped_struck for crash questions.
TABLE county_parking_facilities: county public garages/lots with total_capacity, car_spaces,
 ev_charging_spaces, district ('Silver Spring' ...).
TABLE county_food_businesses: food businesses with first_inspection / latest_inspection dates.
TABLE county_rental_survey: average rent by apartment community, bedroom_type and year (2016-2022),
 with zip. Use AVG over communities only as "average of surveyed communities".
TABLE county_liquor_licenses: current alcohol licensees with zip and license_type.
TABLE county_development_pipeline: projects the Planning Board approved that are not finished:
 project_name, development_type, approved_units, unbuilt_units (homes still to be built),
 multifamily_units, unbuilt_nonres_sqft, office_sqft, retail_sqft, office_jobs, retail_jobs (planners'
 estimates), master_plan, planning_board_date, geoid. Best source for "what is coming" and future growth;
 county_commercial_permits shows what has permits now.
places also holds official county plan areas (kind 'planning area': Long Branch, Takoma/Langley,
 Greater Lyttonsville, Silver Spring East, North and West Silver Spring, Forest Glen - Montgomery Hills
 Corridor, Kemp Mill, University Boulevard, Wheaton CBD and Vicinity, White Oak...), mapped to whole tracts
 like the other named places. Use them the same way.

HOW TO ANSWER WELL. The users are business owners, county staff and students making decisions.
- You may write SEVERAL queries. Put the main query first. Before each extra query write one line
  "-- evidence: <short label of what it shows>". Up to 4 queries in total, no semicolons.
- For a decision, recommendation, "should I", "where", "is it a good idea", needs or planning question,
  add evidence queries for the factors that matter: demand (residents, households, income, age, jobs
  located there), supply (existing competitors, ideally per 1,000 residents), access (stations, bus
  stops, parking), risk (crime, crashes) and trend (change between vintages or years). Pick the 2-3
  that matter most for this question.
- Always give a benchmark so a figure means something: compare the area with Silver Spring overall, the
  county, or a peer place (Bethesda, Rockville, Wheaton, Takoma Park), using the same measure and
  vintage. A rate beats a raw count when areas differ in size.
- A rate's count and population must cover the same area. Listings or records counted over tracts
  are divided by SUM(population_count) from tract_data for those same tracts, never by the official
  place population ('2472450'), which has a different boundary.
- When the area is inside the benchmark (a district or planning area inside Silver Spring), compare
  it with the REST of Silver Spring: the 19 Silver Spring tracts not in the area. Label that row
  'Rest of Silver Spring'. A whole that contains the area is not a fair comparison.
- Give every rate in one answer the same unit, chosen so no rate rounds to 0: per 1,000 residents,
  or per 10,000 residents when a rate is below 0.1 per 1,000. Round rates to 2 decimals.
- Rankings of neighborhoods: rank Silver Spring's areas (the 19 tracts) unless the county is asked for,
  return every ranked area (no LIMIT below 19), and include the measure and its universe.
- If the exact measure is not held but close evidence is (jobs located nearby for foot traffic,
  public parking capacity for parking, child population and child care counts for daycare demand,
  rental survey or Census rent for rents), answer with that evidence and label it in a text column as
  'Closest available evidence: ...'. Reply CANNOT_ANSWER only when no table holds anything relevant.
""".strip()


def short_notes(tables):
    return [SHORT_NOTES[t] for t in tables if t in SHORT_NOTES]
