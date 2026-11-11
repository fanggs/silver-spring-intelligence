"""Describe the installed supplemental data to the existing text-to-SQL flow.

The catalog comes from the actual SQLite schema and source registry. There is
no question keyword router, fixed answer, or second database query language.
"""
from contextlib import closing
import json
import re
import sqlite3
from pathlib import Path


def connect(path):
    con = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


def catalog(path):
    with closing(connect(path)) as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='community_sources'").fetchone():
            return []
        return [dict(r) for r in con.execute('SELECT * FROM community_sources ORDER BY source_id')]


def schema_context(path):
    registered = catalog(path)
    if not registered:
        return ''
    parts = ['ADDITIONAL DATA ACTUALLY INSTALLED IN THIS SQLITE DATABASE:']
    with closing(connect(path)) as con:
        for source in registered:
            table = source['table_name']
            if not re.fullmatch(r'community_[a-z_]+',table):
                continue
            columns = [(r['name'],r['type']) for r in con.execute('PRAGMA table_info("'+table+'")')]
            parts.append('TABLE '+table+' ('+', '.join(k+' '+t for k,t in columns)+')')
            parts.append(source['label']+': '+source['coverage_note'])
            parts.append('Coverage: '+source['geography']+'. Source retrieval date: '+str(source['retrieved_at'] or 'unknown')+'. Data vintage: '+str(source['data_vintage'] or 'unknown')+'. Import time is NOT a source date.')
            for key in ('category','land_use'):
                if key in {k for k,_ in columns}:
                    values = [r[0] for r in con.execute(f'SELECT "{key}" FROM "{table}" WHERE "{key}" IS NOT NULL GROUP BY "{key}" ORDER BY COUNT(*) DESC LIMIT 12')]
                    parts.append(key+' examples (not an exhaustive list): '+json.dumps(values))
            if source.get('first_record'):
                parts.append('Dated records: '+source['first_record']+' through '+source['last_record']+'. Some rows may be undated.')
        names = [r[0] for r in con.execute('SELECT DISTINCT name FROM places ORDER BY name')]
        parts.append('Known named places in places: '+json.dumps(names))
    parts.append('''
Use these tables whenever their fields can answer the question. Counts, lists,
rankings, combinations, comparisons, and rephrased questions are all permitted.
Do not refuse because a question does not match an example. Query available
rows; a missing optional field is not a reason to discard otherwise useful results.
For an e-commerce panel return the requested retail industry evidence and, if
helpful, relevant venues/access from the new tables, with clear labels. A UNION
ALL with consistent labelled columns can combine evidence from different tables.
Do not substitute venue counts for retail employment.
When counting places of several kinds (for example venues that are hotels,
community centres and events venues), return one row per category with
GROUP BY category, so each kind is counted and named by the data itself.
Never return a combined count and also a count of one of its parts as separate
rows: hotels inside a venue total must not appear again as their own row.
The venue data has no capacity or meeting-room information for any venue.
Every community point table has latitude, longitude and geoid linked from its
coordinates to the CURRENT tract_data geometry. Use geoid IN (SELECT geoid
FROM places WHERE ...) for named districts; use the normal Silver Spring tract
scope for unqualified local questions. Null geoid means no containing primary
tract: do not count those records as mapped county tract records.
community_tract_metrics uses source_tract_id on 2010 geography, not current
geoid. community_tract_geometry stores its original geometry_geojson and
geography_vintage. Join these two only using source_tract_id. Never join 2010
source_tract_id directly to tract_data.geoid or substitute undated estimates for
the dated primary Census measures. Extra poverty, burden and vulnerability
metrics remain available with that explicit geographic and vintage limitation.
community_parcels is the rectangular parcel download, without point locations
or current geoid. Do not describe it as all county parcels or use it to infer rents.
community_businesses is overlapping supplementary OSM objects with cuisine and
unnamed listings; use businesses for standard business counts. Use the supplement
for fields absent from businesses, label its narrower coverage, and never add
its totals to businesses. An absent map listing is not proof of no business.
Crime has multiple rows per incident: COUNT(DISTINCT incident_id) counts distinct
reported incidents; COUNT(*) counts rows/offenses. Always label which. Time
series must expose the relevant date range and incomplete-year limitations.
Treat venue capacity as unverified source text; NULL is unknown, not zero.
Do not make a statistical or causal recommendation from simple correlations.
Always include source_url or join community_sources using source_id.
''')
    return '\n'.join(parts)


def used_sources(path, sql):
    return [s for s in catalog(path) if re.search(r'\b'+re.escape(s['table_name'])+r'\b', sql or '',re.I)]


def notes(path, sql):
    out=[]
    for source in used_sources(path, sql):
        out.append(source['label']+': '+source['coverage_note'])
        if not source['retrieved_at'] or not source['data_vintage']:
            out.append('The partner snapshot has no confirmed source retrieval date or data vintage; record dates and import time are different.')
        if source['source_id'] in ('venues','bus_stops','stations','permits','ped_struck','crime','businesses'):
            out.append('Point records are assigned to current Census tracts by location. Named district totals use whole-tract approximations, not a precise district boundary.')
        if source['source_id']=='tract_metrics':
            out.append('These additional demographic measures use original 2010 tracts; they cannot be directly compared with current tract estimates.')
        if source.get('undated_rows'):
            out.append('Some records have no date and are excluded from dated comparisons.')
    return list(dict.fromkeys(out))
