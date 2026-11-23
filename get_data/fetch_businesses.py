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
BOX = "(38.93,-77.55,39.35,-76.90)"
# Business-like leisure and tourism values only: a playground or a park pitch
# is not a business, a gym or a hotel is. Every named office and craft counts.
LEISURE = ("fitness_centre", "sports_centre", "sports_hall", "dance", "bowling_alley", "escape_game",
           "amusement_arcade", "adult_gaming_centre", "ice_rink", "trampoline_park", "climbing", "golf_course",
           "miniature_golf", "swimming_pool", "sauna", "water_park", "hackerspace", "horse_riding")
TOURISM = ("hotel", "motel", "hostel", "guest_house", "museum", "gallery", "theme_park", "zoo", "aquarium")
EXTRA_QUERY_PARTS = (f'nwr["leisure"~"^({"|".join(LEISURE)})$"]{BOX};nwr["office"]{BOX};'
                     f'nwr["tourism"~"^({"|".join(TOURISM)})$"]{BOX};nwr["craft"]{BOX};')
QUERY = f'[out:json][timeout:90];(nwr["amenity"]{BOX};nwr["shop"]{BOX};{EXTRA_QUERY_PARTS});out center tags;'
EXTRA_QUERY = f'[out:json][timeout:90];({EXTRA_QUERY_PARTS});out center tags;'
TAG_KEYS = ("amenity", "shop", "leisure", "tourism", "office", "craft")
GROUPS = {"leisure": "recreation and fitness", "tourism": "lodging and attractions", "office": "office",
          "craft": "trades and crafts"}
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
        found = {key: tags.get(key) for key in TAG_KEYS}
        if found["leisure"] not in LEISURE:
            found["leisure"] = None
        if found["tourism"] not in TOURISM:
            found["tourism"] = None
        # The first tag in TAG_KEYS order is the listing's type, so a place
        # that is both a shop and an office stays a shop, as before.
        key = next((k for k in TAG_KEYS if found[k]), None)
        if not name or latitude is None or longitude is None or not key:
            continue
        kind, osm_id = item["type"], item["id"]
        amenity, shop = found["amenity"], found["shop"]
        group = ("food and drink" if amenity in FOOD_AND_DRINK else "retail" if shop
                 else "other amenity" if amenity else GROUPS[key])
        points.append({
            "business_id": hashlib.sha1(f"{kind}:{osm_id}".encode()).hexdigest(),
            "name": name, "category": found[key].replace("_", " "),
            "category_group": group, "osm_amenity": amenity, "osm_shop": shop,
            "osm_leisure": found["leisure"] if key == "leisure" else None,
            "osm_office": found["office"] if key == "office" else None,
            "osm_tourism": found["tourism"] if key == "tourism" else None,
            "osm_craft": found["craft"] if key == "craft" else None,
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
        groups[(" ".join(row["name"].casefold().split()), row["category"],
                row["category_group"], row["geoid"])].append(row)
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
