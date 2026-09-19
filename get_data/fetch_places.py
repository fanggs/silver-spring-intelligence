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


# Montgomery County's downtown sector plan names seven districts, and no
# agency publishes their boundaries - not OpenStreetMap, not the county GIS,
# not the planning GIS, not ArcGIS Online. They exist as figures in a PDF.
#
# So we locate each one from two sources that DO exist:
#   1. its namesake street or landmark in OpenStreetMap, and
#   2. the official Silver Spring Central Business District polygon from
#      Montgomery County Planning, used to clip the street so that Fenton
#      Street running south to Takoma Park doesn't drag the district with it.
# The two districts with no namesake are split from the CBD at the Metro
# station's latitude, which is what their names describe.
#
# The result is a tract assignment, not a boundary. We say so in `precision`
# rather than implying the Census measures these districts separately - it
# does not, and a tract is the finest level it publishes.
DOWNTOWN_PLAN_URL = (
    "https://montgomeryplanning.org/planning/communities/"
    "silver-spring/silver-spring-downtown-and-adjacent-communities-plan/"
)
CBD_SOURCE_URL = (
    "https://montgomeryplans.org/server/rest/services/Overlays/"
    "Central_Business_Districts_CBD/FeatureServer/0"
)

# district -> (tracts, how it was located)
DOWNTOWN_DISTRICTS = {
    "Fenton Village": (
        ["24031702402", "24031702502", "24031702503"],
        "Fenton Street within the official Silver Spring CBD"),
    "Ellsworth": (
        ["24031702503"],
        "Ellsworth Drive within the official Silver Spring CBD"),
    "Ripley District": (
        ["24031702501"],
        "Ripley Street within the official Silver Spring CBD"),
    "Falklands": (
        ["24031702604"],
        "East and West Falkland Lane within the official Silver Spring CBD"),
    "Metro Center": (
        ["24031702501"],
        "the Silver Spring Metro station"),
    "Downtown North": (
        ["24031702503"],
        "the part of the official CBD north of the Metro station"),
    "South Silver Spring": (
        ["24031702502", "24031702501"],
        "the part of the official CBD south of the Metro station"),
}


def downtown_district_rows():
    rows = []
    for name, (geoids, how) in DOWNTOWN_DISTRICTS.items():
        for geoid in geoids:
            rows.append({
                "name": name,
                "kind": "district",
                "geoid": geoid,
                "latitude": None,
                "longitude": None,
                "precision": f"tract containing {how}",
                "source_name": "Montgomery County Planning / OpenStreetMap",
                "source_url": CBD_SOURCE_URL,
            })
    return rows
