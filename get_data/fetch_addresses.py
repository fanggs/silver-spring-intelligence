"""Build place_locations: one row per mapped place, with its street address.

python -m get_data.fetch_addresses [--db PATH]

Every OpenStreetMap place referenced by businesses, community_businesses and
community_venues gets one row, keyed by its OSM reference (node/123):

  latitude, longitude  from the snapshot tables, so map pins stay where the
                       counts put them
  address              from OpenStreetMap, in this order:
    (own tags come from the main OpenStreetMap API by id; the building
    lookup needs Overpass and is skipped, not retried for long, when the
    public Overpass servers are down - re-run later to fill those in)
    'listing'   the place's own current addr:* tags, or the primary
                snapshot's street address when current tags have none
    'building'  the addr:* tags of the building outline the point sits inside
    (none)      address stays NULL; the place is still mappable by coordinates

Nothing is guessed: no nearest-address matching and no paid geocoder. Places
without any mapped address keep a NULL address and say so on screen.

The answer API reads this table to attach addresses and map locations to
listed places in code, so the model never has to select or join addresses.
Writes a temporary copy, validates it, and replaces the database only on
success. Re-running replaces the table; other tables are untouched.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import time

import requests

from .config import DATABASE_PATH
from .fetch_businesses import HEADERS, OVERPASS_URLS
from .validate_database import validate_database

OSM_URL = "https://www.openstreetmap.org/"
SCHEMA = """
CREATE TABLE place_locations (
    osm_ref        TEXT PRIMARY KEY,           -- node/123, way/456 or relation/789
    source_url     TEXT NOT NULL,              -- https://www.openstreetmap.org/<osm_ref>
    name           TEXT,
    latitude       REAL NOT NULL,
    longitude      REAL NOT NULL,
    address        TEXT,                       -- NULL when OpenStreetMap has none
    address_basis  TEXT CHECK (address_basis IN ('listing', 'building')),
    retrieved_at   TEXT NOT NULL
);
"""


def osm_ref(value):
    """'node/123' from an OSM URL or an osm_id; None for anything else."""
    match = re.search(r"(?:^|openstreetmap\.org/)((?:node|way|relation)/\d+)$", str(value or "").strip())
    return match.group(1) if match else None


def format_address(tags):
    """Street address from addr:* tags, using only the parts that are tagged."""
    number, street = tags.get("addr:housenumber"), tags.get("addr:street")
    if not number or not street:
        return None
    parts = [f"{number} {street}"]
    if tags.get("addr:unit"):
        parts.append(f"Unit {tags['addr:unit']}")
    locality = ", ".join(filter(None, (tags.get("addr:city"), tags.get("addr:state"))))
    tail = " ".join(filter(None, (locality, tags.get("addr:postcode"))))
    if tail:
        parts.append(tail)
    return ", ".join(parts)


BUSY = {429, 500, 502, 503, 504}
OSM_API = "https://api.openstreetmap.org/api/0.6/"


def log(message):
    print(message, flush=True)


def overpass(query, attempts=2, wait=5, timeout=75):
    """POST a spatial query to Overpass. Fails fast when every server is busy.

    Overpass is only needed for the building lookup, which is optional, so a
    busy network costs minutes, not an hour: each server gets `attempts`
    tries with a short pause, then the caller skips the batch.
    """
    failures = []
    for url in OVERPASS_URLS:
        for attempt in range(attempts):
            try:
                response = requests.post(url, data={"data": query}, headers=HEADERS, timeout=timeout)
            except requests.RequestException as error:
                failures.append(f"{url}: {type(error).__name__}")
                break  # Unreachable or too slow: the next server.
            if response.status_code in BUSY:
                failures.append(f"{url}: HTTP {response.status_code}")
                time.sleep(wait * (attempt + 1))
                continue
            try:
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                failures.append(f"{url}: {type(error).__name__}")
                break
            remark = payload.get("remark") or ""
            if "error" in remark.lower() or "timed out" in remark.lower():
                failures.append(f"{url}: {remark[:80]}")
                break  # A partial result must not be used.
            return payload.get("elements", [])
    raise RuntimeError("Overpass unavailable: " + " | ".join(failures[-3:]))


def osm_api_elements(kind, ids, attempts=4):
    """Elements by id from the main OpenStreetMap API (not Overpass).

    One request per batch. An id that no longer exists makes the API refuse
    the whole batch (404/410), so the batch is split until the bad id is
    isolated and skipped.
    """
    if not ids:
        return []
    for attempt in range(attempts):
        try:
            response = requests.get(f"{OSM_API}{kind}s.json", params={f"{kind}s": ",".join(ids)},
                                    headers=HEADERS, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if response.status_code in BUSY:
            time.sleep(2 ** (attempt + 1))
            continue
        if response.status_code in (404, 410):
            if len(ids) == 1:
                return []  # Deleted from OpenStreetMap since the snapshot.
            middle = len(ids) // 2
            return osm_api_elements(kind, ids[:middle]) + osm_api_elements(kind, ids[middle:])
        response.raise_for_status()
        return response.json().get("elements", [])
    raise RuntimeError(f"OpenStreetMap API did not answer for {len(ids)} {kind}s")


def collect_places(db):
    """Every OSM place in the snapshot tables, with snapshot coordinates."""
    places = {}

    def add(ref, name, lat, lon, geoid, address=None):
        if ref and lat is not None and lon is not None and ref not in places:
            places[ref] = {"name": name, "latitude": lat, "longitude": lon, "geoid": geoid, "address": address or None}

    for row in db.execute("SELECT name, latitude, longitude, geoid, address, source_url, duplicate_source_url FROM businesses"):
        snapshot = (row[4] or "").strip() or None
        add(osm_ref(row[5]), row[0], row[1], row[2], row[3], snapshot)
        add(osm_ref(row[6]), row[0], row[1], row[2], row[3], snapshot)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("community_businesses", "community_venues"):
        if table in tables:
            for row in db.execute(f"SELECT name, latitude, longitude, geoid, osm_id FROM {table}"):
                add(osm_ref(row[4]), row[0], row[1], row[2], row[3])
    return places


def own_tags(refs, chunk=200, pause=1.0):
    """Current OSM tags for each ref, by id, from the main OpenStreetMap API.

    Batches of 200 with a one-second pause between them, well inside the
    API's usage policy for light reads.
    """
    tags = {}
    by_type = {}
    for ref in refs:
        kind, number = ref.split("/")
        by_type.setdefault(kind, []).append(number)
    batches = [(kind, numbers[start:start + chunk]) for kind, numbers in sorted(by_type.items())
               for start in range(0, len(numbers), chunk)]
    for index, (kind, ids) in enumerate(batches, 1):
        for element in osm_api_elements(kind, ids):
            tags[f"{element['type']}/{element['id']}"] = element.get("tags", {})
        log(f"  tags: batch {index}/{len(batches)} ({kind}s), {len(tags)} places so far")
        time.sleep(pause)
    return tags


def point_in_ring(lon, lat, ring):
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1:
            inside = not inside
    return inside


def building_addresses(points, chunk=60, give_up_after=2):
    """Address of the addressed building outline each point sits inside, if any.

    Optional extra coverage from Overpass. A batch that fails is skipped, and
    after `give_up_after` failed batches in a row the step stops (Overpass
    is down). Returns (found, skipped_points); re-running fills the gaps.
    """
    found, skipped, failed_in_a_row = {}, 0, 0
    items = list(points.items())
    batches = [items[start:start + chunk] for start in range(0, len(items), chunk)]
    for index, batch in enumerate(batches, 1):
        if failed_in_a_row >= give_up_after:
            skipped += len(batch)
            continue
        around = "".join(f'way(around:25,{p["latitude"]},{p["longitude"]})["building"]["addr:housenumber"]["addr:street"];'
                         for _, p in batch)
        try:
            elements = overpass(f"[out:json][timeout:60];({around});out tags geom;")
        except RuntimeError as error:
            failed_in_a_row += 1
            skipped += len(batch)
            log(f"  buildings: batch {index}/{len(batches)} skipped ({error})")
            continue
        failed_in_a_row = 0
        buildings = [(format_address(e.get("tags", {})), [(n["lon"], n["lat"]) for n in e.get("geometry", [])])
                     for e in elements]
        for ref, p in batch:
            hits = {address for address, ring in buildings
                    if address and len(ring) >= 4 and point_in_ring(p["longitude"], p["latitude"], ring)}
            if len(hits) == 1:  # inside exactly one addressed building
                found[ref] = hits.pop()
        log(f"  buildings: batch {index}/{len(batches)}, {len(found)} addresses so far")
    if skipped:
        log(f"  buildings: {skipped} places skipped because Overpass was unavailable; re-run later to fill them")
    return found, skipped


def silver_spring_geoids():
    from answers.main import SILVER_SPRING_GEOIDS  # one list, shared with the API
    return set(SILVER_SPRING_GEOIDS)


def build_locations(db_path=DATABASE_PATH, *, fetch_tags=own_tags, fetch_buildings=building_addresses,
                    buildings="silver-spring"):
    """buildings: 'silver-spring' (default) runs the slow Overpass building
    lookup only for places in the Silver Spring tracts the app maps; 'all'
    runs it county-wide; 'none' skips it. Own addresses are always fetched
    for every place."""
    db_path = Path(db_path).resolve()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with TemporaryDirectory(prefix=".addresses-", dir=db_path.parent) as folder:
        candidate = Path(folder) / "with-addresses.db"
        with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(candidate)) as target:
            source.backup(target)
        with closing(sqlite3.connect(candidate)) as db, db:
            places = collect_places(db)
            # Current tags for every place: they usually carry city and ZIP,
            # which the snapshot's short street address does not. The
            # snapshot address is the fallback when current tags have none.
            log(f"Step 1/2: own addresses for {len(places)} places (OpenStreetMap API)")
            tags = fetch_tags(list(places))
            basis = {}
            for ref, place in places.items():
                address = format_address(tags.get(ref, {})) or place["address"]
                place["address"] = address
                if address:
                    basis[ref] = "listing"
            scope = None if buildings == "all" else set() if buildings == "none" else silver_spring_geoids()
            still = {ref: p for ref, p in places.items()
                     if not p["address"] and (scope is None or p["geoid"] in scope)}
            log(f"Step 2/2: building outlines for {len(still)} places without their own address (Overpass)")
            from_buildings, skipped = fetch_buildings(still)
            for ref, address in from_buildings.items():
                places[ref]["address"], basis[ref] = address, "building"
            db.execute("DROP TABLE IF EXISTS place_locations")
            db.executescript(SCHEMA)
            db.executemany("INSERT INTO place_locations VALUES (?,?,?,?,?,?,?,?)",
                           [(ref, OSM_URL + ref, p["name"], p["latitude"], p["longitude"], p["address"],
                             basis.get(ref), now) for ref, p in sorted(places.items())])
        validate_database(candidate)
        candidate.replace(db_path)
    counts = {key: sum(1 for ref in places if basis.get(ref) == key) for key in ("listing", "building")}
    return {"places": len(places), **counts, "no_address": len(places) - sum(counts.values()),
            "building_lookup_skipped": skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DATABASE_PATH)
    parser.add_argument("--buildings", choices=("silver-spring", "all", "none"), default="silver-spring",
                        help="Where to run the slow building-outline lookup (default: Silver Spring tracts)")
    args = parser.parse_args()
    print(build_locations(args.db, buildings=args.buildings))


if __name__ == "__main__":
    main()
