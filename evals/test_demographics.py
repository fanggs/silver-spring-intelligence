"""Dated demographic coverage and geographic-query regression tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from answers import main, demographic_context
from answers.sql_guard import run
from get_data.import_demographics import import_demographics, validate_demographics, number

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'data/silverspring.db'
SNAPSHOT = ROOT / 'data/demographics_2024.json'
FENTON = "geography_type='tract' AND geoid IN (SELECT DISTINCT geoid FROM places WHERE name='Fenton Village')"


def query(measure='B02001_002', scope=FENTON):
    return f"""SELECT table_id,label,SUM(estimate) AS estimate,
        COUNT(estimate) AS available_tracts,COUNT(*) AS requested_tracts,
        MIN(acs_vintage) AS acs_vintage,MIN(source_url) AS source_url
        FROM demographic_estimates WHERE measure='{measure}' AND {scope}
        GROUP BY table_id,label"""


class DemographicTests(unittest.TestCase):
    def test_all_current_geographies_and_population_totals_reconcile(self):
        counts = validate_demographics(DB)
        with sqlite3.connect(DB) as con:
            expected = con.execute('SELECT COUNT(*) FROM tract_data').fetchone()[0] + con.execute('SELECT COUNT(*) FROM place_data').fetchone()[0]
        self.assertEqual(counts, {'B02001': expected * 10, 'B03002': expected * 21})

    def test_fenton_race_and_origin_are_different_fully_covered_measures(self):
        white, _ = run(str(DB), query())
        non_hispanic, _ = run(str(DB), query('B03002_003'))
        self.assertEqual(white[0]['estimate'], 3138)
        self.assertEqual(non_hispanic[0]['estimate'], 2907)
        self.assertEqual(white[0]['available_tracts'], 3)
        self.assertEqual(white[0]['requested_tracts'], 3)
        self.assertEqual(white[0]['label'], 'White alone')
        self.assertEqual(non_hispanic[0]['label'], 'Not Hispanic or Latino > White alone')

    def test_wording_variations_use_the_model_catalog_and_real_query_rows(self):
        for question in ['How many Whites are in Fenton Village?',
                         'How many White residents live in Fenton Village?',
                         'How many people classified as White are in Fenton Village?']:
            with self.subTest(question=question), patch.object(main, '_ask_claude', return_value=query()) as model:
                result = main.ask(main.Question(question=question))
            self.assertEqual(model.call_count, 1)
            self.assertIn('B02001_002: White alone', model.call_args.args[0])
            self.assertIn('3,138', result['summary'])
            self.assertIn('White alone', result['summary'])
            self.assertIn('tract area used to approximate Fenton Village', result['summary'])
            self.assertEqual(result['statistic']['value'], 3138)
            self.assertTrue(any('B02001' in source['url'] for source in result['sources']))
            self.assertFalse(any('B01003' in source['url'] for source in result['sources']))

    def test_place_demographics_use_official_place_geography_and_source(self):
        sql = query('B03002_003', "geography_type='place' AND geoid='2472450'")
        with patch.object(main, '_ask_claude', return_value=sql):
            result = main.ask(main.Question(question='How many non-Hispanic White people live in Silver Spring?'))
        self.assertEqual(result['geography']['kind'], 'census_place')
        self.assertEqual(result['highlight_scope'], 'boundary')
        self.assertTrue(any('B03002?g=160XX00US2472450' in source['url'] for source in result['sources']))
        self.assertFalse(any('B02001' in source['url'] for source in result['sources']))

    def test_complete_category_catalog_preserves_ethnicity_and_race_hierarchy(self):
        installed = demographic_context.measures(str(DB))
        self.assertEqual(len(installed), 31)
        labels = {r['measure']: r['label'] for r in installed}
        self.assertNotEqual(labels['B03002_003'], labels['B03002_013'])
        self.assertIn('Hispanic or Latino > White alone', labels['B03002_013'])
        self.assertIn('Black or African American alone', labels['B02001_003'])
        self.assertIn('Asian alone', labels['B02001_005'])
        self.assertIn('Two or More Races', labels['B02001_008'])

    def test_broken_snapshot_cannot_replace_the_active_database(self):
        payload = json.loads(SNAPSHOT.read_text())
        payload['rows'] = payload['rows'][1:]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'active.db'
            target.write_bytes(b'unchanged active snapshot')
            before = hashlib.sha256(target.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                import_demographics(payload, DB, target)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)

    def test_offline_reimport_preserves_primary_and_partner_rows(self):
        payload = json.loads(SNAPSHOT.read_text())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'copy.db'
            import_demographics(payload, DB, target)
            self.assertEqual(validate_demographics(target), validate_demographics(DB))
            with sqlite3.connect(DB) as before, sqlite3.connect(target) as after:
                for table in ['tract_data','place_data','places','industry_employment','businesses','tract_history','community_tract_metrics']:
                    self.assertEqual(before.execute('SELECT * FROM ' + table).fetchall(), after.execute('SELECT * FROM ' + table).fetchall())

    def test_missing_estimates_are_not_rendered_as_complete_totals_or_zero(self):
        self.assertIsNone(number('-666666666'))
        self.assertIsNone(number(''))
        self.assertEqual(number('0'), 0)
        row = {'estimate': 10, 'label': 'White alone', 'acs_vintage': 'ACS 2020-2024',
               'available_tracts': 2, 'requested_tracts': 3}
        self.assertIsNone(main._demographic_count('How many White residents?', query(), [row]))
        self.assertTrue(any('incomplete' in note for note in main._answer_notes(query(), [row], 'Fenton Village')))


if __name__ == '__main__':
    unittest.main()
