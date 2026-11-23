"""Live progress for /ask/stream, and counts code gives the model.

Run: python -m unittest discover -s evals -p 'test_*.py' -v
No network: the model is replaced with a stub.
"""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from answers import main

SS = "', '".join(main.SILVER_SPRING_GEOIDS)
CAFES_SQL = ("SELECT b.name, pl.opening_hours, b.source_url, COUNT(*) OVER () AS total_count "
             "FROM businesses b LEFT JOIN place_locations pl ON pl.source_url = b.source_url "
             f"WHERE b.osm_amenity = 'cafe' AND b.geoid IN ('{SS}') ORDER BY b.name")


class HoursCountTests(unittest.TestCase):
    def test_code_counts_places_with_no_hours(self):
        rows = [{"name": "A", "opening_hours": "Mo-Su 08:00-21:00"},
                {"name": "B", "opening_hours": None},
                {"name": "C", "opening_hours": ""}]
        facts, values = main._exact_totals(rows, "SELECT name, opening_hours FROM businesses")
        self.assertIn("2 of the 3 listed places have no opening hours mapped (unknown, not closed).", facts)
        self.assertTrue({2.0, 3.0} <= values)

    def test_no_hours_count_for_grouped_or_cut_off_rows(self):
        rows = [{"category": "cafe", "opening_hours": None}, {"category": "bar", "opening_hours": "x"}]
        facts, _ = main._exact_totals(rows, "SELECT category, opening_hours FROM b GROUP BY category")
        self.assertFalse(any("opening hours" in f for f in facts))
        cut = [{"name": str(i), "opening_hours": None} for i in range(200)]
        facts, _ = main._exact_totals(cut, "SELECT name, opening_hours FROM b LIMIT 200")
        self.assertFalse(any("opening hours" in f for f in facts))


class StreamTests(unittest.TestCase):
    def setUp(self):
        main.answer_cache = main.AnswerCache()

    def events(self, question, replies):
        calls = iter(replies)
        with patch.object(main, "_ask_claude", side_effect=lambda *a, **k: next(calls)), \
             patch.object(main, "_log_gap"):
            response = TestClient(main.app).post("/ask/stream", json={"question": question})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/x-ndjson")
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def test_steps_arrive_before_the_same_result_as_ask(self):
        events = self.events("Which cafes are open on Sunday?",
                             [CAFES_SQL, "Several cafes have Sunday hours mapped. Check hours before visiting."])
        self.assertEqual([e["stage"] for e in events[:-1]], ["query", "database", "answer"])
        result = events[-1]["result"]
        self.assertTrue(result["summary"].startswith("Several cafes"))
        self.assertTrue(result["rows"])

    def test_a_rejected_draft_shows_the_rewrite_step(self):
        events = self.events("Which cafes are open on Sunday?",
                             [CAFES_SQL, "There are 99 cafes.", "Several cafes have Sunday hours mapped."])
        self.assertEqual([e.get("stage") for e in events[:-1]], ["query", "database", "answer", "rewrite"])
        self.assertNotIn("99", events[-1]["result"]["summary"])

    def test_a_failing_query_gets_one_repair(self):
        bad = CAFES_SQL.replace("ORDER BY b.name", "ORDER BY no_such_column")
        events = self.events("Which cafes are open on Sunday?",
                             [bad, CAFES_SQL, "Several cafes have Sunday hours mapped."])
        self.assertEqual([e.get("stage") for e in events[:-1]], ["query", "database", "repair", "answer"])
        self.assertEqual(events[-1]["result"]["sql"].strip().rstrip(";"), CAFES_SQL)

    def test_a_query_that_fails_twice_is_reported_plainly(self):
        bad = CAFES_SQL.replace("ORDER BY b.name", "ORDER BY no_such_column")
        result = self.events("Which cafes are open on Sunday?", [bad, bad])[-1]["result"]
        self.assertTrue(result["answer"].startswith("I couldn't retrieve"))

    def test_a_server_error_ends_with_an_error_line(self):
        with patch.object(main, "_answer_question", side_effect=RuntimeError("boom")):
            response = TestClient(main.app).post("/ask/stream", json={"question": "anything"})
        self.assertEqual(json.loads(response.text.splitlines()[-1]),
                         {"error": "The server could not answer this question right now."})

    def test_health_names_the_model(self):
        self.assertEqual(TestClient(main.app).get("/health").json()["model"], main.MODEL)

    def test_health_names_the_running_commit(self):
        self.assertEqual(TestClient(main.app).get("/health").json()["commit"], main.RUNNING_COMMIT)


if __name__ == "__main__":
    unittest.main()
