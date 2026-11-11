"""Check the rebuilt snapshot and the API path using independently checked SQL."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from get_data.fetch_businesses import deduplicate_businesses, fetch_businesses
from get_data.validate_database import validate_database

with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "local-test"}):
    from answers import main

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "silverspring.db"
FENTON_TRACTS = "SELECT geoid FROM places WHERE name = 'Fenton Village'"
CASES = {
    "q1-bookstore": f"""SELECT COUNT(*) AS bookstore_count, GROUP_CONCAT(name, ', ') AS bookstore_names,
        MIN(source_url) AS source_url FROM businesses WHERE osm_shop='books' AND geoid IN ({FENTON_TRACTS})""",
    "q2-ecommerce-panel": """SELECT geoid, geography_type, industry, employed_residents, margin_of_error, source_url
        FROM industry_employment WHERE geography_type='place' AND geoid='2472450' AND industry='Retail trade'""",
    "q3-female-residents": """SELECT geoid, place_name, female_count, female_margin_of_error, source_url
        FROM place_data WHERE geoid='2472450'""",
    "q4-restaurants": f"""SELECT COUNT(*) AS restaurant_count, MIN(source_url) AS source_url
        FROM businesses WHERE osm_amenity='restaurant' AND geoid IN ({FENTON_TRACTS})""",
    "population": "SELECT geoid, place_name, population_count, source_url FROM place_data WHERE geoid='2472450'",
}
QUESTIONS = {r["id"]: r["question"] for r in json.loads((ROOT / "evals" / "questions.json").read_text())}
QUESTIONS["population"] = "How many people live in Silver Spring?"


def answer_from_rows(case, rows):
    """Provider stand-in that makes the returned evidence visible in HTTP checks."""
    row = rows[0]
    if case == "q1-bookstore":
        return f"Check demand and rent before deciding. The mapped listings include {row['bookstore_count']} book retailers: {row['bookstore_names']}."
    if case == "q2-ecommerce-panel":
        return f"An estimated {row['employed_residents']:,} Silver Spring residents work in retail. Consider inviting local merchants and making the panel useful for both storefront and online sellers."
    if case == "q3-female-residents":
        return f"The Census estimates {row['female_count']:,} Silver Spring residents are classified as female."
    if case == "q4-restaurants":
        return f"OpenStreetMap lists {row['restaurant_count']} restaurants in the tracts used for Fenton Village."
    return f"The Census estimates {row['population_count']:,} residents in Silver Spring."


def provider_stub(prompt, max_tokens, **_):
    if "These rows came back from the database:" in prompt:
        question, content = prompt.split("\n\nThese rows came back from the database:\n", 1)
        case = next(case for case, text in QUESTIONS.items() if question == f"Question: {text}")
        rows, _ = json.JSONDecoder().raw_decode(content)
        return answer_from_rows(case, rows)
    for case, question in QUESTIONS.items():
        if f"User question: {question}\n" in prompt:
            return CASES[case]
    return "CANNOT_ANSWER"


class DataQualityTests(unittest.TestCase):
    def test_snapshot_has_complete_and_distinct_geographies(self):
        validate_database(DB)
        with closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)) as conn:
            self.assertEqual(conn.execute("SELECT population_count FROM place_data").fetchone(), (81462,))
            placeholders = ",".join("?" * len(main.SILVER_SPRING_GEOIDS))
            total = conn.execute(f"SELECT SUM(population_count) FROM tract_data WHERE geoid IN ({placeholders})",
                                 main.SILVER_SPRING_GEOIDS).fetchone()[0]
            self.assertEqual(total, 81727)
            self.assertEqual(conn.execute("SELECT COUNT(DISTINCT period_end) FROM tract_history").fetchone(), (2,))

    def test_validation_rejects_missing_places_or_missing_history(self):
        for statement, reason in (("DROP TABLE places", "Missing required tables: places"),
                                  ("DELETE FROM tract_history WHERE period_end=2020", "both vintages")):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                copy = Path(directory) / "bad.db"
                shutil.copyfile(DB, copy)
                with closing(sqlite3.connect(copy)) as conn, conn:
                    conn.execute(statement)
                with self.assertRaisesRegex(ValueError, reason):
                    validate_database(copy)

    def test_deduplication_preserves_types_and_separate_locations(self):
        def osm(kind, id, latitude, category, address=None):
            record = {"type": kind, "id": id, "lat": latitude, "lon": -77.02,
                      "tags": {"name": "Sample Venue", "amenity": category}}
            if address:
                record["tags"]["addr:housenumber"] = address
            return record
        rows = fetch_businesses({"elements": [osm("node", 1, 39, "cafe", "1"),
                                              osm("way", 2, 39.0001, "cafe"),
                                              osm("node", 3, 39.0002, "restaurant"),
                                              osm("node", 4, 39, "pub", "3"),
                                              osm("way", 5, 39.0001, "pub", "5")]})
        result = deduplicate_businesses([{**row, "geoid": "24031702502"} for row in rows])
        self.assertEqual(sorted(row["category"] for row in result), ["cafe", "pub", "pub", "restaurant"])
        cafe = next(row for row in result if row["category"] == "cafe")
        self.assertTrue(cafe["duplicate_source_url"].endswith("/way/2"))

    @patch.object(main, "_ask_claude", side_effect=provider_stub)
    def test_four_queries_have_correct_scope_and_source(self, model):
        outcomes = {case: main.ask(main.Question(question=question)) for case, question in QUESTIONS.items()}
        book, retail, sex, restaurant = (outcomes[key] for key in list(CASES)[:4])
        self.assertEqual(book["rows"][0]["bookstore_count"], 2)
        self.assertIn("Mojomala", book["answer"])
        self.assertIn("mapped Census tracts", book["answer"])
        self.assertEqual(retail["rows"][0]["employed_residents"], 3231)
        self.assertIn("employed residents", retail["answer"])
        self.assertTrue(any("C24050?g=160XX00US2472450" in item["url"] for item in retail["sources"]))
        self.assertEqual(sex["rows"][0]["female_count"], 41406)
        self.assertIn("does not measure self-identified gender", sex["answer"])
        self.assertTrue(any("B01001?g=160XX00US2472450" in item["url"] for item in sex["sources"]))
        self.assertNotIn("2472450", sex["highlight_geoids"])
        with closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)) as conn:
            venues = conn.execute(f"SELECT osm_amenity FROM businesses WHERE geoid IN ({FENTON_TRACTS})").fetchall()
        self.assertIn(("cafe",), venues)
        self.assertEqual(restaurant["rows"][0]["restaurant_count"], venues.count(("restaurant",)))
        self.assertIn("mapped Census tracts", restaurant["answer"])
        self.assertIn("OpenStreetMap listings may be incomplete", restaurant["answer"])
        self.assertEqual(outcomes["population"]["rows"][0]["population_count"], 81462)

    @patch.object(main, "_ask_claude", side_effect=[CASES["population"], "There are 99,999 people."])
    def test_unreturned_number_is_not_rendered(self, model):
        response = main.ask(main.Question(question="What is the population of Silver Spring?"))
        self.assertNotIn("99,999", response["answer"])
        self.assertEqual(response["table"][0]["value"], "81,462")

    @patch.object(main, "_ask_claude")
    def test_rates_are_rounded_for_the_summary_and_scope_notes_are_separate(self, model):
        calls = []

        def respond(prompt, max_tokens, **_):
            calls.append(prompt)
            if len(calls) == 1:
                return "SELECT acs_vintage, AVG(public_transit_commuter_count * 100.0 / total_commuter_count) AS transit_pct FROM tract_history GROUP BY acs_vintage"
            _, raw = prompt.split("These rows came back from the database:\n", 1)
            rows, _ = json.JSONDecoder().raw_decode(raw)
            return f"The first period was {rows[0]['transit_pct']}%."

        model.side_effect = respond
        response = main.ask(main.Question(question="What was the average tract transit share?"))
        raw_rate = response["rows"][0]["transit_pct"]
        self.assertNotEqual(raw_rate, round(raw_rate, 1))
        self.assertEqual(response["summary"], f"The first period was {round(raw_rate, 1)}%.")
        self.assertTrue(response["notes"])
        self.assertNotIn(response["notes"][0], response["summary"])
        self.assertIn(response["notes"][0], response["answer"])
        self.assertEqual(response["visual"]["scale_max"], 100)
        self.assertEqual(response["visual"]["items"][0]["display"], f"{raw_rate:.1f}%")
        self.assertIn(f"{raw_rate:.1f}%", response["supporting_data"]["rows"][0])

    @patch.object(main, "_ask_claude")
    def test_grouped_business_counts_answer_directly_from_query_rows(self, model):
        model.return_value = f"""SELECT category, COUNT(*) AS listing_count
            FROM businesses WHERE category IN ('cafe', 'bar') AND geoid IN ({FENTON_TRACTS})
            GROUP BY category"""
        response = main.ask(main.Question(question="How many cafés and bars are mapped around Fenton Village?"))
        self.assertIn("8 café listings", response["summary"])
        self.assertIn("5 bar listings", response["summary"])
        self.assertEqual({point["label"]: point["value"] for point in response["visual"]["items"]},
                         {"Bar": 5, "Café": 8})
        self.assertIsNone(response["next_step"])
        self.assertEqual(model.call_count, 1)

    @patch.object(main, "_ask_claude", return_value=CASES["q3-female-residents"])
    def test_official_sex_estimate_preserves_the_definition(self, model):
        response = main.ask(main.Question(question=QUESTIONS["q3-female-residents"]))
        self.assertIn("41,406", response["summary"])
        self.assertIn("classified as female", response["summary"])
        self.assertIn("does not measure self-identified gender", " ".join(response["notes"]))
        self.assertIsNone(response["visual"])
        self.assertIsNone(response["next_step"])
        self.assertIn("1,495", response["supporting_data"]["rows"][0])
        self.assertEqual(model.call_count, 1)

    def test_decision_separates_the_next_step_and_rejects_unsupported_figures(self):
        question = main.Question(question=QUESTIONS["q1-bookstore"])
        assessment = "Worth exploring. There are 2 mapped book shops in the area."
        with patch.object(main, "_ask_claude", side_effect=[CASES["q1-bookstore"],
                "There are 99,999 mapped book shops.",
                assessment + "\nNext step: Ask local readers what they would buy."]):
            response = main.ask(question)
        self.assertEqual(response["summary"], assessment)
        self.assertEqual(response["next_step"], "Ask local readers what they would buy.")
        self.assertIn(response["next_step"], response["answer"])
        self.assertIsNone(response["visual"])
        with patch.object(main, "_ask_claude", side_effect=[CASES["q1-bookstore"],
                assessment + "\nNext step: This area has 99,999 potential customers."]):
            response = main.ask(question)
        self.assertEqual(response["summary"], assessment)
        self.assertIsNone(response["next_step"])
