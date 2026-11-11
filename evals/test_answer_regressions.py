"""Regression checks for failures observed on the real hosted site."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import Response
from answers import main
from answers.response_cache import AnswerCache
from answers.sql_guard import run, validate, UnsafeQuery

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'data/silverspring.db'
CASES = json.loads((ROOT / 'evals/fixtures/hosted-queries-2026-11-11.json').read_text())['cases']


class QuerySafetyTests(unittest.TestCase):
    def test_punctuation_keywords_comments_and_escaped_quotes_are_data(self):
        for sql in ["SELECT '; capacity unknown; DROP TABLE x' AS note",
                    "-- comment; DELETE\nSELECT 'owner''s; venue' AS note; -- trailing;",
                    'SELECT 1 AS "DROP; value" /* UPDATE; */',
                    'WITH x AS (SELECT 1 AS n) SELECT n FROM x']:
            with self.subTest(sql=sql):
                self.assertTrue(run(str(DB), sql)[0])

    def test_actual_second_statements_and_writes_remain_rejected(self):
        for sql in ['SELECT 1; SELECT 2', 'SELECT 1; DROP TABLE businesses',
                    'WITH x AS (SELECT 1) DELETE FROM businesses',
                    "SELECT 1; /* fine */ UPDATE businesses SET name='x'",
                    'PRAGMA writable_schema=1']:
            with self.subTest(sql=sql), self.assertRaises(UnsafeQuery):
                run(str(DB), sql)
        for sql in ["SELECT load_extension('/tmp/nope')", "SELECT \"load_extension\"('/tmp/nope')"]:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                run(str(DB), sql)

    def test_original_event_query_executes_without_modification(self):
        rows, _ = run(str(DB), CASES[1]['sql'])
        retail = next(row for row in rows if row['metric'].startswith('Retail'))
        self.assertEqual(retail['value'], '3231')
        self.assertTrue(any('capacity unknown' in row['note'] for row in rows))


class AnswerRegressionTests(unittest.TestCase):
    def test_cdp_answer_highlights_boundary_instead_of_place_point_tract(self):
        with patch.object(main, '_ask_claude', return_value=CASES[2]['sql']):
            result = main.ask(main.Question(question=CASES[2]['question']))
        self.assertEqual(result['geography']['kind'], 'census_place')
        self.assertEqual(result['highlight_scope'], 'boundary')
        self.assertEqual(result['highlight_geoids'], [])
        self.assertIn('Self-identified gender is not measured', result['summary'])

    def test_mixed_venue_and_retail_evidence_does_not_pretend_to_be_a_cdp_count(self):
        rows, sql = run(str(DB), CASES[1]['sql'])
        geo = main._answer_geography(sql, rows, main._sql_geoids(sql))
        self.assertEqual(geo['kind'], 'tract_approximation')
        comparison = main._answer_geography('SELECT * FROM place_data UNION ALL SELECT * FROM industry_employment',
            [{'geoid': '2472450', 'geography_type': 'place'}, {'geoid': '24031', 'geography_type': 'county'}], [])
        self.assertNotEqual(comparison['kind'], 'census_place')

    def test_restaurant_lookup_skips_second_model_call_and_exposes_all_matches(self):
        with patch.object(main, '_ask_claude', return_value=CASES[3]['sql']) as model:
            result = main.ask(main.Question(question=CASES[3]['question']))
        self.assertEqual(model.call_count, 1)
        self.assertIn('tract area used to approximate Fenton Village', result['summary'])
        self.assertEqual(len(result['listing_details']['rows']), result['rows'][0]['restaurant_count'])
        self.assertFalse(result['listing_details']['truncated'])
        self.assertTrue(all(row['source_url'].startswith('https://www.openstreetmap.org/') for row in result['listing_details']['rows']))

    def test_listing_details_cannot_change_a_comparison_into_one_inventory(self):
        self.assertIsNone(main._listing_details(CASES[0]['sql']))
        self.assertIsNone(main._listing_details("SELECT COUNT(*) AS count FROM businesses GROUP BY category"))

    def test_long_details_keep_original_text_while_compact_table_is_readable(self):
        rows, sql = run(str(DB), CASES[0]['sql'])
        raw = rows[1]['detail']
        supporting = main._supporting_data(rows)
        self.assertLess(len(supporting['rows'][1][2]), len(raw))
        self.assertNotIn('foot-traffic proxy', str(supporting))
        self.assertIn('foot-traffic proxy', str(rows))
        self.assertIn('Business listing counts do not measure foot traffic or customer demand.', main._answer_notes(sql, rows, CASES[0]['question']))

    def test_api_exposes_the_database_category_used_for_counts(self):
        points = main.businesses()
        fenton = {r[0] for r in sqlite3.connect(DB).execute("SELECT geoid FROM places WHERE name='Fenton Village'")}
        in_area = [p for p in points if p['geoid'] in fenton]
        self.assertTrue(all('category_group' in point for point in points))
        self.assertEqual(sum(p['category_group'] == 'food and drink' for p in in_area), 78)

    def test_repeat_http_requests_reuse_successful_result_without_another_model_call(self):
        with patch.object(main, 'answer_cache', AnswerCache()), patch.object(main, '_ask_claude', return_value=CASES[3]['sql']) as model:
            first, second = Response(), Response()
            one = main.ask(main.Question(question=CASES[3]['question']), response=first)
            two = main.ask(main.Question(question=CASES[3]['question']), response=second)
        self.assertEqual(one, two)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(first.headers['X-CivicLens-Cache'], 'MISS')
        self.assertEqual(second.headers['X-CivicLens-Cache'], 'HIT')

    def test_release_evaluations_can_bypass_cache(self):
        request = main.Request({'type': 'http', 'headers': [(b'cache-control', b'no-cache')]})
        with patch.object(main, 'answer_cache', AnswerCache()), patch.object(main, '_ask_claude', return_value=CASES[3]['sql']) as model:
            main.ask(main.Question(question=CASES[3]['question']), response=Response())
            main.ask(main.Question(question=CASES[3]['question']), response=Response(), request=request)
        self.assertEqual(model.call_count, 2)


class CacheTests(unittest.TestCase):
    def test_failures_are_not_cached(self):
        cache = AnswerCache()
        cache.put('key', {'answer': 'not configured', 'coverage': 'data exists'})
        self.assertIsNone(cache.get('key'))

    def test_expiry_capacity_and_database_replacement(self):
        cache = AnswerCache(ttl=5, capacity=1)
        payload = {'rows': [{'count': 1}], 'sql': 'SELECT 1'}
        with patch('answers.response_cache.time.monotonic', return_value=1):
            cache.put('a', payload)
            cache.put('b', payload)
            self.assertIsNone(cache.get('a'))
        with patch('answers.response_cache.time.monotonic', return_value=7):
            self.assertIsNone(cache.get('b'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.db'
            path.write_text('before')
            one = cache.key(path, 'model', 'question')
            path.write_text('different snapshot')
            self.assertNotEqual(one, cache.key(path, 'model', 'question'))


if __name__ == '__main__':
    unittest.main()
