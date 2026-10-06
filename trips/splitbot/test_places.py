"""Tests for on-trip requests: shared locations, request wake words, places.py."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_splitbot import BOT, GROUP, OWNER, _load, upd  # noqa: E402

listen = _load("splitbot_listen_places", "listen.py")
places = _load("splitbot_places", "places.py")


class Listener(unittest.TestCase):
    def test_locations_and_on_trip_requests_get_through(self):
        resp = {"ok": True, "result": [
            upd(1, GROUP, 222, location={"latitude": 10.77, "longitude": 106.70}),
            upd(2, GROUP, 222, text="any good dessert place near me?"),
            upd(3, GROUP, 333, text="where should we eat tonight"),
            upd(4, GROUP, 333, text="ok see you soon"),                          # chatter: dropped
            upd(5, "-2002", 555, location={"latitude": 1.0, "longitude": 2.0}),  # stranger: dropped
        ]}
        _, evs = listen.parse_updates(resp, OWNER, {GROUP}, BOT)
        self.assertEqual([e["update_id"] for e in evs], [1, 2, 3])
        self.assertEqual(evs[0]["location"], {"lat": 10.77, "lon": 106.70, "live": False, "venue": None})
        self.assertIsNone(evs[1]["location"])


class Places(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SPLITBOT_HOME": self.tmp.name})
        self.env.start()
        self.geo = mock.patch.object(places, "area_name", lambda lat, lon: "District 1")
        self.geo.start()

    def tearDown(self):
        self.geo.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_distance_and_name_matching(self):
        self.assertAlmostEqual(places.km(10.7769, 106.7009, 10.7747, 106.7010), 0.24, places=2)
        self.assertTrue(places.same_place("Kem Bach Dang", "Quán Kem Bạch Đằng"))
        self.assertFalse(places.same_place("STIR cocktail bar", "House Of Merlin - Cocktail Bar"))

    def test_locate_only_trusts_close_confident_matches(self):
        far = {"features": [{"properties": {"name": "Moss Bar"}, "geometry": {"coordinates": [-1.98, 53.18]}}]}
        near = {"features": [{"properties": {"name": "Quán Kem Bạch Đằng"},
                              "geometry": {"coordinates": [106.7010, 10.7747]}}]}
        with mock.patch.object(places, "get_json", lambda url: far):
            r = places.locate(10.7769, 106.7009, "Moss Bar", "Ho Chi Minh City")
        self.assertIsNone(r["km"])
        self.assertIn("google.com/maps/search", r["maps"])
        with mock.patch.object(places, "get_json", lambda url: near):
            r = places.locate(10.7769, 106.7009, "Kem Bach Dang")
        self.assertEqual(r["km"], 0.2)

    def test_last_location_expires_and_falls_back_to_the_group(self):
        places.remember("-1", "5", 10.0, 106.0, now=1000)
        self.assertEqual(places.last("-1", "5", now=1060)["whose"], "yours")
        self.assertEqual(places.last("-1", "9", now=1060)["whose"], "someone else's")
        self.assertIsNone(places.last("-1", "5", now=1000 + 3601))
        self.assertIsNone(places.last("-2", "5", now=1060))
        repo = Path(__file__).resolve().parents[2]
        self.assertFalse(str(places.store()).startswith(str(repo)))  # never in the repo


class Suggest(unittest.TestCase):
    """places.py suggest: travel times, nearest first, logged for the app without people's positions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"SPLITBOT_HOME": str(d / "home"), "SPLIT_DATA": str(d / "data")})
        self.env.start()
        splitlog = sys.modules["splitlog"] if "splitlog" in sys.modules else __import__("splitlog")
        self.sl = splitlog
        with mock.patch("sys.stdout"):
            splitlog.main(["trip", "new", "--chat", GROUP, "--name", "Hanoi", "--currency", "VND", "--members", "A,B"])
        venues = {"Kem Bach Dang": (10.7747, 106.7010), "Far Cafe": (10.80, 106.72)}

        def fake_locate(lat, lon, name, area=""):
            if name not in venues:
                return {"name": name, "maps": places.maps_link(name, area), "km": None}
            vlat, vlon = venues[name]
            return {"name": name, "km": round(places.km(lat, lon, vlat, vlon), 1), "lat": vlat, "lon": vlon,
                    "maps": f"https://www.google.com/maps/search/?api=1&query={vlat},{vlon}"}

        self.patches = [mock.patch.object(places, "area_name", lambda lat, lon: "District 1"),
                        mock.patch.object(places, "locate", fake_locate),
                        mock.patch.object(places, "route", lambda lat, lon, a, b: {
                            "walk_min": round(places.km(lat, lon, a, b) * 13), "walk_km": round(places.km(lat, lon, a, b), 1),
                            "ride_min": 5})]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_nearest_first_with_times_and_logged_for_the_app(self):
        items = [{"name": "Far Cafe", "why": "views"}, {"name": "Mystery Gelato"},
                 {"name": "Kem Bach Dang", "why": "coconut ice cream", "price": "budget"}]
        out = places.suggest(GROUP, "222", "A", "dessert near me", items, lat=10.7769, lon=106.7009)
        lines = [l for l in out.splitlines() if not l.startswith("   ")]
        self.assertTrue(lines[0].startswith("1. Kem Bach Dang · 0.2 km · 🚶 3 min · 🛵 ~5 min"))
        self.assertTrue(lines[1].startswith("2. Far Cafe"))
        self.assertEqual(lines[2], "3. Mystery Gelato")  # not found: no distance, still a Maps link
        trip = self.sl.open_trip(GROUP)
        log = __import__("json").loads((Path(os.environ["SPLIT_DATA"]) / "places" / f"{trip['id']}.json").read_text())
        self.assertEqual(log[-1]["request"], "dessert near me")
        self.assertEqual(log[-1]["near"], "District 1")
        self.assertNotIn("10.7769", __import__("json").dumps(log))  # the asker's position is never stored


if __name__ == "__main__":
    unittest.main()
