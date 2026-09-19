"""
Build a gazetteer of named places in Silver Spring.

The Census publishes tracts, not neighbourhoods, so a question about
"Woodside" has nothing to join to. OpenStreetMap carries the local names, so
we pull them and resolve each to the tract it sits in. Every row keeps its
source, the same as the rest of the database.
"""
from __future__ import annotations
import json
import urllib.parse
import urllib.request

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_SOURCE_URL = "https://www.openstreetmap.org/copyright"
USER_AGENT = "CivicLens/1.0 (Bay Hacks student project)"

# The Silver Spring CDP bounding box, padded slightly so places that sit on
# the edge still come back and can be tested properly.
BBOX = (38.9750, -77.0700, 39.0250, -76.9650)

QUERY = """[out:json][timeout:90];
(
  node["place"](%f,%f,%f,%f);
  way["place"](%f,%f,%f,%f);
);
out center tags;""" % (BBOX + BBOX)


def fetch_osm_places():
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    req = urllib.request.Request(
        OVERPASS_URL, data=body, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.load(resp)

    places = []
    for el in payload.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        kind = tags.get("place")
        if not name or not kind:
            continue
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        places.append({
            "name": name,
            "kind": kind,
            "latitude": float(lat),
            "longitude": float(lon),
            "osm_type": el["type"],
            "osm_id": el["id"],
        })
    return places


def point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1) + x1):
            inside = not inside
    return inside


def point_in_geometry(lon, lat, geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if point_in_ring(lon, lat, poly[0]) and not any(
            point_in_ring(lon, lat, hole) for hole in poly[1:]
        ):
            return True
    return False


def assign_tracts(places, tract_rows):
    """Give each place the tract it falls inside. Drop the ones that miss."""
    located = []
    for p in places:
        for geoid, geom_json in tract_rows:
            try:
                geom = json.loads(geom_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if point_in_geometry(p["longitude"], p["latitude"], geom):
                located.append({**p, "geoid": geoid})
                break
    return located


# Montgomery County's downtown sector plan names districts that no mapping
# dataset carries as boundaries: Ellsworth, Ripley, Metro Center and the
# rest. They are all inside downtown Silver Spring, and the Census publishes
# at tract level, so they resolve to the downtown tracts rather than to a
# boundary of their own. Marked 'district' so an answer can say so plainly
# instead of implying a precision the data does not have.
DOWNTOWN_TRACTS = ["24031702502", "24031702503", "24031702501", "24031702402"]
DOWNTOWN_PLAN_URL = (
    "https://montgomeryplanning.org/planning/communities/"
    "silver-spring/silver-spring-downtown-and-adjacent-communities-plan/"
)
DOWNTOWN_DISTRICTS = [
    "Fenton Village", "Ellsworth", "Ripley District", "Metro Center",
    "Downtown North", "South Silver Spring", "Falklands",
]


def downtown_district_rows():
    rows = []
    for name in DOWNTOWN_DISTRICTS:
        for geoid in DOWNTOWN_TRACTS:
            rows.append({
                "name": name,
                "kind": "district",
                "geoid": geoid,
                "latitude": None,
                "longitude": None,
                "precision": "downtown tracts",
                "source_name": "Montgomery County Planning Department",
                "source_url": DOWNTOWN_PLAN_URL,
            })
    return rows
