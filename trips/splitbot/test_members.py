"""Renaming a member mid-trip keeps every balance and plan reference intact."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan  # noqa: E402
import splitlog  # noqa: E402

GROUP = "-9"


def run(mod, *args):
    out = io.StringIO()
    with redirect_stdout(out):
        code = mod.main(list(args))
    return code, out.getvalue()


class Rename(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SPLIT_DATA": self.tmp.name, "SPLITBOT_HOME": self.tmp.name + "/h"})
        self.env.start()
        run(splitlog, "trip", "new", "--chat", GROUP, "--name", "T", "--currency", "SGD", "--members", "Sam,Kai,Leo")
        run(splitlog, "add", "--chat", GROUP, "--payer", "Leo", "--amount", "30", "--desc", "x", "--category", "food")
        run(splitlog, "add", "--chat", GROUP, "--payer", "Kai", "--amount", "20", "--desc", "y", "--category", "food",
            "--exact", "Kai=5,Leo=15")
        run(plan, "todo", "add", "--chat", GROUP, "--task", "visa", "--owner", "Leo")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def nets(self):
        return {k: v["net"] for k, v in splitlog.nets(splitlog.open_trip(GROUP)).items()}

    def test_rename_keeps_balances_and_updates_the_plan(self):
        before = self.nets()
        code, out = run(splitlog, "member", "rename", "--chat", GROUP, "--name", "Leo", "--to", "Jules")
        self.assertEqual(code, 0, out)
        after = self.nets()
        self.assertNotIn("Leo", after)
        self.assertEqual(after["Jules"], before["Leo"])
        self.assertEqual(sum(after.values()), 0)
        rows = [r for r in splitlog.load_rows()]
        self.assertFalse(any("Leo" in r["payer"] + r["split"] + r["shares_sgd"] for r in rows))
        trip = splitlog.open_trip(GROUP)
        p = json.loads((Path(self.tmp.name) / "plans" / f"{trip['id']}.json").read_text())
        self.assertEqual(p["todos"][0]["owner"], "Jules")

    def test_rename_refuses_clashes_and_bad_names(self):
        for new in ("kai", "bad=name", "a;b", ""):
            code, out = run(splitlog, "member", "rename", "--chat", GROUP, "--name", "Leo", "--to", new)
            self.assertEqual(code, 1, (new, out))
        self.assertIn("Leo", self.nets())


if __name__ == "__main__":
    unittest.main()
