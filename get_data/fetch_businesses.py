"""Fetch public OpenStreetMap business points across Montgomery County."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib

import requests

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Covers the Montgomery County, MD / Silver Spring study area.  The frontend
# can later select a smaller neighborhood such as Fenton Village for a demo.
QUERY = '[out:json][timeout:180];(nwr["amenity"](38.93,-77.55,39.35,-76.90);nwr["shop"](38.93,-77.55,39.35,-76.90););out center tags;'
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "text/plain; charset=utf-8",
    "User-Agent": "CivicLens-BayHacks/1.0 (community-intelligence demo)",
}

def category(tags):
    value = tags.get("amenity") or tags.get("shop") or "other"
    if value in {"restaurant", "fast_food", "cafe", "bar", "pub"}: return "restaurant"
    if value in {"supermarket", "convenience", "greengrocer", "bakery"}: return "grocery"
    if value in {"kindergarten", "childcare"}: return "childcare"
    return value.replace("_", " ")

def fetch_businesses():
    response = None
    errors = []
    for url in OVERPASS_URLS:
        try:
            candidate = requests.post(url, data=QUERY, headers=HEADERS, timeout=240)
            candidate.raise_for_status()
            response = candidate
            break
        except requests.RequestException as error:
            errors.append(f"{url}: {error}")
    if response is None:
        raise RuntimeError("OpenStreetMap business download failed. " + " | ".join(errors))

    now = datetime.now(timezone.utc).isoformat()
    points = []
    for item in response.json().get("elements", []):
        tags = item.get("tags", {}); latitude = item.get("lat") or item.get("center", {}).get("lat"); longitude = item.get("lon") or item.get("center", {}).get("lon")
        if not tags.get("name") or latitude is None or longitude is None: continue
        item_type = item.get("type")
        item_id = item.get("id")
        identity = f"{item_type}:{item_id}"
        points.append({"business_id": hashlib.sha1(identity.encode()).hexdigest(), "name": tags["name"], "category": category(tags), "address": " ".join(filter(None,[tags.get("addr:housenumber"),tags.get("addr:street")])), "latitude": latitude, "longitude": longitude, "source_name": "OpenStreetMap via Overpass API", "source_url": f"https://www.openstreetmap.org/{item_type}/{item_id}", "last_checked_at": now})
    return points
