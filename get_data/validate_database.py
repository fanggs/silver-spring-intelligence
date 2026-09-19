"""Check that the generated database is safe to hand to the API/LLM teammate."""
from .database import connect

def main():
    with connect() as db:
        tracts = db.execute("SELECT COUNT(*) FROM tract_data").fetchone()[0]; geometries = db.execute("SELECT COUNT(*) FROM tract_data WHERE geometry_geojson IS NOT NULL").fetchone()[0]; businesses = db.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        assert tracts >= 200, f"Expected about 232 Montgomery County tracts, got {tracts}"
        assert geometries == tracts, "Every tract needs map geometry"
        assert db.execute("SELECT COUNT(*) FROM tract_data WHERE geoid != CAST(geoid AS TEXT)").fetchone()[0] == 0
        required_metric_gaps = db.execute("""
            SELECT COUNT(*) FROM tract_data
            WHERE population_count IS NULL
               OR foreign_born_count IS NULL
               OR median_income IS NULL
               OR language_population_age_5_plus_count IS NULL
               OR english_only_at_home_count IS NULL
               OR non_english_at_home_count IS NULL
               OR occupied_housing_units_count IS NULL
               OR renter_occupied_housing_units_count IS NULL
               OR total_commuter_count IS NULL
               OR public_transit_commuter_count IS NULL
        """).fetchone()[0]
        assert required_metric_gaps == 0, f"{required_metric_gaps} tracts have missing required metrics"
        invalid_language_counts = db.execute("""
            SELECT COUNT(*) FROM tract_data
            WHERE english_only_at_home_count < 0
               OR non_english_at_home_count < 0
               OR language_population_age_5_plus_count != english_only_at_home_count + non_english_at_home_count
        """).fetchone()[0]
        assert invalid_language_counts == 0, "Language counts must be non-negative and add up to the age-5-plus total"
        invalid_businesses = db.execute("""
            SELECT COUNT(*) FROM businesses
            WHERE geoid IS NULL
               OR source_url NOT LIKE 'https://www.openstreetmap.org/%/%'
        """).fetchone()[0]
        assert businesses > 0, "Expected at least one business location"
        assert invalid_businesses == 0, f"{invalid_businesses} businesses are missing tract links or object URLs"
    print(f"Validation passed: {tracts} tracts, {geometries} geometries, {businesses} businesses.")
if __name__ == "__main__": main()
