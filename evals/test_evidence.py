"""Evidence queries and the number rules that come with them."""
import sqlite3
import unittest

from answers import main
from answers.sql_guard import UnsafeQuery, validate


class EvidenceTests(unittest.TestCase):
    def test_main_query_and_labelled_evidence_are_split(self):
        text = ("SELECT 1 AS a\n-- evidence: residents for scale\nSELECT 2 AS b\n"
                "-- evidence: county benchmark\nSELECT 3 AS c;")
        main_sql, blocks = main._evidence_blocks(text)
        self.assertEqual(main_sql, "SELECT 1 AS a")
        self.assertEqual(blocks, [("residents for scale", "SELECT 2 AS b"), ("county benchmark", "SELECT 3 AS c")])

    def test_single_query_is_unchanged(self):
        self.assertEqual(main._evidence_blocks("SELECT 1"), ("SELECT 1", []))

    def test_at_most_three_evidence_queries(self):
        text = "SELECT 0" + "".join(f"\n-- evidence: e{i}\nSELECT {i}" for i in range(1, 7))
        self.assertEqual(len(main._evidence_blocks(text)[1]), main.MAX_EVIDENCE)

    def test_non_select_evidence_is_dropped_and_guard_still_applies(self):
        main_sql, blocks = main._evidence_blocks("SELECT 1\n-- evidence: bad\nDROP TABLE places")
        self.assertEqual(blocks, [])
        with self.assertRaises(UnsafeQuery):
            validate("SELECT 1 FROM places WHERE 1 = 1 UNION SELECT 1; DROP TABLE places")

    def test_failing_evidence_query_is_skipped_not_fatal(self):
        found = main._run_evidence("q", [("ok", "SELECT COUNT(*) AS n FROM tract_data"),
                                         ("broken", "SELECT no_such_column FROM tract_data")])
        self.assertEqual([item["label"] for item in found], ["ok"])

    def test_replace_function_allowed_replace_statement_blocked(self):
        validate("SELECT replace(name, 'a', 'b') FROM places")
        with self.assertRaises(UnsafeQuery):
            validate("REPLACE INTO places VALUES (1)")

    def test_rates_per_thousand_can_be_calculated_and_named(self):
        text, calculations = main._apply_calculations("[[calc: 22 / 81462 * 1000]] per 1,000 residents", {22.0, 81462.0})
        self.assertEqual(text, "0.3 per 1,000 residents")
        self.assertTrue(main._uses_returned_numbers("0.3 per 1,000 residents", [{"rate": 0.3}]))
        self.assertFalse(main._uses_returned_numbers("0.3 per 5,000 residents", [{"rate": 0.3}]))

    def test_numbers_that_define_a_queried_measure_are_allowed(self):
        rows = [{"kids": 849.0}]
        used = "SELECT SUM(CASE WHEN measure='age_under_5' THEN estimate END) AS kids FROM acs_measures"
        self.assertTrue(main._uses_returned_numbers("849 children under 5", rows, used))
        self.assertFalse(main._uses_returned_numbers("849 children under 6", rows, used))
        self.assertFalse(main._uses_returned_numbers("849 children under 5", rows, "SELECT 849 AS kids"))

    def test_filtered_years_and_zips_name_scope_but_others_do_not(self):
        sql = "SELECT SUM(total_jobs) t FROM jobs_by_tract WHERE year IN (2018, 2023) AND zip IN ('20910')"
        self.assertTrue(main._uses_returned_numbers("2,244 jobs in 2023 in ZIP 20910", [{"t": 2244}], sql))
        self.assertFalse(main._uses_returned_numbers("2,244 jobs in 2021", [{"t": 2244}], sql))

    def test_context_tables_installed_and_described(self):
        with sqlite3.connect(f"file:{main.DATABASE_PATH}?mode=ro", uri=True) as con:
            silver_spring = con.execute("SELECT estimate FROM acs_measures WHERE geoid='2472450' AND "
                                        "measure='population' AND acs_vintage='ACS 2020-2024'").fetchone()
            jobs = con.execute("SELECT total_jobs FROM jobs_by_tract WHERE geoid='24031' AND year=2023").fetchone()
            long_branch = con.execute("SELECT COUNT(*) FROM places WHERE name='Long Branch'").fetchone()[0]
        self.assertEqual(silver_spring[0], 81462)   # matches place_data
        self.assertGreater(jobs[0], 400000)
        self.assertGreater(long_branch, 0)
        from answers import insight_context
        prompt = insight_context.schema_context(main.DATABASE_PATH)
        for table in ("acs_measures", "jobs_by_tract", "business_patterns", "county_development_pipeline"):
            self.assertIn(table, prompt)


if __name__ == "__main__":
    unittest.main()
