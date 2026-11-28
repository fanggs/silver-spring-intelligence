"""Fair comparisons between areas (Nov 27).

An answer said Fenton Village's 2 bookstores were "fewer than the 3 across
Silver Spring", though Fenton Village is part of Silver Spring, and some rates
divided tract counts by the official place population (another boundary).
These check the guidance that prevents both stays in the prompts.
Run: python -m unittest discover -s evals -p 'test_*.py' -v
"""
import inspect
import unittest

from answers import insight_context, main


class ComparisonGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.query_guidance = insight_context.schema_context(main.DATABASE_PATH)

    def test_rates_use_the_same_area_for_count_and_population(self):
        self.assertIn("must cover the same area", self.query_guidance)
        self.assertIn("never by the official\n  place population", self.query_guidance)

    def test_a_district_is_compared_with_the_rest_of_silver_spring(self):
        self.assertIn("'Rest of Silver Spring'", self.query_guidance)

    def test_rates_never_round_to_zero(self):
        self.assertIn("per 10,000 residents when a rate is below 0.1", self.query_guidance)

    def test_answers_compare_rates_not_raw_counts(self):
        source = inspect.getsource(main._answer_question)
        self.assertIn("never by raw counts", source)


if __name__ == "__main__":
    unittest.main()
