"""Build and validate a new CivicLens snapshot before replacing the current file."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import DATABASE_PATH
from .database import connect, initialize_database
from .fetch_businesses import deduplicate_businesses, fetch_businesses
from .fetch_census import CDP_GEOID, TRACT_SOURCE_URL, fetch_census_tracts, fetch_industry_employment, fetch_place_data
from .fetch_geometry import URL as GEOMETRY_URL, fetch_tract_geometry
from .fetch_places import CBD_SOURCE_URL, OSM_SOURCE_URL, assign_tracts, downtown_district_rows, fetch_osm_places, point_in_geometry
from .fetch_trends import fetch_earlier_tracts
from .validate_database import validate_database

CURRENT_VINTAGE = "ACS 2020-2024"


def assign_business_tracts(businesses, tract_shapes):
    prepared = []
    for geoid, tract in tract_shapes.items():
        geometry = json.loads(tract["geometry_geojson"])
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        points = [point for polygon in polygons for ring in polygon for point in ring]
        longitudes, latitudes = zip(*points)
        prepared.append((geoid, geometry, min(longitudes), max(longitudes), min(latitudes), max(latitudes)))
    matched = []
    for business in businesses:
        lon, lat = business["longitude"], business["latitude"]
        for geoid, geometry, min_lon, max_lon, min_lat, max_lat in prepared:
            if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat and point_in_geometry(lon, lat, geometry):
                matched.append({**business, "geoid": geoid})
                break
    return matched


def insert_rows(db, table, rows):
    columns = list(rows[0])
    db.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                   ([row[column] for column in columns] for row in rows))


def build_snapshot(output, *, business_payload=None, place_payload=None, partner_warehouse=None, partner_geometry=None):
    now = datetime.now(timezone.utc).isoformat()
    census = fetch_census_tracts()
    earlier = fetch_earlier_tracts()
    place_data = fetch_place_data()
    industry = fetch_industry_employment()
    geometry = fetch_tract_geometry()
    census_ids = {row["geoid"] for row in census}
    if census_ids != geometry.keys() or census_ids != {row["geoid"] for row in earlier}:
        raise RuntimeError("Current ACS, earlier ACS and TIGER must cover the same tract IDs.")
    businesses = deduplicate_businesses(assign_business_tracts(fetch_businesses(business_payload), geometry))
    places = downtown_district_rows()
    geometry_rows = [(geoid, row["geometry_geojson"]) for geoid, row in geometry.items()]
    for place in assign_tracts(fetch_osm_places(place_payload), geometry_rows):
        places.append({**{key: place[key] for key in ("name", "kind", "geoid", "latitude", "longitude")},
                       "precision": "tract containing the OpenStreetMap place point",
                       "source_name": "OpenStreetMap",
                       "source_url": f"https://www.openstreetmap.org/{place['osm_type']}/{place['osm_id']}"})
    unique_places = {(row["name"], row["kind"], row["geoid"]): row for row in places}
    history_columns = set(earlier[0])
    history = []
    for records, end in ((earlier, 2020), (census, 2024)):
        for row in records:
            history.append({**{key: row[key] for key in sorted(history_columns)},
                            "period_start": end - 4, "period_end": end,
                            "acs_vintage": f"ACS {end - 4}-{end}", "retrieved_at": now,
                            "source_url": f"https://data.census.gov/table/ACSDT5Y{end}.B01003"})
    official_url = f"{TRACT_SOURCE_URL}?g=160XX00US{CDP_GEOID}"
    sources = [
        ("acs_2024", CURRENT_VINTAGE, "U.S. Census Bureau", TRACT_SOURCE_URL, "Montgomery County tracts", "2024", now, "ACS estimates by tract"),
        ("acs_2020", "ACS 2016-2020", "U.S. Census Bureau", "https://data.census.gov/table/ACSDT5Y2020.B01003", "Montgomery County tracts", "2020", now, "Comparable earlier tract estimates"),
        ("acs_place_2024", CURRENT_VINTAGE, "U.S. Census Bureau", official_url, "Silver Spring CDP", "2024", now, "Published estimates for the official Census place boundary"),
        ("acs_industry_2024", CURRENT_VINTAGE, "U.S. Census Bureau", "https://data.census.gov/table/ACSDT5Y2024.C24050", "Montgomery County tracts and Silver Spring CDP", "2024", now, "Industry of employed residents age 16 and older"),
        ("tract_geometry_2024", "Census tract boundaries", "U.S. Census Bureau", GEOMETRY_URL, "Montgomery County tracts", "2024", now, "Generalized tract geometry"),
        ("osm_businesses", "Mapped businesses and amenities", "OpenStreetMap", OSM_SOURCE_URL, "Montgomery County tracts", now[:10], now, "Named OSM listings. Coverage depends on contributions."),
        ("osm_places", "Named places", "OpenStreetMap", OSM_SOURCE_URL, "Silver Spring area", now[:10], now, "Place points mapped to the tract that contains them."),
        ("mncppc_cbd", "Downtown district tract mappings", "Montgomery County Planning / OpenStreetMap", CBD_SOURCE_URL, "Downtown Silver Spring", "2022", now, "Tract approximations for districts, not district boundaries."),
    ]
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".civiclens-build-", dir=output.parent) as directory:
        candidate = Path(directory) / "snapshot.db"
        initialize_database(candidate)
        with closing(connect(candidate)) as db, db:
            insert_rows(db, "tract_data", [
                {**row, **geometry[row["geoid"]], "acs_vintage": CURRENT_VINTAGE,
                 "source_url": TRACT_SOURCE_URL, "retrieved_at": now} for row in census
            ])
            insert_rows(db, "tract_history", history)
            insert_rows(db, "businesses", businesses)
            insert_rows(db, "places", [{**row, "retrieved_at": now} for row in unique_places.values()])
            insert_rows(db, "place_data", [{**place_data, "acs_vintage": CURRENT_VINTAGE,
                                           "source_url": official_url, "retrieved_at": now}])
            insert_rows(db, "industry_employment", [{**row, "acs_vintage": CURRENT_VINTAGE,
                                                     "retrieved_at": now} for row in industry])
            db.executemany("INSERT INTO data_sources VALUES (?,?,?,?,?,?,?,?)", sources)
        # Import from the preserved partner snapshot after the primary rebuild.
        # A failure here still leaves the active output untouched.
        bundled = Path(__file__).resolve().parents[1] / 'data' / 'partner_snapshot'
        partner_warehouse = partner_warehouse or (bundled / 'community.duckdb' if (bundled / 'community.duckdb').exists() else None)
        partner_geometry = partner_geometry or (bundled / 'tracts.geojson' if (bundled / 'tracts.geojson').exists() else None)
        if bool(partner_warehouse) != bool(partner_geometry):
            raise ValueError('Both partner database and matching geometry are required')
        if partner_warehouse:
            from .import_community import import_community
            import_community(partner_warehouse, partner_geometry, candidate, candidate)
        demographic_snapshot = Path(__file__).resolve().parents[1] / 'data/demographics_2024.json'
        if demographic_snapshot.exists():
            from .import_demographics import import_demographics
            import_demographics(json.loads(demographic_snapshot.read_text()), candidate, candidate)
        # Addresses and map locations for every listed place, after all the
        # place tables exist. Reads OpenStreetMap like the business fetch.
        from .fetch_addresses import build_locations
        build_locations(candidate)
        validate_database(candidate)
        candidate.replace(output)
    print(f"Saved validated database to {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DATABASE_PATH)
    parser.add_argument("--osm-businesses-json", type=Path, help="Use a saved full Overpass businesses response")
    parser.add_argument("--osm-places-json", type=Path, help="Use a saved full Overpass place-names response")
    parser.add_argument('--partner-warehouse', type=Path, help='Replacement partner snapshot (bundled snapshot is used by default)')
    parser.add_argument('--partner-geometry', type=Path, help='Matching original partner tract GeoJSON')
    args = parser.parse_args()
    build_snapshot(args.output,
                   business_payload=json.loads(args.osm_businesses_json.read_text()) if args.osm_businesses_json else None,
                   place_payload=json.loads(args.osm_places_json.read_text()) if args.osm_places_json else None,
                   partner_warehouse=args.partner_warehouse, partner_geometry=args.partner_geometry)


if __name__ == "__main__":
    main()
