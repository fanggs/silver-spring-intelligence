"""Describe installed, dated demographic measures without a question router."""
from contextlib import closing
import re
import sqlite3


def measures(database):
    with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='demographic_estimates' AND type='table'").fetchone():
            return []
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute('SELECT DISTINCT table_id,measure,label,parent_measure,acs_vintage FROM demographic_estimates ORDER BY measure')]


def schema_context(database):
    installed = measures(database)
    if not installed:
        return ''
    catalog = '\n'.join(f"{r['measure']}: {r['label']} (parent {r['parent_measure'] or 'none'})" for r in installed)
    return '''
DATED RACE AND ETHNICITY DATA ACTUALLY INSTALLED:
TABLE demographic_estimates
 geoid TEXT, geography_type TEXT ('tract' or 'place'), table_id TEXT,
 measure TEXT (unique Census variable within each geography), label TEXT,
 parent_measure TEXT, estimate INTEGER, margin_of_error INTEGER,
 acs_vintage TEXT, source_url TEXT, retrieved_at TEXT.
These ACS 2020-2024 estimates match the current primary tract IDs and the
official Silver Spring Census place. Prefer these over undated 2010 partner
race metrics. Race/ethnicity population questions are answerable from these
columns; do not refuse them just because tract_data has no race columns.
For named districts use geography_type='tract' AND geoid IN (SELECT DISTINCT
geoid FROM places WHERE LOWER(name)=LOWER(the requested place)). SUM the one
requested measure across those tracts, not the entire county. For Silver
Spring as a whole use geography_type='place' AND geoid='2472450'. Never add
place and tract estimates together. Include table_id, label, acs_vintage,
source_url, COUNT(estimate) AS available_tracts and COUNT(*) AS requested_tracts for an aggregate so incomplete
estimates are visible. NULL estimates are unknown, never zero. Aggregate MOEs
are not additive; return individual tract MOEs if no aggregation is computed.
Generic White/Black/Asian population means the corresponding 'alone' race
category, including any Hispanic origin. Explicitly label that definition.
Use B03002 when Hispanic/non-Hispanic origin is specified. Hispanic origin is
not a mutually exclusive race. Parent categories and their subcategories
overlap: never sum a total with its parts or sum all rows of a table.
Do not pretend this includes detailed national origins or every possible
multiracial combination. The complete installed category list follows:
''' + catalog


def is_place(sql, rows):
    if not re.search(r'\bdemographic_estimates\b', sql, re.I):
        return False
    types = {r.get('geography_type') for r in rows if r.get('geography_type')}
    if types:
        return types == {'place'}
    return bool(re.search(r"geography_type\s*=\s*['\"]place['\"]", sql, re.I))


def source_tables(sql, rows):
    if not re.search(r'\bdemographic_estimates\b', sql, re.I):
        return []
    found = {str(r.get('table_id')) for r in rows if r.get('table_id')}
    found.update(re.findall(r'\bB(?:02001|03002)', sql, re.I))
    for row in rows:
        found.update(re.findall(r'B(?:02001|03002)', str(row.get('source_url', '')), re.I))
    return sorted(t.upper() for t in found if t.upper() in {'B02001','B03002'}) or ['B02001','B03002']
