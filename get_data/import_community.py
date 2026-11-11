"""Add a partner snapshot to the Census-backed SQLite database without replacing it.

python -m get_data.import_community --warehouse PATH --geometry PATH
Builds a separate file, validates it, and replaces the output only on success.
DuckDB is a build dependency only. The serving API continues to use SQLite.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from .build_database import assign_business_tracts
from .config import DATABASE_PATH
from .database import connect
from .validate_database import validate_database

ORIGIN = 'https://github.com/Monisha-Repos/community-intelligence-map'
COUNTY = 'https://data.montgomerycountymd.gov/resource/'
DATASETS = {
    'venues': ('Event and gathering venues', 'https://www.openstreetmap.org/copyright', 'Mapped venue objects; capacity and availability may be missing or unverified.'),
    'bus_stops': ('Ride On bus stops', COUNTY+'sfeg-dbzu.json', 'Countywide stop inventory; study-area metrics must filter geography.'),
    'stations': ('Rail and Metro stations', 'https://www.openstreetmap.org/copyright', 'Mapped station objects; no service-frequency or accessibility guarantee.'),
    'permits': ('Building permits', COUNTY+'m88u-pqki.json', 'Permit records, not completed construction; some issue dates are missing.'),
    'ped_struck': ('Pedestrian-struck dispatches', COUNTY+'gptc-gfn8.json', 'Dispatch records, not a complete collision registry.'),
    'crime': ('Reported crime rows', COUNTY+'icn6-v9z3.json', 'Multiple rows may share an incident_id. Count DISTINCT incident_id for distinct reports; reports are not all crime.'),
    'parcels': ('Parcel assessments', 'https://gis3.montgomerycountymd.gov/arcgis/rest/services/property/property_query/FeatureServer/0', 'Downloaded for the partner Fenton rectangle; no coordinates or district polygon supplied. Assessed values are not rents or market sale prices.'),
    'businesses': ('Supplemental mapped business objects', 'https://www.openstreetmap.org/copyright', 'Overlaps primary businesses; never add both counts. Use primary businesses for standard counts, this table for extra fields such as cuisine.'),
    'tract_metrics': ('Additional county demographic metrics', 'https://gis3.montgomerycountymd.gov/arcgis/rest/services/census/demographics/MapServer/9', 'Undated demographics on 2010 tract boundaries. Not interchangeable with the primary dated ACS or 2024 tract geometry.'),
}

REGISTRY_SCHEMA = '''
CREATE TABLE community_sources (
 source_id TEXT PRIMARY KEY, table_name TEXT UNIQUE NOT NULL, label TEXT NOT NULL,
 source_url TEXT NOT NULL, source_repository TEXT NOT NULL, source_sha256 TEXT NOT NULL,
 imported_at TEXT NOT NULL, retrieved_at TEXT, data_vintage TEXT,
 geography TEXT NOT NULL, geography_vintage TEXT, coverage_note TEXT NOT NULL,
 row_count INTEGER NOT NULL, first_record TEXT, last_record TEXT, undated_rows INTEGER
);
'''


def quote(name):
    return '"'+name.replace('"','""')+'"'


def merge_point_and_building(records, max_meters=50):
    """Merge a place OpenStreetMap maps twice: once as a point, once as its building.

    Same rule as the primary businesses table (fetch_businesses.deduplicate_businesses),
    so both tables count a place once: exactly two records with the same name,
    category and tract, one a node and the other a way/relation, within 50 m.
    Anything less certain (three same-named buildings, two separate points,
    pairs further apart) is kept as is. The kept row is the node, filled in
    with any field only the building had; the building's link is kept in
    duplicate_source_url so nothing is lost.
    """
    from collections import defaultdict
    from .fetch_businesses import distance_meters
    groups = defaultdict(list)
    for record in records:
        name = ' '.join(str(record.get('name') or '').casefold().split())
        if not name or record.get('latitude') is None or record.get('longitude') is None:
            groups[('', id(record))].append(record)
            continue
        groups[(name, record.get('category'), record.get('geoid'))].append(record)
    merged = []
    for group in groups.values():
        if len(group) == 2:
            node, other = sorted(group, key=lambda r: not str(r.get('osm_id') or '').startswith('node/'))
            if (str(node.get('osm_id') or '').startswith('node/') and other.get('osm_id')
                    and not str(other['osm_id']).startswith('node/')
                    and distance_meters(node, other) <= max_meters):
                kept = {**{k: v for k, v in other.items() if v is not None}, **{k: v for k, v in node.items() if v is not None}}
                kept['duplicate_source_url'] = 'https://www.openstreetmap.org/'+other['osm_id']
                merged.append({k: kept.get(k) for k in {**node, **kept}})
                continue
        merged.extend(group)
    return merged


def import_community(warehouse, geometry, base=DATABASE_PATH, output=DATABASE_PATH):
    import duckdb
    warehouse, geometry, base, output = map(lambda x: Path(x).resolve(), (warehouse, geometry, base, output))
    if output in (warehouse, geometry):
        raise ValueError('Output must not overwrite the supplied partner files')
    geo = json.loads(geometry.read_text())
    features = geo.get('features', [])
    ids = [str(f['properties']['tract_id']) for f in features]
    if not features or len(ids) != len(set(ids)):
        raise ValueError('Partner map must contain unique tract IDs')
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(warehouse.read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(warehouse), read_only=True) as partner, TemporaryDirectory(prefix='.community-import-', dir=output.parent) as folder:
        available = {r[0] for r in partner.execute('SHOW TABLES').fetchall()}
        if not set(DATASETS) <= available:
            raise ValueError('Partner snapshot is missing expected source tables')
        if set(ids) != {r[0] for r in partner.execute('SELECT tract_id FROM tract_metrics').fetchall()}:
            raise ValueError('Partner geography and demographic IDs differ')
        candidate = Path(folder)/'combined.db'
        with closing(connect(base, readonly=True)) as original, closing(sqlite3.connect(candidate)) as target:
            original.backup(target)
        with closing(connect(candidate)) as db, db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0].startswith('community_')]
            # Remove child tables first. Primary Census tables are never replaced.
            for name in sorted(tables, key=lambda n: n == 'community_sources'):
                db.execute('DROP TABLE '+quote(name))
            db.executescript(REGISTRY_SCHEMA)
            tract_shapes = {r['geoid']: {'geometry_geojson':r['geometry_geojson']} for r in db.execute('SELECT geoid, geometry_geojson FROM tract_data')}
            counts = {}
            for source, (label, url, note) in DATASETS.items():
                table = 'community_'+source
                description = partner.execute('DESCRIBE '+quote(source)).fetchall()
                fields = [r[0] for r in description]
                cursor = partner.execute('SELECT * FROM '+quote(source))
                records = [dict(zip(fields, row)) for row in cursor.fetchall()]
                columns = [('latitude' if f=='lat' else 'longitude' if f=='lon' else 'source_tract_id' if f=='tract_id' else f,
                            'REAL' if any(t in str(typ).upper() for t in ['DOUBLE','FLOAT','DECIMAL']) else 'INTEGER' if 'INT' in str(typ).upper() else 'TEXT')
                           for f,typ,*_ in description]
                records = [{('latitude' if k=='lat' else 'longitude' if k=='lon' else 'source_tract_id' if k=='tract_id' else k):v for k,v in row.items()} for row in records]
                if 'lat' in fields and 'lon' in fields:
                    # Assign CURRENT tract IDs from point geometry; do not join
                    # 2010 source tract IDs directly to 2024 tract IDs.
                    positioned = [dict(r, _import_row=i) for i,r in enumerate(records) if r['latitude'] is not None and r['longitude'] is not None]
                    matched = {r['_import_row']: r['geoid'] for r in assign_business_tracts(positioned, tract_shapes)}
                    records = [dict(r, geoid=matched.get(i)) for i,r in enumerate(records)]
                    columns.append(('geoid','TEXT REFERENCES tract_data(geoid)'))
                if source == 'businesses':
                    records = merge_point_and_building(records)
                    columns.append(('duplicate_source_url','TEXT'))
                columns += [('source_id','TEXT REFERENCES community_sources(source_id)'), ('source_url','TEXT')]
                geography = '2010 source census tracts' if source=='tract_metrics' else 'Partner Fenton rectangular parcel download' if source=='parcels' else 'Partner source download; point-based 2024 tract links where available'
                date_column = 'issued' if source=='permits' else 'when' if source in ('crime','ped_struck') else None
                dated = [r[date_column] for r in records if r.get(date_column)] if date_column else []
                db.execute('INSERT INTO community_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (source,table,label,url,ORIGIN,digest,now,None,None,geography,'2010' if source=='tract_metrics' else None,note,len(records),min(dated) if dated else None,max(dated) if dated else None,len(records)-len(dated) if date_column else None))
                db.execute('CREATE TABLE '+quote(table)+' ('+', '.join(quote(k)+' '+t for k,t in columns)+')')
                names = [k for k,_ in columns]
                for record in records:
                    record['source_id'] = source
                    record['source_url'] = 'https://www.openstreetmap.org/'+record['osm_id'] if record.get('osm_id') else url
                db.executemany('INSERT INTO '+quote(table)+' VALUES ('+','.join('?' for _ in names)+')',
                               ([r.get(k) for k in names] for r in records))
                if 'lat' in fields:
                    db.execute('CREATE INDEX '+quote(table+'_geoid')+' ON '+quote(table)+'(geoid)')
                counts[table] = len(records)
            db.execute('CREATE TABLE community_tract_geometry (source_tract_id TEXT PRIMARY KEY, geometry_geojson TEXT NOT NULL, geography_vintage TEXT NOT NULL)')
            db.executemany('INSERT INTO community_tract_geometry VALUES (?,?,?)', [(f['properties']['tract_id'],json.dumps(f['geometry']),'2010') for f in features])
        validate_database(candidate)
        validate_community(candidate)
        candidate.replace(output)
    return counts


def validate_community(path=DATABASE_PATH):
    with closing(connect(path, readonly=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'community_sources' not in tables:
            return {}
        results = {}
        for source in db.execute('SELECT * FROM community_sources').fetchall():
            table = source['table_name']
            if table not in {'community_'+s for s in DATASETS}:
                raise ValueError('Unexpected supplemental table in registry')
            count = db.execute('SELECT COUNT(*) FROM '+quote(table)).fetchone()[0]
            if count != source['row_count']:
                raise ValueError('Supplemental source count mismatch: '+table)
            if not source['source_sha256']:
                raise ValueError('Missing supplemental source checksum')
            results[table] = count
        if set(results) != {'community_'+s for s in DATASETS}:
            raise ValueError('Incomplete supplemental source registry')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Unlinked supplemental records')
        return results


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--warehouse', type=Path, required=True)
    p.add_argument('--geometry', type=Path, required=True)
    p.add_argument('--base', type=Path, default=DATABASE_PATH)
    p.add_argument('--output', type=Path, default=DATABASE_PATH)
    args = p.parse_args()
    print(json.dumps(import_community(args.warehouse,args.geometry,args.base,args.output),indent=2))
