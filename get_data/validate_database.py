"""Check that the generated database is safe to hand to the API/LLM teammate."""
from .database import connect

def main():
    with connect() as db:
        tracts = db.execute("SELECT COUNT(*) FROM tract_data").fetchone()[0]; geometries = db.execute("SELECT COUNT(*) FROM tract_data WHERE geometry_geojson IS NOT NULL").fetchone()[0]; businesses = db.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        assert tracts >= 200, f"Expected about 232 Montgomery County tracts, got {tracts}"
        assert geometries == tracts, "Every tract needs map geometry"
        assert db.execute("SELECT COUNT(*) FROM tract_data WHERE geoid != CAST(geoid AS TEXT)").fetchone()[0] == 0
    print(f"Validation passed: {tracts} tracts, {geometries} geometries, {businesses} businesses.")
if __name__ == "__main__": main()
