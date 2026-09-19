"""Run the complete CivicLens data pipeline and create data/silverspring.db."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from .database import connect, initialize_database
from .fetch_census import TRACT_SOURCE_URL, fetch_census_tracts
from .fetch_geometry import URL as GEOMETRY_URL, fetch_tract_geometry
from .fetch_businesses import fetch_businesses


def _point_in_ring(longitude: float, latitude: float, ring: list[list[float]]) -> bool:
    """Return whether a longitude/latitude point is inside one GeoJSON ring."""
    inside = False
    for index in range(len(ring)):
        previous_longitude, previous_latitude = ring[index - 1]
        current_longitude, current_latitude = ring[index]
        crosses_latitude = (current_latitude > latitude) != (previous_latitude > latitude)
        if crosses_latitude:
            intersection = (previous_longitude - current_longitude) * (latitude - current_latitude)
            intersection /= previous_latitude - current_latitude
            if longitude < current_longitude + intersection:
                inside = not inside
    return inside


def _point_in_geometry(longitude: float, latitude: float, geometry: dict) -> bool:
    """Check a point against Polygon or MultiPolygon GeoJSON, including holes."""
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    for polygon in polygons:
        if _point_in_ring(longitude, latitude, polygon[0]) and not any(
            _point_in_ring(longitude, latitude, hole) for hole in polygon[1:]
        ):
            return True
    return False


def assign_business_tracts(businesses: list[dict], tract_shapes: dict[str, dict]) -> list[dict]:
    """Keep only businesses inside a downloaded Montgomery County tract."""
    prepared = []
    for geoid, tract in tract_shapes.items():
        geometry = json.loads(tract["geometry_geojson"])
        coordinates = geometry["coordinates"]
        all_points = [point for polygon in (coordinates if geometry["type"] == "MultiPolygon" else [coordinates]) for ring in polygon for point in ring]
        longitudes = [point[0] for point in all_points]
        latitudes = [point[1] for point in all_points]
        prepared.append((geoid, geometry, min(longitudes), max(longitudes), min(latitudes), max(latitudes)))

    matched = []
    for business in businesses:
        longitude, latitude = business["longitude"], business["latitude"]
        for geoid, geometry, min_lon, max_lon, min_lat, max_lat in prepared:
            if min_lon <= longitude <= max_lon and min_lat <= latitude <= max_lat and _point_in_geometry(longitude, latitude, geometry):
                business["geoid"] = geoid
                matched.append(business)
                break
    return matched


def main():
    census = fetch_census_tracts()
    geometry = fetch_tract_geometry()
    businesses = assign_business_tracts(fetch_businesses(), geometry)
    now = datetime.now(timezone.utc).isoformat()
    initialize_database()
    with connect() as db:
        db.execute("DELETE FROM tract_data"); db.execute("DELETE FROM businesses"); db.execute("DELETE FROM data_sources")
        for tract in census:
            shape = geometry.get(tract["geoid"])
            if not shape: continue
            tract.update(shape); tract.update({"acs_vintage":"2024 ACS 5-year", "source_url":TRACT_SOURCE_URL, "retrieved_at":now})
            columns = list(tract); db.execute(f"INSERT INTO tract_data ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [tract[column] for column in columns])
        for business in businesses:
            columns = list(business); db.execute(f"INSERT OR REPLACE INTO businesses ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [business[column] for column in columns])
        db.executemany("INSERT INTO data_sources VALUES (?,?,?,?,?,?,?,?)", [("acs_2024","ACS 2024 5-year","U.S. Census Bureau",TRACT_SOURCE_URL,"All Montgomery County census tracts","2024",now,"Tract-level demographic estimates, including the C16001 tract-level language table"),("tract_geometry_2024","Census tract boundaries","U.S. Census Bureau",GEOMETRY_URL,"All Montgomery County census tracts","2024",now,"Generalized Census tract geometry"),("osm_businesses","Business locations","OpenStreetMap", "https://www.openstreetmap.org","Businesses inside Montgomery County census tracts",now[:10],now,"Coverage depends on OpenStreetMap contributions")])
    print(f"Saved {len(census)} ACS records, {len(geometry)} tract polygons, and {len(businesses)} business points.")
if __name__ == "__main__": main()
