"""Import dated race/ethnicity estimates on the same geography as primary ACS.

Uses official public summary files; no Census or model key is required.
Only Montgomery County tracts and the official Silver Spring place are stored.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import requests

from .config import DATABASE_PATH

YEAR = 2024
BASE = f'https://www2.census.gov/programs-surveys/acs/summary_file/{YEAR}/table-based-SF'
TABLES = ('B02001', 'B03002')
SNAPSHOT = Path(__file__).resolve().parents[1] / 'data/demographics_2024.json'
SCHEMA = '''
CREATE TABLE demographic_estimates (
 geoid TEXT NOT NULL, geography_type TEXT NOT NULL,
 table_id TEXT NOT NULL, measure TEXT NOT NULL, label TEXT NOT NULL,
 parent_measure TEXT, estimate INTEGER, margin_of_error INTEGER,
 acs_vintage TEXT NOT NULL, source_url TEXT NOT NULL, retrieved_at TEXT NOT NULL,
 PRIMARY KEY(geoid, geography_type, measure)
);
CREATE INDEX demographic_measure ON demographic_estimates(measure, geography_type, geoid);
CREATE TABLE demographic_sources (
 table_id TEXT PRIMARY KEY, title TEXT NOT NULL, file_url TEXT NOT NULL,
 sha256 TEXT NOT NULL, retrieved_at TEXT NOT NULL, last_modified TEXT,
 row_count INTEGER NOT NULL
);
'''


def download(url, path):
    digest = hashlib.sha256()
    with requests.get(url, stream=True, timeout=(15, 120)) as response:
        response.raise_for_status()
        with path.open('wb') as output:
            for chunk in response.iter_content(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        return {'file_url': url, 'sha256': digest.hexdigest(),
                'last_modified': response.headers.get('Last-Modified'),
                'retrieved_at': datetime.now(timezone.utc).isoformat()}


def read_measures(path):
    """Read official labels AND parent categories, avoiding flattened ambiguity."""
    measures = {}
    parents = {}
    with path.open(encoding='utf-8-sig', newline='') as source:
        for row in csv.DictReader(source, delimiter='|'):
            table = row['Table ID']
            if table not in TABLES or not row['Unique ID']:
                continue
            indent = int(row['Indent'])
            stack = parents.setdefault(table, {})
            parent = stack.get(indent - 1)
            label = row['Label'].rstrip(':')
            if parent and indent > 1:
                label = measures[parent]['label'] + ' > ' + label
            key = row['Unique ID']
            measures[key] = {'measure': key, 'table_id': table, 'label': label,
                             'parent_measure': parent, 'title': row['Title']}
            stack[indent] = key
            for level in list(stack):
                if level > indent:
                    del stack[level]
    if {value['table_id'] for value in measures.values()} != set(TABLES):
        raise ValueError('Official Census category definitions are incomplete')
    return measures


def number(value):
    if value in (None, '', 'null'):
        return None
    parsed = int(value)
    return None if parsed < 0 else parsed


def extract_table(path, table, measures, expected, provenance):
    rows = []
    with path.open(encoding='utf-8-sig', newline='') as source:
        reader = csv.DictReader(source, delimiter='|')
        codes = [key for key in measures if key.startswith(table + '_')]
        required = {'GEO_ID'} | {key.replace('_', '_E') for key in codes} | {key.replace('_', '_M') for key in codes}
        if not required <= set(reader.fieldnames or []):
            raise ValueError('Unexpected Census summary file columns')
        for row in reader:
            geography = row['GEO_ID']
            kind = 'tract' if geography.startswith('1400000US') else 'place' if geography.startswith('1600000US') else None
            geoid = geography.split('US')[-1]
            if (kind, geoid) not in expected:
                continue
            for key in codes:
                definition = measures[key]
                rows.append({
                    'geoid': geoid, 'geography_type': kind, 'table_id': table,
                    'measure': key, 'label': definition['label'], 'parent_measure': definition['parent_measure'],
                    'estimate': number(row[key.replace('_', '_E')]),
                    'margin_of_error': number(row[key.replace('_', '_M')]),
                    'acs_vintage': f'ACS {YEAR - 4}-{YEAR}',
                    'source_url': f'https://data.census.gov/table/ACSDT5Y{YEAR}.{table}?g=' + ('1400000US' if kind == 'tract' else '160XX00US') + geoid,
                    'retrieved_at': provenance['retrieved_at'],
                })
    return rows


def fetch_snapshot(database=DATABASE_PATH):
    with closing(sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True)) as con:
        expected = {('tract', row[0]) for row in con.execute('SELECT geoid FROM tract_data')}
        expected.update(('place', row[0]) for row in con.execute('SELECT geoid FROM place_data'))
    payload = {'year': YEAR, 'rows': [], 'sources': []}
    with TemporaryDirectory(prefix='civiclens-census-') as folder:
        folder = Path(folder)
        shell = folder / 'shells.txt'
        payload['definitions_source'] = download(BASE + '/documentation/ACS20245YR_Table_Shells.txt', shell)
        measures = read_measures(shell)
        for table in TABLES:
            path = folder / (table + '.dat')
            provenance = download(BASE + f'/data/5YRData/acsdt5y{YEAR}-{table.lower()}.dat', path)
            rows = extract_table(path, table, measures, expected, provenance)
            payload['rows'].extend(rows)
            payload['sources'].append(dict(provenance, table_id=table, title=next(v['title'] for v in measures.values() if v['table_id'] == table), row_count=len(rows)))
    return payload


def import_demographics(payload, base=DATABASE_PATH, output=DATABASE_PATH):
    if payload.get('year') != YEAR:
        raise ValueError('Demographic snapshot vintage does not match the primary ACS release')
    base, output = Path(base).resolve(), Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.demographics-', dir=output.parent) as directory:
        candidate = Path(directory) / 'candidate.db'
        with closing(sqlite3.connect(f'file:{base}?mode=ro', uri=True)) as original, closing(sqlite3.connect(candidate)) as target:
            original.backup(target)
        with closing(sqlite3.connect(candidate)) as con, con:
            con.executescript('DROP TABLE IF EXISTS demographic_estimates; DROP TABLE IF EXISTS demographic_sources;' + SCHEMA)
            fields = [row[1] for row in con.execute('PRAGMA table_info(demographic_estimates)')]
            con.executemany('INSERT INTO demographic_estimates VALUES (' + ','.join('?' for _ in fields) + ')',
                            ([row[key] for key in fields] for row in payload['rows']))
            for source in payload['sources']:
                con.execute('INSERT INTO demographic_sources VALUES (?,?,?,?,?,?,?)',
                    [source[key] for key in ('table_id','title','file_url','sha256','retrieved_at','last_modified','row_count')])
                con.execute('INSERT OR REPLACE INTO data_sources VALUES (?,?,?,?,?,?,?,?)',
                    ('acs_' + source['table_id'].lower() + '_2024', source['title'], 'U.S. Census Bureau',
                     f'https://data.census.gov/table/ACSDT5Y2024.{source["table_id"]}',
                     'Montgomery County tracts and official Silver Spring CDP', '2020-2024', source['retrieved_at'],
                     'Race and Hispanic origin are distinct; district totals use current whole-tract approximations.'))
        validate_demographics(candidate)
        candidate.replace(output)


def validate_demographics(database):
    with closing(sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True)) as con:
        expected = {('tract', row[0]): row[1] for row in con.execute('SELECT geoid,population_count FROM tract_data')}
        expected.update({('place', row[0]): row[1] for row in con.execute('SELECT geoid,population_count FROM place_data')})
        for table in TABLES:
            totals = {(r[0], r[1]): r[2] for r in con.execute('SELECT geography_type,geoid,estimate FROM demographic_estimates WHERE measure=?', (table + '_001',))}
            if totals != expected:
                raise ValueError('Demographic coverage or population differs from the primary ACS snapshot: ' + table)
        if con.execute("SELECT 1 FROM demographic_estimates WHERE estimate < 0 OR margin_of_error < 0 OR acs_vintage != 'ACS 2020-2024'").fetchone():
            raise ValueError('Invalid demographic estimates or vintage')
        counts = dict(con.execute('SELECT table_id,COUNT(*) FROM demographic_estimates GROUP BY table_id'))
        sources = dict(con.execute('SELECT table_id,row_count FROM demographic_sources'))
        if counts != sources or set(counts) != set(TABLES):
            raise ValueError('Missing demographic source records')
        for table, size in (('B02001', 10), ('B03002', 21)):
            coverage = {(r[0], r[1]): r[2] for r in con.execute('SELECT geography_type,geoid,COUNT(*) FROM demographic_estimates WHERE table_id=? GROUP BY geography_type,geoid', (table,))}
            if coverage != {key: size for key in expected}:
                raise ValueError('Incomplete demographic categories: ' + table)
        if con.execute('SELECT 1 FROM demographic_sources WHERE length(sha256) != 64 OR retrieved_at IS NULL').fetchone():
            raise ValueError('Missing demographic provenance')
        origins = {(r[0],r[1]):r[2] for r in con.execute("SELECT geography_type,geoid,SUM(estimate) FROM demographic_estimates WHERE measure IN ('B03002_002','B03002_012') GROUP BY geography_type,geoid")}
        if origins != expected:
            raise ValueError('Hispanic origin totals do not reconcile with primary population')
        # Exclude parent/subtotal rows when checking the mutually exclusive race categories.
        sums = {(r[0],r[1]):r[2] for r in con.execute("SELECT geography_type,geoid,SUM(estimate) FROM demographic_estimates WHERE table_id='B02001' AND measure BETWEEN 'B02001_002' AND 'B02001_008' GROUP BY geography_type,geoid")}
        if sums != expected:
            raise ValueError('Race categories do not reconcile with primary population')
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DATABASE_PATH)
    parser.add_argument('--snapshot', type=Path, help='Import a saved extracted official snapshot without network access')
    parser.add_argument('--save-snapshot', type=Path, default=SNAPSHOT)
    args = parser.parse_args()
    payload = json.loads(args.snapshot.read_text()) if args.snapshot else fetch_snapshot(args.database)
    import_demographics(payload, args.database, args.database)
    args.save_snapshot.write_text(json.dumps(payload, indent=2) + '\n')
    print(validate_demographics(args.database))


if __name__ == '__main__':
    main()
