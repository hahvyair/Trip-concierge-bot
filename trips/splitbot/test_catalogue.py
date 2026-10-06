"""Tests for the trip catalogue PDF and bulk itinerary import."""

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalogue  # noqa: E402
import plan  # noqa: E402
import splitlog  # noqa: E402

GROUP = "-88"
HAS_BROWSER = bool(shutil.which("node")) and Path("/opt/pw-browsers").exists()


def run(mod, *args, stdin=""):
    out = io.StringIO()
    with redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(stdin)):
        code = mod.main(list(args))
    return code, out.getvalue()


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"SPLIT_DATA": str(d / "data"), "SPLITBOT_HOME": str(d / "home")})
        self.env.start()
        run(splitlog, "trip", "new", "--planning", "--chat", GROUP, "--name", "Japan", "--currency", "JPY",
            "--start", "2030-12-12", "--end", "2030-12-14", "--members", "Sam:1,Mei")
        run(plan, "set", "--chat", GROUP, "--destination", "Kyoto", "--budget", "2500")
        run(plan, "idea", "add", "--chat", GROUP, "--text", "Hoshinoya <b>Kyoto</b>", "--kind", "stay",
            "--why", "riverside ryokan", "--price", "high", "--status", "chosen")
        run(plan, "idea", "add", "--chat", GROUP, "--text", "Dropped place", "--kind", "food", "--status", "dropped")
        self.trip = splitlog.open_trip(GROUP)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_import_sorts_and_replaces(self):
        lines = "\n".join([
            '{"day": "2030-12-13", "time": "15:00", "what": "Check in"}',
            '{"day": "2030-12-13", "time": "07:30", "what": "Fushimi Inari", "where": "Fushimi", "cost_sgd": 0}',
            '{"day": "2030-12-12", "what": "Land at KIX", "note": "ICOCA at the airport"}'])
        code, out = run(plan, "itinerary", "import", "--chat", GROUP, stdin=lines)
        self.assertEqual(code, 0, out)
        it = plan.load_plan(self.trip)["itinerary"]
        self.assertEqual([x["what"] for x in it["2030-12-13"]], ["Fushimi Inari", "Check in"])
        self.assertEqual(it["2030-12-12"][0]["note"], "ICOCA at the airport")
        code, _ = run(plan, "itinerary", "import", "--chat", GROUP, "--replace",
                      stdin='{"day": "2030-12-13", "what": "Rest day"}')
        self.assertEqual([x["what"] for x in plan.load_plan(self.trip)["itinerary"]["2030-12-13"]], ["Rest day"])

    def test_import_is_all_or_nothing(self):
        code, out = run(plan, "itinerary", "import", "--chat", GROUP,
                        stdin='{"day": "2030-12-12", "what": "ok"}\n{"what": "no day"}')
        self.assertEqual(code, 1)
        self.assertIn("needs a day", out)
        self.assertEqual(plan.load_plan(self.trip)["itinerary"], {})

    def test_html_sections_escaping_and_dropped_ideas(self):
        run(plan, "itinerary", "import", "--chat", GROUP,
            stdin='{"day": "2030-12-12", "time": "19:00", "what": "Dinner", "where": "Nishiki Market"}')
        h = catalogue.build_html(self.trip)
        for part in ("Trip catalogue", "At a glance", "Day by day", "The shortlist", "Where to stay",
                     "Thursday 12 December"):
            self.assertIn(part, h)
        self.assertIn("Hoshinoya &lt;b&gt;Kyoto&lt;/b&gt;", h)
        self.assertNotIn("Dropped place", h)
        self.assertIn("google.com/maps/search", h)

    def test_day_labels_sort_in_order(self):
        self.assertEqual(catalogue.sort_days(["Day 10", "2030-12-13", "Day 2", "2030-12-12"]),
                         ["2030-12-12", "2030-12-13", "Day 2", "Day 10"])

    @unittest.skipUnless(HAS_BROWSER, "needs node + Chromium")
    def test_renders_pdf(self):
        pdf = catalogue.render(self.trip)
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertTrue(pdf.name.endswith("_Japan Trip Catalogue.pdf"))


if __name__ == "__main__":
    unittest.main()
