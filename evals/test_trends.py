"""Yearly line chart: drawn only from returned rows, with partial years marked."""
import unittest
from unittest.mock import patch

from answers import main


CRIME_SQL = ('SELECT substr("when",1,4) AS year, COUNT(DISTINCT incident_id) AS incident_count '
             'FROM community_crime WHERE "when" IS NOT NULL GROUP BY year ORDER BY year')
CATALOG = [{"table_name": "community_crime", "first_record": "2017-04-02", "last_record": "2026-11-21"}]


def rows(*pairs, **extra):
    return [{"year": year, "incident_count": count, **extra} for year, count in pairs]


class TrendVisualTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main, "used_sources", lambda path, sql: CATALOG if "community_crime" in sql else [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_yearly_counts_become_a_line_of_exactly_the_returned_values(self):
        data = rows(("2019", 2521), ("2017", 1800), ("2018", 2400), ("2026", 2100))
        visual = main._trend_visual(CRIME_SQL, data)
        self.assertEqual(visual["kind"], "line")
        self.assertEqual([p["label"] for p in visual["points"]], ["2017", "2018", "2019", "2026"])
        self.assertEqual([p["value"] for p in visual["points"]], [1800, 2400, 2521, 2100])
        self.assertEqual(visual["points"][2]["display"], "2,521")

    def test_partial_first_and_last_years_are_marked_with_source_dates(self):
        visual = main._trend_visual(CRIME_SQL, rows(("2017", 1), ("2018", 2), ("2025", 3), ("2026", 4)))
        self.assertEqual([p["partial"] for p in visual["points"]], [True, False, False, True])
        self.assertIn("records start Apr 2, 2017", visual["note"])
        self.assertIn("records end Nov 21, 2026", visual["note"])

    def test_mid_year_date_filter_in_the_query_marks_that_year(self):
        sql = CRIME_SQL.replace('IS NOT NULL', ">= '2019-06-01'")
        visual = main._trend_visual(sql, rows(("2019", 1), ("2020", 2), ("2021", 3)))
        self.assertTrue(visual["points"][0]["partial"])
        self.assertIn("starts Jun 1, 2019", visual["note"])

    def test_full_year_filter_does_not_mark_a_partial_year(self):
        sql = CRIME_SQL.replace('IS NOT NULL', ">= '2019-01-01'")
        visual = main._trend_visual(sql, rows(("2019", 1), ("2020", 2), ("2021", 3)))
        self.assertFalse(visual["points"][0]["partial"])

    def test_undated_row_is_left_off_the_chart(self):
        visual = main._trend_visual(CRIME_SQL, rows((None, 9999), ("2019", 1), ("2020", 2), ("2021", 3)))
        self.assertNotIn(9999, [p["value"] for p in visual["points"]])

    def test_annotation_text_column_does_not_block_the_chart(self):
        data = [{"year": "2025", "incident_count": 5, "coverage": "full year"},
                {"year": "2024", "incident_count": 4, "coverage": "full year"},
                {"year": "2026", "incident_count": 3, "coverage": "partial year"}]
        self.assertIsNotNone(main._trend_visual(CRIME_SQL, data))

    def test_several_series_are_not_drawn_as_one_line(self):
        data = [{"year": y, "category": c, "incident_count": 1} for y in ("2019", "2020", "2021")
                for c in ("Theft", "Assault")]
        self.assertIsNone(main._trend_visual(CRIME_SQL, data))

    def test_two_years_is_not_a_trend(self):
        self.assertIsNone(main._trend_visual(CRIME_SQL, rows(("2019", 1), ("2020", 2))))

    def test_rows_without_a_year_column_get_no_line(self):
        data = [{"area": "A", "incident_count": 1}, {"area": "B", "incident_count": 2}, {"area": "C", "incident_count": 3}]
        self.assertIsNone(main._trend_visual(CRIME_SQL, data))

    def test_census_period_history_keeps_its_own_visual(self):
        self.assertIsNone(main._trend_visual("SELECT * FROM tract_history", rows(("2019", 1), ("2020", 2), ("2021", 3))))

    def test_percentages_and_incomes_are_not_drawn_as_counts(self):
        data = [{"year": y, "pct_renters": v} for y, v in (("2019", 1.0), ("2020", 2.0), ("2021", 3.0))]
        self.assertIsNone(main._trend_visual(CRIME_SQL, data))

    def test_answer_visual_prefers_the_trend(self):
        visual = main._answer_visual(CRIME_SQL, rows(("2019", 1), ("2020", 2), ("2021", 3)), None)
        self.assertEqual(visual["kind"], "line")

    def test_note_only_uses_dates_from_the_catalog_or_query(self):
        visual = main._trend_visual(CRIME_SQL, rows(("2017", 1), ("2018", 2), ("2026", 3)))
        import re
        allowed = {"2017", "2", "2026", "21"}
        self.assertTrue(set(re.findall(r"\d+", visual["note"])) <= allowed)


if __name__ == "__main__":
    unittest.main()
