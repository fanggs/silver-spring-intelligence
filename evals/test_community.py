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

    def test_point_and_building_of_one_place_are_merged(self):
        from get_data.import_community import merge_point_and_building
        base = {'category': 'restaurant', 'geoid': '24031702502'}
        node = dict(base, name='The Big Greek Cafe', osm_id='node/1', latitude=38.9920286, longitude=-77.0262992, cuisine='greek')
        way = dict(base, name='The Big Greek Cafe', osm_id='way/2', latitude=38.9924096, longitude=-77.0262884, cuisine=None)
        merged = merge_point_and_building([node, way])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['osm_id'], 'node/1')
        self.assertEqual(merged[0]['cuisine'], 'greek')
        self.assertEqual(merged[0]['duplicate_source_url'], 'https://www.openstreetmap.org/way/2')
        # Kept apart, as in the primary table: too far apart, two points, or three buildings.
        far = dict(way, latitude=38.9915)
        self.assertEqual(len(merge_point_and_building([node, far])), 2)
        self.assertEqual(len(merge_point_and_building([node, dict(node, osm_id='node/3')])), 2)
        campus = [dict(base, name='NOAA', osm_id=f'way/{i}', latitude=38.99, longitude=-77.03) for i in range(3)]
        self.assertEqual(len(merge_point_and_building(campus)), 3)

    def test_both_business_tables_agree_on_fenton_restaurants(self):
        fenton = "(SELECT geoid FROM places WHERE LOWER(name) = 'fenton village')"
        with closing(sqlite3.connect(DB)) as con:
            primary = con.execute(f"SELECT COUNT(*) FROM businesses WHERE osm_amenity='restaurant' AND geoid IN {fenton}").fetchone()[0]
            partner = con.execute(f"SELECT COUNT(*) FROM community_businesses WHERE category='restaurant' AND geoid IN {fenton}").fetchone()[0]
        self.assertEqual(primary, partner)

    def test_missing_model_is_not_reported_as_missing_data(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':''}), patch.object(main,'client',None):
            result=main.ask(main.Question(question='Which venues are available?'))
        self.assertIn('not configured',result['answer'])
        self.assertIn('data is available',result['answer'])

    def test_returned_dates_and_capacity_text_can_be_described(self):
        self.assertTrue(main._uses_returned_numbers('Issued 2026-10-28; capacity 120.', [{'issued':'2026-10-28','capacity':'120'}]))
        self.assertFalse(main._uses_returned_numbers('There are 99999 venues.', [{'source_url':'https://example.com/99999'}]))

    def test_count_sentence_names_the_filtered_category(self):
        sql = "SELECT COUNT(*) AS listing_count, MIN(source_url) AS source_url FROM businesses WHERE osm_amenity = 'restaurant' AND geoid IN (SELECT geoid FROM places WHERE LOWER(name) = LOWER('Fenton Village'))"
        sentence = main._single_listing_count('How many restaurants are in Fenton Village?', sql, [{'listing_count': 43, 'source_url': 'x'}])
        self.assertEqual(sentence, 'There are 43 mapped restaurant listings in the tract area used to approximate Fenton Village.')

    def test_mixed_venue_count_gets_a_database_breakdown(self):
        area = "geoid IN (SELECT geoid FROM places WHERE LOWER(name) LIKE '%silver spring%')"
        with closing(sqlite3.connect(DB)) as con:
            n = dict(con.execute(f"SELECT category, COUNT(*) FROM community_venues WHERE {area} "
                                 "AND category IN ('hotel','community_centre','events_venue') GROUP BY category").fetchall())
        total = sum(n.values())
        sql = (f"SELECT 'Venues' AS metric, COUNT(*) AS value FROM community_venues v WHERE v.{area} "
               "AND v.category IN ('hotel','community_centre','events_venue') "
               f"UNION ALL SELECT 'Hotels' AS metric, COUNT(*) AS value FROM community_venues v WHERE v.{area} "
               "AND v.category = 'hotel'")
        rows = [{'metric': 'Venues', 'value': total}, {'metric': 'Hotels', 'value': n['hotel']}]
        facts, values = main._category_breakdowns(sql, rows)
        self.assertIn(f"{n['hotel']} hotel", facts[0])
        self.assertIn('Venues', facts[0])
        self.assertIn(float(n['hotel']), values)
        # The hotels row sits inside the venue total: flagged, never added.
        self.assertTrue(any('already included' in f for f in facts))

    def test_breakdown_is_dropped_when_it_does_not_reconcile(self):
        sql = "SELECT COUNT(*) AS value FROM community_venues WHERE category IN ('hotel','museum')"
        facts, values = main._category_breakdowns(sql, [{'value': 999}])
        self.assertEqual((facts, values), ([], set()))

    def test_guard_reports_which_numbers_failed(self):
        missing = []
        ok = main._uses_returned_numbers("No, there aren't 100 cafes; there are 16.", [{'cafe_count': 16}], None, (), missing)
        self.assertFalse(ok)
        self.assertEqual(missing, ['100'])

    def test_mixed_measures_in_a_union_get_a_neutral_column(self):
        # The live b3d52ec/01f396d query: venue counts stacked under employment.
        sql = ("SELECT 'Retail' AS label, employed_residents AS employed_residents FROM industry_employment "
               "UNION ALL SELECT category AS label, COUNT(*) AS employed_residents FROM community_venues GROUP BY category")
        rows = [{'label': 'Retail', 'employed_residents': 3231}, {'label': 'Candidate event venue: hotel', 'employed_residents': 5}]
        fixed = main._neutral_union_columns(rows, sql)
        self.assertEqual(fixed[1], {'label': 'Candidate event venue: hotel', 'value': 5})
        # One table: the column means one thing, so it keeps its name.
        same = "SELECT name, capacity FROM community_venues UNION ALL SELECT name, capacity FROM community_venues"
        self.assertEqual(main._neutral_union_columns([{'name': 'x', 'capacity': 3}], same), [{'name': 'x', 'capacity': 3}])

    def test_a_hotel_is_rejected_when_the_data_has_five(self):
        rows = [{'label': 'Candidate event venue: community_centre', 'value': 2},
                {'label': 'Candidate event venue: events_venue', 'value': 1},
                {'label': 'Candidate event venue: hotel', 'value': 5}]
        missing = []
        self.assertFalse(main._uses_returned_numbers('Venues include a community centre, an events venue, and a hotel.', rows, None, (), missing))
        self.assertEqual(len(missing), 2)  # the centre and the hotel; 1 events venue is right
        self.assertTrue(main._uses_returned_numbers('Venues include 2 community centres, an events venue, and 5 hotels.', rows))
        # A list of individual places: each row is one place; total_count is the list size.
        listed = [{'name': 'Silver Spring Recreation and Aquatic Center', 'category': 'community_centre', 'total_count': 7}]
        self.assertTrue(main._uses_returned_numbers('The 7 include a community centre.', listed))
        # A category stored in a column the query happened to call geoid.
        odd = [{'measure': 'Venues', 'geoid': 'community_centre', 'value': 2}]
        self.assertFalse(main._uses_returned_numbers('Venues include a community centre.', odd))

    def test_counts_are_never_presented_as_foot_traffic_or_demand(self):
        rows = [{'food_and_drink_count': 78}]
        bad = 'Fenton has 78 food and drink listings, suggesting solid foot traffic in the area.'
        self.assertFalse(main._uses_returned_numbers(bad, rows))
        self.assertFalse(main._uses_returned_numbers('Fenton has 78 food and drink listings, so the area has strong foot traffic potential.', rows))
        self.assertFalse(main._uses_returned_numbers('Food and drink dominates with 78 listings, so retail foot traffic exists, but bookstore demand is unknown.', rows))
        for fine in ['Fenton has 78 food and drink listings, but demand is unknown.',
                     "This count alone doesn't tell you about foot traffic.",
                     'Mapped listings cannot show customer demand or foot traffic.']:
            self.assertTrue(main._uses_returned_numbers(fine, rows), fine)
        # A next step asking about foot traffic is advice, not a claim.
        self.assertTrue(main._uses_returned_numbers('Ask staff how busy foot traffic typically is.', rows, claims=False))

    def test_numbers_written_as_words_are_checked_too(self):
        # The live answer on b3d52ec: the rows listed 5 hotels, the text said "six".
        hotels = [{'label': f'Candidate event venue: Hotel {i} (hotel)'} for i in range(5)]
        rows = hotels + [{'hotel_count': 5, 'community_centre_count': 2}]
        self.assertFalse(main._uses_returned_numbers('Options include two community centres and six hotels.', rows))
        self.assertTrue(main._uses_returned_numbers('Options include two community centres and five hotels.', rows))
        self.assertTrue(main._uses_returned_numbers('Options include 2 community centres and 5 hotels.', rows))
        self.assertEqual(main._word_numbers('twenty-one stops, thirty two shops, three hundred permits, a dozen cafes'),
                         {21.0, 32.0, 300.0, 12.0})
        # "one" is ordinary language, and names from the data are returned evidence.
        self.assertTrue(main._uses_returned_numbers('Ask one specific question at Five Guys.', [{'name': 'Five Guys'}]))
        self.assertFalse(main._uses_returned_numbers('There are seven bookstores.', [{'count': 2}]))

    def test_year_filtered_by_query_can_name_scope_but_nothing_else(self):
        rows = [{'incidents': 3635}]
        sql = """SELECT COUNT(*) AS incidents FROM community_crime WHERE "when" >= '2025-01-01' AND "when" < '2026-01-01' LIMIT 500"""
        self.assertTrue(main._uses_returned_numbers('In 2025, 3,635 incidents were reported.', rows, sql))
        self.assertFalse(main._uses_returned_numbers('In 2025, 3,635 incidents were reported.', rows))
        # A bare number in the SQL (a LIMIT, a code) is not evidence.
        self.assertFalse(main._uses_returned_numbers('There are 500 incidents.', rows, sql))
        self.assertFalse(main._uses_returned_numbers('In 2024, 3,635 incidents.', rows, sql))

    def test_code_computed_totals_allow_correct_math_only(self):
        # Two bookstores returned as two rows of 1: "2" is correct, "3" is not.
        rows = [{'bookstore_count': 1, 'name': 'A'}, {'bookstore_count': 1, 'name': 'B'}]
        facts, values = main._exact_totals(rows, 'SELECT ... GROUP BY source_url')
        self.assertTrue(main._uses_returned_numbers('There are 2 bookstores.', rows, None, values))
        self.assertFalse(main._uses_returned_numbers('There are 3 bookstores.', rows, None, values))
        self.assertTrue(any('2' in fact for fact in facts))

    def test_record_count_only_when_result_is_complete(self):
        rows = [{'name': str(i)} for i in range(7)]
        _, complete = main._exact_totals(rows, 'SELECT name FROM community_venues')
        self.assertIn(7.0, complete)
        # A LIMIT that was reached means there may be more: no count allowed.
        _, cut = main._exact_totals(rows, 'SELECT name FROM community_venues LIMIT 7')
        self.assertNotIn(7.0, cut)
        _, capped = main._exact_totals([{'name': 'x'}] * 200, 'SELECT name FROM businesses')
        self.assertNotIn(200.0, capped)

    def test_cut_off_results_are_detected_and_explained(self):
        seven = [{'name': str(i)} for i in range(7)]
        self.assertIsNone(main._result_limit(seven, 'SELECT name FROM v LIMIT 20'))  # under the limit: complete
        self.assertEqual(main._result_limit(seven, 'SELECT name FROM v LIMIT 7'), {'shown': 7, 'total': None})
        self.assertEqual(main._result_limit([{}] * 200, 'SELECT name FROM v')['shown'], 200)  # app's 200-row cap
        # The database counted every match before the cut-off: say exactly how many.
        counted = [{'name': 'x', 'total_count': 461}] * 200
        self.assertEqual(main._result_limit(counted, 'SELECT ... LIMIT 500'), {'shown': 200, 'total': 461})
        self.assertEqual(main.limited_results_note({'shown': 200, 'total': 461}),
                         "Showing the first 200 of 461 results. The other 261 aren't listed here.")
        self.assertIn('There may be more', main.limited_results_note({'shown': 200, 'total': None}))
        # A total_count proving nothing is missing means it is complete.
        self.assertIsNone(main._result_limit([{'total_count': 7}] * 7, 'SELECT ... LIMIT 7'))
        # "Top 5" asked for 5: not a cut-off.
        top = [{'category': str(i), 'listing_count': 9} for i in range(5)]
        self.assertIsNone(main._result_limit(top, 'SELECT ... LIMIT 5', question='What are the top 5 categories?'))
        self.assertIsNone(main._result_limit(top, 'SELECT ... LIMIT 5', question='Which five most common categories?'))
        self.assertIsNotNone(main._result_limit(top, 'SELECT ... LIMIT 5', question='List every category'))

    def test_medians_and_rates_are_never_summed(self):
        rows = [{'median_income': 50000, 'transit_pct': 20.0}, {'median_income': 70000, 'transit_pct': 10.0}]
        _, values = main._exact_totals(rows, 'SELECT median_income, transit_pct FROM tract_data')
        self.assertNotIn(120000.0, values)
        self.assertNotIn(30.0, values)

    def test_code_calculates_any_operation_on_returned_numbers(self):
        rows = [{'transit_2020': 11515, 'commuters_2020': 46794}, {'transit_2024': 6475, 'commuters_2024': 46937}]
        operands = main._returned_numbers(rows)
        text, calcs = main._apply_calculations(
            'Transit fell from [[calc: 11515 / 46794 * 100]]% to [[calc: 6475 / 46937 * 100]]%, '
            'a drop of [[calc: 11515 - 6475]] riders, or [[calc: (6475 - 11515) / 11515 * 100]]%.', operands)
        self.assertEqual(text, 'Transit fell from 24.6% to 13.8%, a drop of 5,040 riders, or -43.8%.')
        self.assertTrue(all('result' in c for c in calcs))
        computed = {c['result'] for c in calcs}
        self.assertTrue(main._uses_returned_numbers(text, rows, None, computed))

    def test_calculation_refuses_invented_numbers_and_tricks(self):
        operands = {11515.0, 46794.0, 0.0}
        for expression in ['11515 / 40000', '11515 * 2', '__import__("os")', 'max(11515, 46794)', '11515 / 0', '11515 ** 2']:
            text, calcs = main._apply_calculations(f'Value [[calc: {expression}]].', operands)
            self.assertIn(main.CALC_ERROR, text, expression)
            self.assertIn('error', calcs[0])
            self.assertFalse(main._uses_returned_numbers(text, [{'a': 11515}]))

    def test_unverified_sentence_and_failed_calc_are_dropped(self):
        rows = [{'value': 3231}]
        text, _ = main._apply_calculations('About 3,231 Silver Spring residents work in retail. That is [[calc: 3231 / 5000]] of all.', {3231.0})
        self.assertEqual(main._verified_sentences(text, rows), 'About 3,231 Silver Spring residents work in retail.')

    def test_gap_log_records_reason_and_question(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'gaps.jsonl'
            old = os.environ.get('CIVICLENS_GAP_LOG')
            os.environ['CIVICLENS_GAP_LOG'] = str(path)
            try:
                main._log_gap('How many dentists are there?', 'no_data_for_question')
            finally:
                if old is None:
                    os.environ.pop('CIVICLENS_GAP_LOG')
                else:
                    os.environ['CIVICLENS_GAP_LOG'] = old
            record = json.loads(path.read_text().splitlines()[0])
            self.assertEqual(record['reason'], 'no_data_for_question')
            self.assertEqual(record['question'], 'How many dentists are there?')

    def test_fallback_keeps_only_sentences_with_returned_figures(self):
        rows = [{'value': 3231, 'margin_of_error': 537}, {'value': None}]
        text = 'Yes: 3,231 Silver Spring residents work in retail trade. There are 11 candidate venues nearby. Capacity is not published for any of them.'
        kept = main._verified_sentences(text, rows)
        self.assertEqual(kept, 'Yes: 3,231 Silver Spring residents work in retail trade. Capacity is not published for any of them.')
        self.assertNotIn('11', kept)
        self.assertIsNone(main._verified_sentences('There are 11 venues. Yes.', rows))

    def test_query_timeout_is_elapsed_time_not_instruction_estimate(self):
        with self.assertRaises(sqlite3.OperationalError):
            run(str(DB),'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n',timeout_seconds=0.01)
