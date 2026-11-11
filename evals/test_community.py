"""Integration checks use real installed records; providers are explicit stand-ins."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from answers import main
from answers.community_context import catalog, schema_context
from answers.sql_guard import run
from get_data.import_community import import_community, validate_community

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'data/silverspring.db'
FENTON = "SELECT geoid FROM places WHERE name='Fenton Village'"


class CommunityTests(unittest.TestCase):
    def test_primary_and_supplemental_sources_coexist(self):
        counts=validate_community(DB)
        self.assertTrue(counts)
        with closing(sqlite3.connect(DB)) as con:
            self.assertEqual(con.execute('SELECT female_count FROM place_data').fetchone()[0],41406)
            self.assertEqual(con.execute("SELECT employed_residents FROM industry_employment WHERE geography_type='place' AND industry='Retail trade'").fetchone()[0],3231)
            self.assertTrue(con.execute('SELECT COUNT(*) FROM tract_history').fetchone()[0])
            for row in catalog(DB):
                self.assertIsNone(row['retrieved_at'])
                self.assertIsNone(row['data_vintage'])
                self.assertTrue(row['source_sha256'])

    def test_points_reassigned_by_geometry_and_old_tract_ids_kept_separate(self):
        with closing(sqlite3.connect(DB)) as con:
            self.assertFalse(con.execute('PRAGMA foreign_key_check').fetchone())
            fields={r[1] for r in con.execute('PRAGMA table_info(community_tract_metrics)')}
            self.assertIn('source_tract_id',fields)
            self.assertNotIn('geoid',fields)
            self.assertTrue(con.execute('SELECT COUNT(*) FROM community_venues WHERE geoid IS NOT NULL').fetchone()[0])
            row=con.execute('SELECT source_tract_id FROM community_tract_metrics LIMIT 1').fetchone()
            self.assertIsNotNone(con.execute('SELECT 1 FROM community_tract_geometry WHERE source_tract_id=?',row).fetchone())

    def test_catalog_describes_all_installed_fields_and_categories(self):
        context=schema_context(DB)
        with closing(sqlite3.connect(DB)) as con:
            for source in catalog(DB):
                self.assertIn(source['table_name'],context)
                for field in con.execute('PRAGMA table_info('+source['table_name']+')'):
                    self.assertIn(field[1],context)
        self.assertIn('cuisine',context)
        self.assertIn('source_tract_id',context)
        self.assertIn('capacity',context)

    def test_variations_reach_model_without_keyword_rejections(self):
        sql=f"SELECT name, capacity, website, source_url FROM community_venues WHERE geoid IN ({FENTON}) ORDER BY name"
        seen=[]
        def provider(prompt,max_tokens):
            seen.append(prompt)
            return sql if 'User question:' in prompt else 'See the mapped venue options and their supplied details.'
        for question in ['Which event venues are in Fenton Village?', 'Can you list places to host an e-commerce panel around Fenton?', 'Show me venue names and capacities for Fenton Village.']:
            with self.subTest(question=question),patch.object(main,'_ask_claude',side_effect=provider):
                result=main.ask(main.Question(question=question))
                self.assertTrue(result['rows'])
                self.assertTrue(any('capacity' in row for row in result['rows']))
                self.assertTrue(any('community_venues' in p and question in p for p in seen))
                self.assertTrue(any('Event and gathering venues' in s['label'] for s in result['sources']))
                self.assertFalse(any('Census' in s['label'] for s in result['sources']))

    def test_complementary_industry_and_venue_query(self):
        sql=f"""SELECT industry AS label, employed_residents AS value, source_url FROM industry_employment
                  WHERE geography_type='place' AND industry='Retail trade'
                  UNION ALL SELECT 'Mapped venues in Fenton tracts',COUNT(*),MIN(source_url)
                  FROM community_venues WHERE geoid IN ({FENTON})"""
        with patch.object(main,'_ask_claude',side_effect=[sql,'The returned evidence includes retail employment and venue options.']):
            result=main.ask(main.Question(question='For our e-commerce panel, show retail employment and local venues.'))
        self.assertTrue(any(r['value']==3231 for r in result['rows']))
        self.assertEqual(len(result['rows']),2)
        self.assertTrue(any('C24050' in s['url'] for s in result['sources']))
        self.assertTrue(any('Event and gathering venues' in s['label'] for s in result['sources']))
        self.assertIn('employed residents',' '.join(result['notes']))

    def test_new_data_queries_execute_and_return_source_specific_notes(self):
        queries=[
            ('community_bus_stops','SELECT stopname,source_url FROM community_bus_stops WHERE geoid IS NOT NULL LIMIT 3'),
            ('community_permits','SELECT issued,worktype,valuation,source_url FROM community_permits WHERE issued IS NOT NULL ORDER BY issued DESC LIMIT 3'),
            ('community_crime','SELECT COUNT(DISTINCT incident_id) AS incident_count,MIN(source_url) AS source_url FROM community_crime'),
            ('community_parcels','SELECT land_use,COUNT(*) AS parcel_count,MIN(source_url) AS source_url FROM community_parcels GROUP BY land_use'),
            ('community_businesses',"SELECT name,cuisine,source_url FROM community_businesses WHERE cuisine IS NOT NULL LIMIT 3"),
            ('community_tract_metrics','SELECT source_tract_id,pct_below_poverty,source_url FROM community_tract_metrics ORDER BY pct_below_poverty DESC LIMIT 3'),
        ]
        for table,sql in queries:
            with self.subTest(table=table),patch.object(main,'_ask_claude',side_effect=[sql,'See the returned source records.']):
                result=main.ask(main.Question(question='Explore the available community data'))
                self.assertTrue(result['rows'])
                self.assertTrue(result['notes'])
                self.assertFalse(any('B01003' in s['url'] for s in result['sources']))
        with closing(sqlite3.connect(DB)) as con:
            raw,distinct=con.execute('SELECT COUNT(*),COUNT(DISTINCT incident_id) FROM community_crime').fetchone()
            self.assertGreater(raw,distinct)

    def test_bad_partner_pair_leaves_output_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'snapshot.db';shutil.copy2(DB,target)
            before=hashlib.sha256(target.read_bytes()).hexdigest()
            invalid=Path(folder)/'geometry.json';invalid.write_text('{"features":[]}')
            with self.assertRaises(ValueError):
                import_community(ROOT/'data/partner_snapshot/community.duckdb',invalid,base=DB,output=target)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),before)

    def test_import_is_repeatable_without_duplicate_sources_or_primary_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'combined.db'
            counts=import_community(ROOT/'data/partner_snapshot/community.duckdb',ROOT/'data/partner_snapshot/tracts.geojson',base=DB,output=target)
            self.assertEqual(counts,validate_community(DB))
            with closing(sqlite3.connect(target)) as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM community_sources').fetchone()[0],len(counts))
                self.assertEqual(con.execute('SELECT female_count FROM place_data').fetchone()[0],41406)

    def test_missing_model_is_not_reported_as_missing_data(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':''}), patch.object(main,'client',None):
            result=main.ask(main.Question(question='Which venues are available?'))
        self.assertIn('not configured',result['answer'])
        self.assertIn('data is available',result['answer'])

    def test_returned_dates_and_capacity_text_can_be_described(self):
        self.assertTrue(main._uses_returned_numbers('Issued 2026-10-28; capacity 120.', [{'issued':'2026-10-28','capacity':'120'}]))
        self.assertFalse(main._uses_returned_numbers('There are 99999 venues.', [{'source_url':'https://example.com/99999'}]))

    def test_query_timeout_is_elapsed_time_not_instruction_estimate(self):
        with self.assertRaises(sqlite3.OperationalError):
            run(str(DB),'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n',timeout_seconds=0.01)
