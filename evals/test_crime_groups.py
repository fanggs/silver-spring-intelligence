"""Fixed crime definitions: "theft" counts the same categories every time."""
from contextlib import closing
import sqlite3
import unittest

from answers import main
from answers.community_context import CRIME_GROUPS, crime_definitions, crime_group, schema_context


THEFT_SQL = ("SELECT substr(\"when\",1,4) AS year, COUNT(DISTINCT incident_id) AS incident_count "
             "FROM community_crime WHERE category IN ("
             + ", ".join("'" + c + "'" for c in CRIME_GROUPS["theft"]["categories"]) + ") GROUP BY year")


class CrimeGroupTests(unittest.TestCase):
    def test_every_listed_category_exists_in_the_data(self):
        # A renamed county category would silently drop out of the count.
        with closing(sqlite3.connect("file:" + main.DATABASE_PATH + "?mode=ro", uri=True)) as con:
            have = {row[0] for row in con.execute("SELECT DISTINCT category FROM community_crime")}
        for key, group in CRIME_GROUPS.items():
            self.assertEqual(set(group["categories"]) - have, set(), key)

    def test_groups_do_not_overlap(self):
        self.assertFalse(set(CRIME_GROUPS["theft"]["categories"]) & set(CRIME_GROUPS["violent"]["categories"]))

    def test_theft_excludes_separately_counted_offenses(self):
        for other in ("Motor Vehicle Theft", "Robbery", "Burglary/Breaking and Entering", "Identity Theft"):
            self.assertNotIn(other, CRIME_GROUPS["theft"]["categories"])

    def test_query_prompt_carries_the_exact_lists(self):
        prompt = schema_context(main.DATABASE_PATH)
        self.assertIn(crime_definitions(), prompt)
        for group in CRIME_GROUPS.values():
            for category in group["categories"]:
                self.assertIn("'" + category + "'", prompt)

    def test_group_is_recognised_from_the_query(self):
        self.assertEqual(crime_group(THEFT_SQL), "theft")
        partial = THEFT_SQL.replace("'Shoplifting', ", "")
        self.assertIsNone(crime_group(partial))
        self.assertIsNone(crime_group(THEFT_SQL.replace("community_crime", "businesses")))

    def test_answer_notes_say_which_definition_was_counted(self):
        notes = main._answer_notes(THEFT_SQL, [{"year": "2019", "incident_count": 1}], "Is theft going up?")
        self.assertIn(CRIME_GROUPS["theft"]["note"], notes)

    def test_definition_notes_carry_no_numbers(self):
        import re
        for group in CRIME_GROUPS.values():
            self.assertIsNone(re.search(r"\d", group["note"]))


if __name__ == "__main__":
    unittest.main()
