"""Verify a complete CivicLens database snapshot before serving it."""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from .config import DATABASE_PATH
from .database import connect
from .fetch_census import CDP_GEOID, INDUSTRIES
from .fetch_places import downtown_district_rows


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_database(path=DATABASE_PATH):
    with closing(connect(path, readonly=True)) as db:
        require([r[0] for r in db.execute("PRAGMA integrity_check")] == ["ok"], "SQLite integrity check failed")
        required_tables = {"tract_data", "tract_history", "businesses", "places", "place_data", "industry_employment", "data_sources"}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require(required_tables <= tables, f"Missing required tables: {', '.join(sorted(required_tables - tables))}")
        require(not db.execute("PRAGMA foreign_key_check").fetchone(), "Unlinked records in snapshot")
        tract_ids = {r[0] for r in db.execute("SELECT geoid FROM tract_data")}
        require(len(tract_ids) >= 200 and all(len(g) == 11 and g.startswith("24031") and g.isdigit() for g in tract_ids),
                "Expected complete Montgomery County tract coverage")
        require(not db.execute("""
            SELECT 1 FROM tract_data WHERE geometry_geojson IS NULL OR NOT json_valid(geometry_geojson)
                OR population_count IS NULL OR female_count IS NULL OR male_count IS NULL
                OR population_count != female_count + male_count OR female_count < 0 OR male_count < 0
                OR foreign_born_count IS NULL OR median_income IS NULL
                OR occupied_housing_units_count IS NULL OR renter_occupied_housing_units_count IS NULL
                OR renter_occupied_housing_units_count > occupied_housing_units_count
                OR total_commuter_count IS NULL OR public_transit_commuter_count IS NULL
                OR public_transit_commuter_count > total_commuter_count
                OR english_only_at_home_count IS NULL OR non_english_at_home_count IS NULL
                OR language_population_age_5_plus_count IS NULL
                OR english_only_at_home_count < 0 OR non_english_at_home_count < 0
                OR language_population_age_5_plus_count != english_only_at_home_count + non_english_at_home_count
        """).fetchone(), "Missing or inconsistent current tract data")
        history = {(r[0], r[1], r[2]) for r in db.execute("SELECT geoid, period_start, period_end FROM tract_history")}
        require(history == {(g, start, end) for g in tract_ids for start, end in ((2016, 2020), (2020, 2024))},
                "Tract history must include both vintages for every tract")
        require(not db.execute("SELECT 1 FROM tract_history WHERE population_count IS NULL OR total_commuter_count IS NULL OR source_url IS NULL").fetchone(),
                "Missing required historical estimates or provenance")
        current_history = db.execute("""SELECT 1 FROM tract_history h JOIN tract_data t USING (geoid)
            WHERE h.period_end = 2024 AND (h.population_count IS NOT t.population_count
                OR h.public_transit_commuter_count IS NOT t.public_transit_commuter_count
                OR h.median_income IS NOT t.median_income)""").fetchone()
        require(not current_history, "Current history differs from current tract estimates")
        districts = {(r[0], r[1]) for r in db.execute("SELECT name, geoid FROM places WHERE kind='district'")}
        require({(r["name"], r["geoid"]) for r in downtown_district_rows()} <= districts,
                "Missing downtown district mappings")
        require(db.execute("SELECT 1 FROM places WHERE kind != 'district' AND latitude IS NOT NULL AND longitude IS NOT NULL").fetchone(),
                "Missing OpenStreetMap neighborhood mappings")
        place = db.execute("SELECT population_count, female_count, male_count, population_margin_of_error, female_margin_of_error FROM place_data WHERE geoid=?", (CDP_GEOID,)).fetchone()
        require(place is not None and place[0] > 0 and min(place[1], place[2]) >= 0 and place[0] == place[1] + place[2]
                and all(value is not None and value >= 0 for value in place[3:]),
                "Missing or inconsistent official Silver Spring place estimates")
        expected = {("tract", g, i) for g in tract_ids for i in INDUSTRIES} | {("place", CDP_GEOID, i) for i in INDUSTRIES}
        actual = {tuple(r) for r in db.execute("SELECT geography_type, geoid, industry FROM industry_employment")}
        require(actual == expected, "Missing or unexpected industry/geography pairs")
        require(not db.execute("""SELECT 1 FROM industry_employment WHERE employed_residents IS NULL OR employed_residents < 0
                OR margin_of_error IS NULL OR margin_of_error < 0""").fetchone(), "Missing industry estimates or margins of error")
        require(not db.execute("""SELECT 1 FROM industry_employment GROUP BY geography_type, geoid
            HAVING SUM(CASE WHEN industry != 'All industries' THEN employed_residents ELSE 0 END)
                != MAX(CASE WHEN industry = 'All industries' THEN employed_residents END)""").fetchone(),
                "Industry counts do not add up to employed residents")
        count = db.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        require(count > 0 and not db.execute("""
            SELECT 1 FROM businesses WHERE (osm_amenity IS NULL AND osm_shop IS NULL)
                OR category != replace(coalesce(osm_amenity, osm_shop), '_', ' ')
                OR source_url NOT LIKE 'https://www.openstreetmap.org/%/%'
        """).fetchone(), "Missing businesses or lost original OpenStreetMap types")
        sources = {r[0] for r in db.execute("SELECT dataset_id FROM data_sources")}
        require({"acs_2024", "acs_2020", "acs_place_2024", "acs_industry_2024", "tract_geometry_2024", "osm_businesses", "osm_places", "mncppc_cbd"} <= sources,
                "Incomplete source registry")
    if 'community_sources' in tables:
        from .import_community import validate_community
        validate_community(path)
    if 'demographic_estimates' in tables:
        from .import_demographics import validate_demographics
        validate_demographics(path)
    print(f"Validation passed: {len(tract_ids)} tracts, {count} listings, official place estimates, industry, neighborhoods and both historical vintages.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", type=Path, default=DATABASE_PATH)
    validate_database(parser.parse_args().database)


if __name__ == "__main__":
    main()
