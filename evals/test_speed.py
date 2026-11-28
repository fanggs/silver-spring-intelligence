"""Speed changes (Nov 27): early map preview and time limits.

Run: python -m unittest discover -s evals -p 'test_*.py' -v
No network: the model is replaced with a stub.
"""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from answers import main

SS = "', '".join(main.SILVER_SPRING_GEOIDS)
SHORT_SQL = ("SELECT b.name, b.source_url, COUNT(*) OVER () AS total_count FROM businesses b "
             f"WHERE b.osm_amenity = 'cafe' AND b.geoid IN ('{SS}') ORDER BY b.name")


class StreamPreviewTests(unittest.TestCase):
    def setUp(self):
        main.answer_cache = main.AnswerCache()

    def events(self, replies):
        calls = iter(replies)
        with patch.object(main, "_ask_claude", side_effect=lambda *a, **k: next(calls)), \
             patch.object(main, "_log_gap"):
            response = TestClient(main.app).post("/ask/stream", json={"question": "Which cafes are in Silver Spring?"})
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    def test_data_preview_arrives_before_the_answer_and_matches_it(self):
        events = self.events([SHORT_SQL, "Several cafes are mapped. Their addresses are in the list below."])
        answer_step = next(e for e in events if e.get("stage") == "answer")
        preview, result = answer_step["preview"], events[-1]["result"]
        # No model-written text in the preview: only what code built from rows.
        self.assertIsNone(preview["summary"])
        self.assertIsNone(preview["next_step"])
        for key in ("rows", "supporting_data", "highlight_geoids", "highlight_scope", "sources", "sql", "visual"):
            self.assertEqual(preview[key], result[key], key)
        self.assertTrue(result["summary"].startswith("Several cafes"))


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        main.answer_cache = main.AnswerCache()

    def test_no_rewrite_after_the_deadline(self):
        calls = iter([SHORT_SQL, "There are 99 cafes. Check hours before visiting."])
        clock = iter([0.0] + [main.REWRITE_DEADLINE + 1] * 50)
        with patch.object(main, "_ask_claude", side_effect=lambda *a, **k: next(calls)) as ask, \
             patch.object(main, "_log_gap"), patch.object(main.time, "perf_counter", side_effect=lambda: next(clock)):
            result = main._answer_question(main.Question(question="Which cafes are in Silver Spring?"))
        self.assertEqual(ask.call_count, 2)          # no second draft
        self.assertNotIn("99", result["summary"])    # the failing sentence is still removed

    def test_each_model_call_has_a_time_limit(self):
        calls = iter([SHORT_SQL, "Several cafes are mapped."])
        with patch.object(main, "_ask_claude", side_effect=lambda *a, **k: next(calls)) as ask, \
             patch.object(main, "_log_gap"):
            main._answer_question(main.Question(question="Which cafes are in Silver Spring?"))
        timeouts = [call.kwargs.get("timeout") for call in ask.call_args_list]
        self.assertEqual(timeouts, [main.SQL_TIMEOUT, main.ANSWER_TIMEOUT])


if __name__ == "__main__":
    unittest.main()


class BackgroundAndCacheTests(unittest.TestCase):
    def test_answers_are_kept_for_a_day_within_one_data_snapshot(self):
        self.assertEqual(main.AnswerCache().ttl, 86400)

    def test_no_background_work_outside_the_deployed_service(self):
        with patch.dict(main.os.environ, {"RENDER": "", "CIVICLENS_BACKGROUND": ""}), \
             patch.object(main.threading, "Thread") as thread:
            main._start_background()
        thread.assert_not_called()
