"""Addresses and map locations for listed places, and model-call limits.

Run: python -m unittest discover -s evals -p 'test_*.py' -v
No network: OpenStreetMap and the model are replaced with stubs.
"""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from answers import main
from get_data import fetch_addresses

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "silverspring.db"


class AddressBuildTests(unittest.TestCase):
    def test_address_uses_only_tagged_parts(self):
        self.assertEqual(fetch_addresses.format_address({
            "addr:housenumber": "8401", "addr:street": "Colesville Road", "addr:unit": "140",
            "addr:city": "Silver Spring", "addr:state": "MD", "addr:postcode": "20910"}),
            "8401 Colesville Road, Unit 140, Silver Spring, MD 20910")
        self.assertEqual(fetch_addresses.format_address({"addr:housenumber": "8317", "addr:street": "Fenton Street"}),
                         "8317 Fenton Street")
        # A street with no number, or a number with no street, is not an address.
        self.assertIsNone(fetch_addresses.format_address({"addr:street": "Fenton Street"}))
        self.assertIsNone(fetch_addresses.format_address({"addr:housenumber": "12"}))

    def test_point_in_building_outline(self):
        square = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
        self.assertTrue(fetch_addresses.point_in_ring(0.5, 0.5, square))
        self.assertFalse(fetch_addresses.point_in_ring(1.5, 0.5, square))

    def test_build_keeps_every_place_and_records_where_each_address_came_from(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "copy.db"
            shutil.copy(DB, copy)
            with closing(sqlite3.connect(copy)) as con:
                places = fetch_addresses.collect_places(con)
            refs = sorted(places)
            own, inside = refs[0], refs[1]

            def tags(wanted):
                return {own: {"addr:housenumber": "1", "addr:street": "Test Street"}}

            def buildings(points):
                return ({inside: "2 Building Road"} if inside in points else {}), 0

            with patch.object(fetch_addresses, "validate_database"):
                counts = fetch_addresses.build_locations(copy, fetch_tags=tags, fetch_buildings=buildings, buildings="all")
            with closing(sqlite3.connect(copy)) as con:
                stored = dict(((r[0], (r[1], r[2])) for r in con.execute("SELECT osm_ref, address, address_basis FROM place_locations")))
                businesses = con.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
            self.assertEqual(len(stored), len(places))
            self.assertEqual(stored[own], ("1 Test Street", "listing"))
            self.assertEqual(stored[inside][1], "building" if stored[inside][0] == "2 Building Road" else "listing")
            self.assertEqual(counts["places"], len(places))
            # Nothing else in the database changes.
            with closing(sqlite3.connect(DB)) as original:
                self.assertEqual(businesses, original.execute("SELECT COUNT(*) FROM businesses").fetchone()[0])


class AttachPlacesTests(unittest.TestCase):
    LOOKUP = {"node/1": {"osm_ref": "node/1", "name": "Ikko Sushi", "latitude": 38.99, "longitude": -77.02,
                         "address": "8211 Georgia Avenue, Silver Spring", "address_basis": "listing"},
              "way/2": {"osm_ref": "way/2", "name": "Spring Garden", "latitude": 38.98, "longitude": -77.03,
                        "address": None, "address_basis": None}}

    def run_attach(self, rows, sql):
        with patch.object(main, "_place_lookup", side_effect=lambda urls: {r: self.LOOKUP[r] for r in map(main._osm_ref, urls) if r in self.LOOKUP}):
            return main._attach_places(rows, sql)

    def test_listed_places_get_address_and_location_and_placeholders_go(self):
        # The tester's query shape: cuisine from the partner table, a NULL address placeholder.
        rows = [{"name": "Ikko Sushi", "cuisine": "sushi", "address_placeholder": None, "source_url": "https://www.openstreetmap.org/node/1"},
                {"name": "Spring Garden", "cuisine": "asian", "address_placeholder": None, "source_url": "https://www.openstreetmap.org/way/2"}]
        sql = "SELECT name, cuisine, NULL AS address_placeholder, source_url FROM community_businesses WHERE cuisine LIKE '%asian%'"
        enriched, locations = self.run_attach(rows, sql)
        self.assertEqual(enriched[0]["address"], "8211 Georgia Avenue, Silver Spring")
        self.assertNotIn("address_placeholder", enriched[0])
        self.assertIsNone(enriched[1]["address"])  # no mapped address: not invented
        self.assertEqual(locations[0]["latitude"], 38.99)
        self.assertEqual(locations[1]["name"], "Spring Garden")  # still mappable

    def test_grouped_rows_never_get_one_places_address(self):
        rows = [{"category": "restaurant", "listing_count": 43, "source_url": "https://www.openstreetmap.org/node/1"}]
        for sql in ["SELECT category, COUNT(*) AS listing_count, MIN(source_url) AS source_url FROM businesses GROUP BY category",
                    "SELECT COUNT(*) AS listing_count, MIN(source_url) AS source_url FROM businesses",
                    "SELECT GROUP_CONCAT(name) AS names, MIN(b.source_url) AS source_url FROM businesses b"]:
            enriched, locations = self.run_attach(rows, sql)
            self.assertEqual(enriched, rows, sql)
            self.assertIsNone(locations, sql)

    def test_rows_with_their_own_coordinates_are_mappable(self):
        rows = [{"name": "Silver Spring", "latitude": 38.9939, "longitude": -77.0310, "source_url": "https://www.openstreetmap.org/copyright"}]
        enriched, locations = self.run_attach(rows, "SELECT name, latitude, longitude, source_url FROM community_stations")
        self.assertEqual(locations[0]["latitude"], 38.9939)

    def test_supporting_data_carries_aligned_locations(self):
        rows = [{"name": "Ikko Sushi", "source_url": "https://www.openstreetmap.org/node/1"}]
        enriched, locations = self.run_attach(rows, "SELECT name, source_url FROM businesses")
        data = main._supporting_data(enriched, locations)
        self.assertEqual(len(data["locations"]), len(data["rows"]))
        self.assertIn("Address", data["columns"])
        self.assertNotIn("Address basis", data["columns"])  # provenance stays out of the table

    def test_real_database_lookup_when_built(self):
        with closing(sqlite3.connect(DB)) as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='place_locations'").fetchone():
                self.skipTest("place_locations not built in this database")
            ref, lat = con.execute("SELECT osm_ref, latitude FROM place_locations LIMIT 1").fetchone()
        self.assertEqual(main._place_lookup(["https://www.openstreetmap.org/" + ref])[ref]["latitude"], lat)


class ModelCallTests(unittest.TestCase):
    def fake_client(self, stop_reason, blocks):
        response = SimpleNamespace(stop_reason=stop_reason, content=blocks)
        calls = []
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: calls.append(kw) or response))
        return client, calls

    def test_cut_off_response_is_never_used(self):
        client, _ = self.fake_client("max_tokens", [SimpleNamespace(type="text", text="SELECT name FROM busi")])
        with patch.object(main, "client", client):
            with self.assertRaises(main.ModelTruncated):
                main._ask_claude("prompt", max_tokens=10)

    def test_schema_prefix_is_cached_and_effort_is_set(self):
        client, calls = self.fake_client("end_turn", [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="SELECT 1")])
        with patch.object(main, "client", client):
            self.assertEqual(main._ask_claude("SCHEMA\n\nUser question: q", max_tokens=8000, effort="medium", cache_prefix="SCHEMA\n\n"), "SELECT 1")
        content = calls[0]["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "SCHEMA\n\n", "cache_control": {"type": "ephemeral"}})
        self.assertEqual(content[1]["text"], "User question: q")
        self.assertEqual(calls[0]["output_config"], {"effort": "medium"})

    def test_truncated_sql_is_reported_plainly_not_as_unreachable(self):
        with patch.object(main, "_ask_claude", side_effect=main.ModelTruncated("cut")), patch.object(main, "_log_gap") as log:
            result = main.ask(main.Question(question="show me asian restaurants and their addresses"))
        self.assertNotIn("RuntimeError", result["answer"])
        self.assertIn("one thing at a time", result["answer"])
        self.assertEqual(log.call_args[0][1], "model_truncated")


if __name__ == "__main__":
    unittest.main()
