"""Refresh the county datasets straight from Montgomery County open data.

python -m get_data.refresh_county [--db PATH] [--dry-run]

The partner snapshot copied these tables once. The county publishes them on
dataMontgomery and updates crime, permits and pedestrian-struck dispatches
daily, for free, with no key. This replaces those three tables with the
current county records for the same Silver Spring rectangle the snapshot
used, so coverage does not change, only freshness.

Nothing is replaced unless every check passes:
  - the field mapping still holds: records present before and now must
    match field for field (at least 98%; the county corrects a few)
  - no meaningful loss: at least 98% of the old records are still present
  - the newest record is not older than before
  - validate_database and validate_community pass on the result
Otherwise the database is left untouched and the reason is printed.
Writes a temporary copy and replaces the database only on success.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time

import requests

from .build_database import assign_business_tracts
from .config import DATABASE_PATH
from .fetch_businesses import HEADERS
from .import_community import validate_community, quote
from .validate_database import validate_database

COUNTY = "https://data.montgomerycountymd.gov/resource/"
# The partner snapshot's rectangle. It contains all 19 Silver Spring tracts.
SOUTH, WEST, NORTH, EAST = 38.965, -77.07, 39.035, -76.97
AREA = f"Silver Spring rectangle ({SOUTH} to {NORTH} N, {WEST} to {EAST} E)"
PAGE = 50000


def day(value):
    return str(value)[:10] if value else None


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def crime_row(r):
    against = (r.get("crimename1") or "").removeprefix("Crime Against ").strip() or None
    return {"latitude": number(r.get("latitude")), "longitude": number(r.get("longitude")),
            "incident_id": r.get("incident_id"), "when": day(r.get("date")),
            "against": against, "category": r.get("crimename2"), "detail": r.get("crimename3"),
            "place": r.get("place"), "victims": number(r.get("victims"))}


def permit_row(r):
    location = r.get("location") or {}
    return {"latitude": number(location.get("latitude")), "longitude": number(location.get("longitude")),
            "permitno": r.get("permitno"), "worktype": r.get("worktype"), "usecode": r.get("usecode"),
            "issued": day(r.get("issueddate")), "valuation": number(r.get("declaredvaluation")),
            "applicationtype": r.get("applicationtype")}


def ped_row(r):
    return {"latitude": number(r.get("latitude")), "longitude": number(r.get("longitude")),
            "incident_id": r.get("incident_id"), "when": day(r.get("start_time")), "address": r.get("address")}


# source_id: (dataset, location column for the area filter, mapper, key, compared fields, date field)
DATASETS = {
    "crime": ("icn6-v9z3", "geolocation", crime_row, "incident_id",
              ("when", "against", "category", "detail", "place", "victims"), "when"),
    "permits": ("m88u-pqki", "location", permit_row, "permitno",
                ("worktype", "usecode", "issued", "valuation", "applicationtype"), "issued"),
    "ped_struck": ("gptc-gfn8", "geolocation", ped_row, "incident_id", ("when", "address"), "when"),
}


def fetch(dataset, column, pause=1.0):
    """Every county record inside the rectangle, paged, oldest first."""
    rows, offset = [], 0
    where = f"within_box({column}, {NORTH}, {WEST}, {SOUTH}, {EAST})"
    while True:
        for attempt in range(4):
            try:
                response = requests.get(f"{COUNTY}{dataset}.json", headers=HEADERS, timeout=120,
                                        params={"$where": where, "$order": ":id", "$limit": PAGE, "$offset": offset})
                if response.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {response.status_code}")
                response.raise_for_status()
                page = response.json()
                break
            except (requests.RequestException, ValueError):
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
        rows.extend(page)
        print(f"  {dataset}: {len(rows)} records", flush=True)
        if len(page) < PAGE:
            return rows
        offset += PAGE
        time.sleep(pause)


def rows_updated(dataset):
    meta = requests.get(f"https://data.montgomerycountymd.gov/api/views/{dataset}.json", headers=HEADERS, timeout=60).json()
    return datetime.fromtimestamp(meta["rowsUpdatedAt"], timezone.utc).date().isoformat()


def compare(old, new, key, fields):
    """How well the new records agree with the old ones, keyed by `key`.

    A key may have several rows (one per offence), so each side is compared
    as a sorted list of field tuples.
    """
    def grouped(rows):
        groups = defaultdict(list)
        for row in rows:
            groups[row[key]].append(tuple(row.get(f) for f in fields))
        return {k: sorted(v, key=repr) for k, v in groups.items()}
    before, after = grouped(old), grouped(new)
    shared = before.keys() & after.keys()
    same = sum(1 for k in shared if before[k] == after[k])
    return {"old_keys": len(before), "kept": len(shared), "kept_share": len(shared) / max(len(before), 1),
            "same_share": same / max(len(shared), 1), "new_keys": len(after.keys() - before.keys())}


def check(source_id, old, new, date_field):
    _, _, _, key, fields, _ = DATASETS[source_id]
    result = compare(old, new, key, fields)
    old_last = max((r[date_field] for r in old if r.get(date_field)), default=None)
    new_last = max((r[date_field] for r in new if r.get(date_field)), default=None)
    problems = []
    if result["same_share"] < 0.98:
        problems.append(f"only {result['same_share']:.1%} of matching records agree field for field; the mapping may have changed")
    if result["kept_share"] < 0.98:
        problems.append(f"only {result['kept_share']:.1%} of existing records are still published")
    if old_last and (not new_last or new_last < old_last):
        problems.append(f"newest record went backwards ({old_last} -> {new_last})")
    return {**result, "rows_before": len(old), "rows_after": len(new), "last_before": old_last,
            "last_after": new_last, "problems": problems}


def refresh(db_path=DATABASE_PATH, *, fetcher=fetch, updated=rows_updated, dry_run=False):
    db_path = Path(db_path).resolve()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = {}
    with TemporaryDirectory(prefix=".county-", dir=db_path.parent) as folder:
        candidate = Path(folder) / "refreshed.db"
        with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(candidate)) as target:
            source.backup(target)
        with closing(sqlite3.connect(candidate)) as db:
            db.row_factory = sqlite3.Row
            shapes = {r["geoid"]: {"geometry_geojson": r["geometry_geojson"]}
                      for r in db.execute("SELECT geoid, geometry_geojson FROM tract_data")}
            for source_id, (dataset, column, mapper, key, fields, date_field) in DATASETS.items():
                table = "community_" + source_id
                columns = [r["name"] for r in db.execute(f"PRAGMA table_info({quote(table)})")]
                old = [dict(r) for r in db.execute(f"SELECT * FROM {quote(table)}")]
                raw = fetcher(dataset, column)
                new = [row for row in map(mapper, raw) if row["latitude"] is not None and row["longitude"] is not None and row[key]]
                found = {r["_i"]: r["geoid"] for r in assign_business_tracts([dict(r, _i=i) for i, r in enumerate(new)], shapes)}
                registry = db.execute("SELECT source_url FROM community_sources WHERE source_id=?", (source_id,)).fetchone()
                for i, row in enumerate(new):
                    row.update(geoid=found.get(i), source_id=source_id, source_url=registry["source_url"])
                result = check(source_id, old, new, date_field)
                report[source_id] = result
                if result["problems"]:
                    continue
                dated = [r[date_field] for r in new if r.get(date_field)]
                digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
                with db:
                    db.execute(f"DELETE FROM {quote(table)}")
                    db.executemany(f"INSERT INTO {quote(table)} VALUES ({','.join('?' * len(columns))})",
                                   [[row.get(c) for c in columns] for row in new])
                    db.execute("""UPDATE community_sources SET source_repository=?, source_sha256=?, imported_at=?,
                                  retrieved_at=?, data_vintage=?, geography=?, row_count=?, first_record=?,
                                  last_record=?, undated_rows=? WHERE source_id=?""",
                               (f"https://data.montgomerycountymd.gov/d/{dataset}", digest, now, now,
                                f"Montgomery County open data, updated {updated(dataset)}",
                                f"{AREA}; point-based 2024 tract links", len(new), min(dated, default=None),
                                max(dated, default=None), len(new) - len(dated), source_id))
        if any(r["problems"] for r in report.values()):
            report["replaced"] = False
            return report
        validate_database(candidate)
        validate_community(candidate)
        if not dry_run:
            candidate.replace(db_path)
    report["replaced"] = not dry_run
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DATABASE_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and check, but do not replace the database")
    args = parser.parse_args()
    report = refresh(args.db, dry_run=args.dry_run)
    for source_id, result in report.items():
        if isinstance(result, dict):
            print(f"{source_id}: {result['rows_before']} -> {result['rows_after']} rows, newest "
                  f"{result['last_before']} -> {result['last_after']}, {result['same_share']:.1%} of matching records "
                  f"identical, {result['kept_share']:.1%} kept" + (f"  PROBLEMS: {result['problems']}" if result["problems"] else ""))
    print("Database replaced." if report["replaced"] else "Database NOT replaced.")
    raise SystemExit(0 if report["replaced"] or (args.dry_run and not any(
        r["problems"] for r in report.values() if isinstance(r, dict))) else 1)


if __name__ == "__main__":
    main()
