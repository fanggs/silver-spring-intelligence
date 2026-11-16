"""Answers name areas the way people do, never "Census Tract 7016.02".

Run: python -m unittest discover -s evals -p 'test_*.py' -v
No network: the model is replaced with a stub.
"""
import json
import re
import unittest
from unittest.mock import patch

from answers import main

GROCERY_SQL = ("SELECT t.geoid, t.tract_name, t.population_count, "
               "(SELECT COUNT(*) FROM businesses b WHERE b.geoid = t.geoid AND b.osm_shop = 'supermarket') "
               "AS grocery_listing_count, t.source_url FROM tract_data t WHERE t.geoid IN ('24031701602', "
               "'24031702700', '24031701601') ORDER BY grocery_listing_count, t.population_count DESC")
TRACT = re.compile(r"\btracts?\b", re.I)


class AreaLabelTests(unittest.TestCase):
    def test_every_tract_has_a_name_without_tract_numbers(self):
        labels = main._area_labels()
        with main.sqlite3.connect(main.DATABASE_PATH) as db:
            geoids = [g for (g,) in db.execute("SELECT geoid FROM tract_data")]
        self.assertEqual(set(labels), set(geoids))
        for label in labels.values():
            self.assertFalse(TRACT.search(label), label)
            self.assertNotRegex(label, r"\d{4}")

    def test_silver_spring_areas_use_neighbourhood_names(self):
        labels = main._area_labels()
        self.assertEqual(labels["24031701602"], "Northwest Park / Oakview")
        self.assertEqual(labels["24031701601"], "near Oakview")
        self.assertIn("Lyttonsville", labels["24031702700"])
        self.assertEqual(len({labels[g] for g in main.SILVER_SPRING_GEOIDS}), len(main.SILVER_SPRING_GEOIDS))

    def test_model_rows_name_the_area_and_drop_tract_numbers(self):
        rows = main._with_area_names([
            {"geoid": "24031701602", "tract_name": "Census Tract 7016.02; Montgomery County; Maryland", "population_count": 7158},
            {"tract_name": "Census Tract 7027; Montgomery County; Maryland"},
            {"source_tract_id": "24031701600", "tract_name": "Census Tract 7016, Montgomery County, Maryland"},
            {"geoid": "24031702000", "median_income": 50000}])
        self.assertEqual(rows[0], {"geoid": "24031701602", "area": "Northwest Park / Oakview", "population_count": 7158})
        self.assertIn("Lyttonsville", rows[1]["area"])
        self.assertEqual(rows[2], {"area": "Northwest Park / Oakview"})
        self.assertEqual(rows[3]["area"], "Browns Corner / New Hampshire Estates")
        self.assertNotIn("Census Tract", json.dumps(rows))
        self.assertNotIn("7016.02", json.dumps(rows))

    def test_last_line_of_defence(self):
        self.assertEqual(main._no_tract_words("Across 19 Census tracts, one tract is low."),
                         "Across 19 Census areas, one area is low.")


class AnswerTests(unittest.TestCase):
    def ask(self, replies):
        calls = iter(replies)
        with patch.object(main, "_ask_claude", side_effect=lambda *a, **k: next(calls)), \
             patch.object(main, "_log_gap"):
            return main._answer_question(main.Question(question="I want to open a grocery store. Where is there less competition?"))

    def test_an_answer_that_names_a_tract_is_rewritten(self):
        result = self.ask([GROCERY_SQL,
                           "Census Tract 7016.02 (7,158 residents) has no mapped grocery listings.",
                           "Northwest Park / Oakview (7,158 residents) has no mapped grocery listings."])
        self.assertTrue(result["summary"].startswith("Northwest Park / Oakview"))
        self.assertNotIn("7016", result["summary"])

    def test_nothing_on_screen_says_tract(self):
        result = self.ask([GROCERY_SQL, "Few areas have mapped grocery listings; the tracts vary. Next step: walk the tract."])
        shown = " ".join([result["answer"], json.dumps(result["supporting_data"]), json.dumps(result["sources"]),
                          json.dumps(result["table"]), json.dumps(result.get("visual")), json.dumps(result.get("statistic"))])
        self.assertFalse(TRACT.search(shown.replace("tract_", "")), shown)
        # The raw rows keep the published tract for anyone checking the source.
        self.assertIn("Census Tract", json.dumps(result["rows"]))


if __name__ == "__main__":
    unittest.main()
