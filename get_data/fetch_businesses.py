"""Load named OpenStreetMap listings with their original venue types."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import math

import requests

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
QUERY = '[out:json][timeout:90];(nwr["amenity"](38.93,-77.55,39.35,-76.90);nwr["shop"](38.93,-77.55,39.35,-76.90););out center tags;'
HEADERS = {"Accept": "application/json", "User-Agent": "CivicLens-BayHacks/1.0"}
FOOD_AND_DRINK = {"restaurant", "fast_food", "cafe", "bar", "pub", "biergarten", "ice_cream", "food_court"}


def osm_elements(payload):
    if not isinstance(payload, dict) or payload.get("remark") or not isinstance(payload.get("elements"), list) or not payload["elements"]:
        raise ValueError("OpenStreetMap returned an empty or incomplete result.")
    return payload["elements"]


def fetch_osm(query):
    failures = []
    for url in OVERPASS_URLS:
        try:
            response = requests.get(url, params={"data": query}, headers=HEADERS, timeout=100)
            response.raise_for_status()
            payload = response.json()
            osm_elements(payload)
            return payload
        except (requests.RequestException, ValueError) as error:
            failures.append(f"{url}: {type(error).__name__}")
    raise RuntimeError("OpenStreetMap download failed. " + " | ".join(failures))


def fetch_businesses(payload=None):
    if payload is None:
        payload = fetch_osm(QUERY)
    now = datetime.now(timezone.utc).isoformat()
    points = []
    for item in osm_elements(payload):
        tags = item.get("tags", {})
        latitude = item.get("lat", item.get("center", {}).get("lat"))
        longitude = item.get("lon", item.get("center", {}).get("lon"))
        name = tags.get("name", "").strip()
        amenity, shop = tags.get("amenity"), tags.get("shop")
        if not name or latitude is None or longitude is None or not (amenity or shop):
            continue
        kind, osm_id = item["type"], item["id"]
        group = "food and drink" if amenity in FOOD_AND_DRINK else "retail" if shop else "other amenity"
        points.append({
            "business_id": hashlib.sha1(f"{kind}:{osm_id}".encode()).hexdigest(),
            "name": name, "category": (amenity or shop).replace("_", " "),
            "category_group": group, "osm_amenity": amenity, "osm_shop": shop,
            "address": " ".join(filter(None, (tags.get("addr:housenumber"), tags.get("addr:street")))),
            "latitude": latitude, "longitude": longitude,
            "source_name": "OpenStreetMap via Overpass API",
            "source_url": f"https://www.openstreetmap.org/{kind}/{osm_id}",
            "duplicate_source_url": None, "last_checked_at": now,
        })
    return points


def distance_meters(a, b):
    north = (a["latitude"] - b["latitude"]) * 111_195
    east = (a["longitude"] - b["longitude"]) * 111_195 * math.cos(math.radians(a["latitude"]))
    return math.hypot(north, east)


def deduplicate_businesses(rows):
    """Merge unambiguous nearby node/area pairs and retain both OSM object URLs."""
    groups = defaultdict(list)
    for row in rows:
        groups[(" ".join(row["name"].casefold().split()), row["osm_amenity"],
                row["osm_shop"], row["geoid"])].append(row)
    merged = []
    for group in groups.values():
        if len(group) == 2:
            node, area = sorted(group, key=lambda row: row["source_url"])
            different_addresses = node["address"] and area["address"] and node["address"].casefold() != area["address"].casefold()
            if ("/node/" in node["source_url"] and "/node/" not in area["source_url"]
                    and not different_addresses and distance_meters(node, area) <= 50):
                primary = area if area["address"] and not node["address"] else node
                alternate = node if primary is area else area
                merged.append({**primary, "duplicate_source_url": alternate["source_url"]})
                continue
        merged.extend(group)
    return merged
