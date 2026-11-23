"""Gyms, hotels, offices and trades are loaded from OpenStreetMap too."""
import sqlite3
import unittest

from answers import main
from get_data.fetch_businesses import QUERY, fetch_businesses


def element(osm_id, **tags):
    return {"type": "node", "id": osm_id, "lat": 39.0, "lon": -77.0, "tags": {"name": f"Place {osm_id}", **tags}}


class BusinessTypeTests(unittest.TestCase):
    def parse(self, *elements):
        return {row["source_url"].rsplit("/", 1)[1]: row for row in fetch_businesses({"elements": list(elements)})}

    def test_new_types_are_parsed_with_their_own_column_and_group(self):
        rows = self.parse(element(1, leisure="fitness_centre"), element(2, tourism="hotel"),
                          element(3, office="lawyer"), element(4, craft="brewery"))
        self.assertEqual((rows["1"]["category"], rows["1"]["category_group"], rows["1"]["osm_leisure"]),
                         ("fitness centre", "recreation and fitness", "fitness_centre"))
        self.assertEqual(rows["2"]["category_group"], "lodging and attractions")
        self.assertEqual((rows["3"]["category"], rows["3"]["osm_office"]), ("lawyer", "lawyer"))
        self.assertEqual(rows["4"]["category_group"], "trades and crafts")

    def test_non_business_leisure_and_tourism_are_left_out(self):
        rows = self.parse(element(5, leisure="playground"), element(6, leisure="pitch"), element(7, tourism="information"))
        self.assertEqual(rows, {})

    def test_existing_types_keep_priority(self):
        rows = self.parse(element(8, shop="books", office="company"), element(9, amenity="restaurant", tourism="hotel"))
        self.assertEqual((rows["8"]["category"], rows["8"]["osm_office"]), ("books", None))
        self.assertEqual((rows["9"]["category_group"], rows["9"]["osm_tourism"]), ("food and drink", None))

    def test_full_rebuild_query_asks_for_every_type(self):
        for key in ('"amenity"', '"shop"', '"leisure"', '"office"', '"tourism"', '"craft"'):
            self.assertIn(key, QUERY)

    def test_database_has_gyms_and_unchanged_restaurants(self):
        with sqlite3.connect(f"file:{main.DATABASE_PATH}?mode=ro", uri=True) as con:
            gyms = con.execute("SELECT COUNT(*) FROM businesses WHERE osm_leisure='fitness_centre'").fetchone()[0]
            hotels = con.execute("SELECT COUNT(*) FROM businesses WHERE osm_tourism='hotel'").fetchone()[0]
            restaurants = con.execute("SELECT COUNT(*) FROM businesses WHERE osm_amenity='restaurant'").fetchone()[0]
        self.assertGreater(gyms, 20)
        self.assertGreater(hotels, 10)
        self.assertEqual(restaurants, 758)


if __name__ == "__main__":
    unittest.main()
