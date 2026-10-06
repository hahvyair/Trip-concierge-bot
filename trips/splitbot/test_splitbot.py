"""Tests for the bill-split bot. Run: python3 -m unittest trips/splitbot/test_splitbot.py

The listener and sender tests run against a fake Telegram API on localhost, so
they need no token and send nothing. Ledger tests pass --fx, so no network.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import importlib.util

HERE = Path(__file__).resolve().parent


def _load(name, file):
    # Unique module names, so a second bot with its own listen.py never clashes.
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


listen = _load("splitbot_listen", "listen.py")
splitlog = _load("splitbot_splitlog", "splitlog.py")

OWNER, GROUP, BOT = "111", "-1001", {"id": 42, "username": "tripsplit_bot"}
FRIENDS = "-3003"  # a group the owner is in, with no trip yet


def upd(uid, chat, sender, **msg):
    return {"update_id": uid, "message": {"message_id": uid * 10, "date": 1791000000 + uid,
                                          "chat": {"id": int(chat), "type": "group", "title": "Japan"},
                                          "from": {"id": int(sender), "first_name": f"U{sender}"},
                                          **msg}}


class ParseUpdates(unittest.TestCase):
    def test_trip_group_accepts_members_and_drops_chatter(self):
        resp = {"ok": True, "result": [
            upd(1, GROUP, 222, text="paid 8400 for the taxi"),
            upd(2, GROUP, 333, text="lol that ramen was good"),       # chatter: dropped
            upd(3, GROUP, 333, photo=[{"file_id": "s"}, {"file_id": "b"}]),
            upd(4, GROUP, 444, text="who owes what?"),                 # keyword
            upd(5, GROUP, 444, text="hey @tripsplit_bot"),             # mention
            upd(6, GROUP, 222, text="no it was ben",
                reply_to_message={"message_id": 7, "from": {"id": 42}, "text": "#3 taxi"}),
            upd(7, "-2002", 555, text="paid 100"),                    # other group: dropped
            upd(8, "-2002", int(OWNER), text="/newtrip Bali IDR"),    # owner anywhere: kept
            upd(9, GROUP, 666, new_chat_members=[{"id": 666, "first_name": "Eve"}]),
        ]}
        nxt, evs = listen.parse_updates(resp, OWNER, {GROUP}, BOT)
        self.assertEqual(nxt, 10)
        self.assertEqual([e["update_id"] for e in evs], [1, 3, 4, 5, 6, 8, 9])
        self.assertEqual(evs[1]["file_id"], "b")
        self.assertTrue(evs[4]["reply_to"]["from_bot"])
        self.assertTrue(evs[5]["from"]["is_owner"])
        self.assertFalse(evs[0]["from"]["is_owner"])
        self.assertEqual(evs[6]["joined"][0]["name"], "Eve")
        self.assertNotIn("ramen", json.dumps(evs))

    def test_report_requests_get_through_without_numbers(self):
        asks = ["send the excel pls", "can I get a visualisation", "show me a chart",
                "how much did we spend today", "breakdown?", "spreadsheet"]
        resp = {"ok": True, "result": [upd(i + 1, GROUP, 222, text=t) for i, t in enumerate(asks)]
                + [upd(50, GROUP, 222, text="see you at the lobby")]}
        _, evs = listen.parse_updates(resp, OWNER, {GROUP}, BOT)
        self.assertEqual([e["text"] for e in evs], asks)

    def test_owner_private_chat_is_accepted(self):
        resp = {"ok": True, "result": [upd(1, OWNER, int(OWNER), text="send me the excel"),
                                       upd(2, "222", 222, text="send me the excel")]}
        _, evs = listen.parse_updates(resp, OWNER, {GROUP}, BOT)
        self.assertEqual([e["chat_id"] for e in evs], [OWNER])

    def test_voice_notes_become_text_and_audio_is_deleted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        heard = {"v1": "Kai paid 450k for dinner", "v2": "haha see you at the lobby", "v4": None}
        fetched = []

        def fake_curl_json(url, _t):
            fid = url.split("file_id=")[1]
            return {"result": {"file_path": f"voice/{fid}.oga"}}

        def fake_run(cmd, **_kw):
            dest = Path(cmd[cmd.index("-o") + 1])
            dest.write_bytes(b"OggS")
            fetched.append(dest)

        def fake_transcribe(path, hint=""):
            self.assertIn("Grab", hint)                    # names and money words bias the model
            update_id = int(Path(path).stem.split("-")[1])  # downloads are named voice-<update id>
            return heard[f"v{update_id}"]

        resp = {"ok": True, "result": [
            upd(1, GROUP, 222, voice={"file_id": "v1", "duration": 4}),
            upd(2, GROUP, 333, voice={"file_id": "v2", "duration": 3}),          # chatter: still reaches the bot
            upd(3, "-2002", 555, voice={"file_id": "v3", "duration": 3}),        # stranger: never fetched
            upd(4, GROUP, 444, voice={"file_id": "v4", "duration": 5}),          # can't transcribe
            upd(5, GROUP, 444, voice={"file_id": "v5", "duration": 900}),        # too long: not fetched
        ]}
        with mock.patch.dict(os.environ, {"SPLITBOT_HOME": tmp.name}), \
                mock.patch.object(listen, "curl_json", fake_curl_json), \
                mock.patch.object(listen.subprocess, "run", fake_run), \
                mock.patch.object(listen, "open_trips", lambda: []), \
                mock.patch.object(listen.transcribe, "transcribe", fake_transcribe):
            listen.transcribe_voices("api", resp, OWNER, {GROUP})
        self.assertEqual(len(fetched), 3)                         # v1, v2, v4; not the stranger's or the long one
        self.assertTrue(all(not f.exists() for f in fetched))     # audio deleted
        _, evs = listen.parse_updates(resp, OWNER, {GROUP}, BOT)
        self.assertEqual([e["update_id"] for e in evs], [1, 2, 4, 5])  # every voice note gets through
        self.assertEqual(evs[0]["text"], "Kai paid 450k for dinner")
        self.assertEqual(evs[0]["voice"], {"duration": 4, "failed": False})
        self.assertTrue(evs[2]["voice"]["failed"])
        self.assertTrue(evs[3]["voice"]["too_long"])

    def test_empty_and_error(self):
        self.assertEqual(listen.parse_updates({"ok": True, "result": []}, OWNER, {GROUP}, BOT), (None, []))
        self.assertEqual(listen.parse_updates({"ok": False}, OWNER, {GROUP}, BOT), (None, []))


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"SPLIT_DATA": str(d / "data"),
                                                "SPLITBOT_HOME": str(d / "home")})
        self.env.start()
        self.run_ok("trip", "new", "--chat", GROUP, "--name", "Japan", "--currency", "JPY",
                    "--members", "Ana:111:ana,Ben,Chloe")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cmd(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = splitlog.main(list(args))
        return code, buf.getvalue()

    def run_ok(self, *args):
        code, out = self.run_cmd(*args)
        self.assertEqual(code, 0, out)
        return out

    def add(self, *extra, amount="9000", payer="Ana", fx="0.01"):
        return self.run_ok("add", "--chat", GROUP, "--payer", payer, "--amount", amount,
                           "--desc", "x", "--fx", fx, *extra)

    def nets(self):
        return {k: v["net"] for k, v in splitlog.nets(splitlog.open_trip(GROUP)).items()}

    def test_allocate_always_sums_exactly(self):
        for total in (1, 100, 9999, 123457):
            for w in ({"a": 1, "b": 1, "c": 1}, {"a": 2, "b": 1}, {"a": 0.3, "b": 0.7, "c": 5}):
                self.assertEqual(sum(splitlog.allocate(total, w).values()), total)

    def test_equal_default_is_everyone_and_nets_sum_to_zero(self):
        self.add()                                       # S$90 by Ana, 3 ways
        self.add("--equal", "Ben,Chloe", payer="Ben", amount="1000")
        n = self.nets()
        self.assertEqual(sum(n.values()), 0)
        self.assertEqual(n["Ana"], 9000 - 3000)
        self.assertEqual(n["Chloe"], -3000 - 500)

    def test_exact_must_add_up_unless_prorated(self):
        code, out = self.run_cmd("add", "--chat", GROUP, "--payer", "Ben", "--amount", "1100",
                                 "--desc", "dinner", "--fx", "0.01", "--exact", "Ben=500,Ana=500")
        self.assertEqual(code, 1)
        self.assertIn("add up", out)
        self.add("--exact", "Ben=500,Ana=500", "--prorate", payer="Ben", amount="1100")
        self.assertEqual(self.nets()["Ana"], -550)

    def test_settle_plan_clears_everyone(self):
        self.add()
        self.add("--shares", "Ben=2,Chloe=1", payer="Chloe", amount="3000")
        plan = splitlog.settle_plan(self.nets())
        after = dict(self.nets())
        for d, c, x in plan:
            after[d] += x
            after[c] -= x
        self.assertTrue(all(v == 0 for v in after.values()))
        for d, c, x in plan:
            self.run_ok("transfer", "--chat", GROUP, "--payer", d, "--to", c,
                        "--amount", f"{x / 100:.2f}", "--currency", "SGD")
        self.assertTrue(all(v == 0 for v in self.nets().values()))

    def test_dedup_unknown_name_undo_and_edit(self):
        self.add("--update-id", "7")
        self.assertIn("Already logged", self.add("--update-id", "7"))
        code, out = self.run_cmd("add", "--chat", GROUP, "--payer", "Zed", "--amount", "1",
                                 "--desc", "x", "--fx", "1")
        self.assertEqual(code, 1)
        self.assertIn("Members: Ana, Ben, Chloe", out)
        self.add(payer="Ben", amount="300")
        self.assertIn("#2", self.run_ok("undo", "--chat", GROUP))
        self.assertIn("Corrected #1", self.run_ok("edit", "--chat", GROUP, "--entry", "1",
                                                  "--amount", "12000"))
        live = splitlog.trip_rows(splitlog.open_trip(GROUP))
        self.assertEqual([(r["entry"], r["amount"]) for r in live], [("1", "12000")])
        self.assertEqual(self.nets()["Ana"], 8000)
        statuses = sorted(r["status"] for r in splitlog.load_rows())
        self.assertEqual(statuses, ["active", "deleted", "edited"])  # nothing is erased

    def test_username_and_event_date(self):
        box = Path(os.environ["SPLITBOT_HOME"]) / "inbox"
        box.mkdir(parents=True)
        (box / "55.json").write_text(json.dumps({"date": 1790956800}))  # 2026-10-03 00:00 SGT
        self.add("--update-id", "55", payer="@ana")
        r = splitlog.load_rows()[-1]
        self.assertEqual((r["payer"], r["date"]), ("Ana", "2026-10-03"))

    def test_one_open_trip_per_group_and_close(self):
        code, out = self.run_cmd("trip", "new", "--chat", GROUP, "--name", "Again")
        self.assertEqual(code, 1)
        self.add()
        self.assertIn("to settle up", self.run_ok("trip", "close", "--chat", GROUP))
        self.run_ok("trip", "new", "--chat", GROUP, "--name", "Next", "--members", "A,B")
        self.assertEqual(splitlog.open_trip(GROUP)["name"], "Next")


report = _load("splitbot_report", "report.py")
HAS_BROWSER = bool(shutil.which("node")) and Path("/opt/pw-browsers").exists()


class Reports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"SPLIT_DATA": str(d / "data"),
                                                "SPLITBOT_HOME": str(d / "home")})
        self.env.start()
        S = splitlog.main
        with redirect_stdout(io.StringIO()):
            S(["trip", "new", "--chat", GROUP, "--name", "Hanoi", "--currency", "VND",
               "--members", "Sam:111,Kai,Leo", "--start", "2026-10-01"])
            for payer, amt, cat, day, extra in [
                    ("Sam", "900000", "food", "2026-10-01", []),
                    ("Kai", "3000000", "lodging", "2026-10-01", []),
                    ("Leo", "60", "shopping", "2026-10-03", ["--currency", "SGD", "--equal", "Leo,Kai"]),
                    ("Sam", "300000", "transport", "2026-10-03", [])]:
                S(["add", "--chat", GROUP, "--payer", payer, "--amount", amt, "--desc", cat,
                   "--category", cat, "--date", day, "--fx", "0.00005", *extra])
            S(["transfer", "--chat", GROUP, "--payer", "Kai", "--to", "Sam", "--amount", "5",
               "--currency", "SGD", "--date", "2026-10-03"])
        self.trip = splitlog.open_trip(GROUP)
        # Charts and day counts run up to "today"; pin it so the tests don't age.
        self.today = mock.patch.object(report.sl, "trip_today", lambda trip: "2026-10-03")
        self.today.start()

    def tearDown(self):
        self.today.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_daily_text_figures(self):
        text, new = report.daily_text(self.trip, "2026-10-03")
        self.assertIn("Today: S$75.00 across 2 payments", text)   # 60 SGD + 300k VND (S$15)
        self.assertIn("Shopping S$60.00 · Transport S$15.00", text)
        self.assertIn("Trip so far: S$270.00 over 3 days", text)  # settle-up not counted
        self.assertEqual(new, 2 + 1)  # no report yet: everything dated today, incl. the settle-up

    def test_excel_ties_to_ledger(self):
        from openpyxl import load_workbook
        wb = load_workbook(report.build_excel(self.trip))
        tx = wb["Transactions"]
        rows = [r for r in tx.iter_rows(min_row=2, values_only=True) if isinstance(r[0], int)]
        self.assertEqual(len(rows), 5)
        for r in rows:  # each row's member shares add up to its SGD amount
            self.assertAlmostEqual(sum(r[10:13]), r[8], places=2)
        daily = {r[0].date().isoformat(): r for r in wb["Daily"].iter_rows(min_row=2, values_only=True)
                 if hasattr(r[0], "date")}
        self.assertEqual(sorted(daily), ["2026-10-01", "2026-10-02", "2026-10-03"])
        head = [c.value for c in wb["Daily"][1]]
        cats = slice(1, head.index("Total"))
        self.assertAlmostEqual(sum(daily["2026-10-01"][cats]), 195.0)   # 900k + 3m VND at 0.00005
        shares = slice(head.index("Total") + 1, len(head))
        self.assertAlmostEqual(sum(daily["2026-10-03"][shares]), 75.0)  # settle-up excluded
        bal = {r[0]: r[3] for r in wb["Balances"].iter_rows(min_row=2, max_row=4, values_only=True)}
        self.assertAlmostEqual(sum(bal.values()), 0, places=2)
        self.assertEqual(len(wb["Daily"]._charts) + len(wb["Balances"]._charts), 2)

    def test_drinks_is_its_own_category(self):
        with redirect_stdout(io.StringIO()):
            code = splitlog.main(["add", "--chat", GROUP, "--payer", "Sam", "--amount", "800000", "--desc",
                                  "cocktails", "--category", "drinks", "--date", "2026-10-03", "--fx", "0.00005"])
        self.assertEqual(code, 0)
        h = report.chart_html(splitlog.open_trip(GROUP), "2026-10-03")
        self.assertIn("Drinks", h)
        self.assertLess(h.index(">Food<"), h.index(">Drinks<"))  # legend: drinks right after food

    def test_chart_html_labels_categories_and_balances(self):
        h = report.chart_html(self.trip, "2026-10-03")
        for word in ("Food", "Lodging", "Shopping", "Transport", "is owed", "owes"):
            self.assertIn(word, h)
        self.assertNotIn("Activities", h)  # only categories that occur

    def test_balance_labels_stay_clear_of_names_and_edge(self):
        import re
        # A big debtor: Sam pays nothing, Ana... here Sam owes most of a large bill.
        with redirect_stdout(io.StringIO()):
            splitlog.main(["add", "--chat", GROUP, "--payer", "Kai", "--amount", "90000000",
                           "--desc", "villa", "--category", "lodging", "--date", "2026-10-03", "--fx", "0.00005"])
        svg = report.svg_balances(splitlog.open_trip(GROUP))
        W, L = 820, 120
        for x, anchor, text in re.findall(r"<text x='([\d.]+)' y='[\d.]+' text-anchor='(\w+)'[^>]*>([^<]+)</text>", svg):
            if not text.startswith(("owes", "is owed")):
                continue
            x, width = float(x), len(text) * 7.2
            left, right = (x - width, x) if anchor == "end" else (x, x + width)
            self.assertGreaterEqual(left, L, text)   # never over the names column
            self.assertLessEqual(right, W, text)     # never off the right edge

    @unittest.skipUnless(HAS_BROWSER, "needs node + Chromium")
    def test_chart_renders_png(self):
        png = report.render_chart(self.trip, "2026-10-03")
        self.assertTrue(png.read_bytes().startswith(b"\x89PNG"))

    def test_wrapped_stats(self):
        import wrapped
        s = wrapped.stats(self.trip)
        self.assertEqual(s["total"], 27000)                       # settle-up not counted
        self.assertEqual(s["days"], ["2026-10-01", "2026-10-02", "2026-10-03"])
        self.assertEqual(s["peak"], "2026-10-01")                 # S$195 vs S$75
        self.assertEqual(s["cats"][0], ("lodging", 15000))
        self.assertEqual([wrapped.rp.cents(r) for r in s["top"]], [15000, 6000, 4500, 1500])
        names = [a["name"] for a in s["awards"]]
        self.assertEqual(names, ["Sam", "Kai", "Leo"])         # one award each
        self.assertEqual(len({a["title"] for a in s["awards"]}), 3)
        self.assertEqual(sum(x for *_, x in s["plan"]),
                         sum(v["net"] for v in s["nets"].values() if v["net"] > 0))

    def test_wrapped_escapes_and_names_the_file(self):
        import wrapped
        with redirect_stdout(io.StringIO()):
            splitlog.main(["add", "--chat", GROUP, "--payer", "Sam", "--amount", "10", "--currency", "SGD",
                           "--desc", "<b>pho</b>", "--category", "food", "--date", "2026-10-02"])
        h = wrapped.build_html(splitlog.open_trip(GROUP))
        self.assertIn("&lt;b&gt;pho&lt;/b&gt;", h)
        self.assertNotIn("<b>pho</b>", h)
        self.assertEqual(wrapped.pdf_name(self.trip), "2026.10.03_Hanoi Trip Wrapped.pdf")

    def test_wrapped_nothing_logged(self):
        import wrapped
        with redirect_stdout(io.StringIO()):
            splitlog.main(["trip", "new", "--chat", "-42", "--name", "Empty", "--members", "A,B"])
        with self.assertRaises(wrapped.sl.SplitError):
            wrapped.stats(splitlog.open_trip("-42"))

    def test_wrapped_finds_a_closed_trip(self):
        with redirect_stdout(io.StringIO()):
            splitlog.main(["trip", "close", "--chat", GROUP])
        trip = report.latest_trip(GROUP, splitlog.load_trips())
        self.assertEqual((trip["name"], trip["status"]), ("Hanoi", "closed"))
        # A closed trip's chart stops at the day it closed, not today.
        with mock.patch.object(report.sl, "trip_today", lambda t: "2026-12-31"):
            trip["closed"] = "2026-10-03"
            self.assertEqual(report.trip_days(trip, report.expenses(trip))[-1], "2026-10-03")

    @unittest.skipUnless(HAS_BROWSER, "needs node + Chromium")
    def test_endtrip_closes_then_posts_settle_chart_excel_and_wrapped(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = report.main(["endtrip", "--chat", GROUP])
        self.assertEqual(code, 0, out.getvalue())
        kinds = [ln.split(":")[0] for ln in out.getvalue().splitlines()
                 if ln.split(":")[0] in ("text", "photo", "document")]
        self.assertEqual(kinds, ["text", "photo", "document", "document"])
        self.assertIn("Trip ended.", out.getvalue())
        self.assertIn("Trip Wrapped.pdf", out.getvalue())
        self.assertEqual(splitlog.load_trips()[0]["status"], "closed")
        with redirect_stdout(io.StringIO()):  # a second /endtrip finds no open trip
            self.assertEqual(report.main(["endtrip", "--chat", GROUP]), 1)

    @unittest.skipUnless(HAS_BROWSER, "needs node + Chromium")
    def test_wrapped_renders_pdf(self):
        import wrapped
        pdf = wrapped.render(self.trip)
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))


class FakeTelegram(BaseHTTPRequestHandler):
    updates, actions, sent, polled = [], [], [], []

    def log_message(self, *a):
        pass

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path
        if "/getUpdates" in path:
            self.polled.append(path)
            offset = int(path.split("offset=")[1].split("&")[0])
            self.reply({"ok": True, "result": [u for u in self.updates if u["update_id"] >= offset]})
        elif "/getMe" in path:
            self.reply({"ok": True, "result": {**BOT, "can_read_all_group_messages": False}})
        elif "/getChatMember" in path:
            self.reply(self.member(path))
        elif "/getFile" in path:
            self.reply({"ok": True, "result": {"file_path": "photos/file_1.jpg"}})
        elif "/file/bot" in path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"\xff\xd8JPEGDATA")
        elif "/sendChatAction" in path:
            self.actions.append(path)
            self.reply({"ok": True})
        else:
            self.reply({"ok": False})

    @staticmethod
    def member(text):
        status = "member" if f"chat_id={FRIENDS}" in text.replace("%2D", "-") else "left"
        return {"ok": True, "result": {"status": status}}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode(errors="replace")
        if "/getChatMember" in self.path:
            return self.reply(self.member(body))
        if "/getMe" in self.path:
            return self.reply({"ok": True, "result": {**BOT, "can_read_all_group_messages": False}})
        self.sent.append(body)
        if "/sendPoll" in self.path:
            return self.reply({"ok": True, "result": {"message_id": 90 + len(self.sent),
                                                      "poll": {"id": f"P{len(self.sent)}"}}})
        self.reply({"ok": True, "result": {"message_id": 99}})


class EndToEnd(unittest.TestCase):
    def setUp(self):
        FakeTelegram.updates = [
            upd(1, "-2002", 999, text="paid 100 for stuff"),
            upd(2, GROUP, 222, photo=[{"file_id": "p1"}], caption="dinner, split 3"),
            upd(3, GROUP, 333, text="anyone up for karaoke"),
            upd(4, FRIENDS, 333, text="@tripsplit_bot are you on?"),
        ]
        FakeTelegram.actions, FakeTelegram.sent = [], []
        self.srv = HTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "data").mkdir()
        (d / "data" / "trips.json").write_text(json.dumps({"trips": [
            {"id": "t", "name": "Japan", "chat_id": GROUP, "currency": "JPY", "status": "open",
             "created": "2026-10-01", "closed": None, "members": []}]}))
        self.env = {**{k: v for k, v in os.environ.items() if k != "TELEGRAM_CHAT_ID"},
                    "SPLIT_BOT_TOKEN": "TEST", "SPLIT_OWNER_ID": OWNER,
                    "SPLIT_DATA": str(d / "data"), "SPLITBOT_HOME": str(d / "home"),
                    "SPLITBOT_API": f"http://127.0.0.1:{self.srv.server_port}",
                    "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def run_py(self, script, *args, stdin=None, timeout=60):
        return subprocess.run([sys.executable, str(HERE / script), *args], env=self.env,
                              input=stdin, capture_output=True, text=True, timeout=timeout)

    def test_listener_saves_photo_and_replays_inbox(self):
        r = self.run_py("listen.py", "20")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[0], "=== SPLIT BOT EVENTS ===")
        self.assertEqual(len(lines), 3)  # stranger group and chatter dropped
        self.assertEqual(json.loads(lines[2])["chat_id"], FRIENDS)  # owner's group, no trip yet
        ev = json.loads(lines[1])
        self.assertEqual((ev["update_id"], ev["chat_id"]), (2, GROUP))
        self.assertTrue(Path(ev["photo"]).read_bytes().startswith(b"\xff\xd8"))
        self.assertEqual((Path(self.tmp.name) / "home" / "offset").read_text(), "5")
        self.assertEqual(len(FakeTelegram.actions), 2)
        me = json.loads((Path(self.tmp.name) / "home" / "me.json").read_text())
        self.assertFalse(me["can_read_all_group_messages"])
        r2 = self.run_py("listen.py", "5")  # session died before handling: replayed
        self.assertEqual(json.loads(r2.stdout.splitlines()[1])["update_id"], 2)

    def test_exits_5_when_code_changes(self):
        FakeTelegram.updates = []
        code = ("import sys, os, threading, time; sys.path.insert(0, %r); import listen;"
                "listen.POLL_TIMEOUT = 1; listen.code_stamp = (lambda c=[0]: (c.__setitem__(0, c[0] + 1), c[0])[1]);"
                "sys.argv = ['listen.py', '20']; sys.exit(listen.main())") % str(HERE)
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 5, r.stderr)

    def test_only_owner_can_stop(self):
        FakeTelegram.updates = [upd(5, GROUP, 222, text="/stopbot"),
                                upd(6, GROUP, int(OWNER), text="/stopbot")]
        r = self.run_py("listen.py", "20")
        self.assertEqual(r.returncode, 4)
        self.assertIn("STOP REQUESTED", r.stdout)
        self.assertEqual(json.loads(r.stdout.splitlines()[1])["update_id"], 5)

    def test_send_only_to_trip_groups_or_owner(self):
        ok = self.run_py("send.py", "--chat", GROUP, "--reply-to", "20", stdin="logged #1")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("reply_to_message_id=20", FakeTelegram.sent[-1])
        friends = self.run_py("send.py", "--chat", FRIENDS, stdin="no trip yet")
        self.assertEqual(friends.returncode, 0, friends.stderr)
        bad = self.run_py("send.py", "--chat", "-2002", stdin="hi")
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(len(FakeTelegram.sent), 2)
        st = self.run_py("send.py", "--status")
        self.assertIn("privacy mode ON", st.stdout)

    def test_token_file_fallback(self):
        env = {k: v for k, v in self.env.items() if k != "SPLIT_BOT_TOKEN"}
        home = Path(env["SPLITBOT_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "token").write_text("TEST\n")
        r = subprocess.run([sys.executable, str(HERE / "listen.py"), "20"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SPLIT BOT EVENTS", r.stdout)

    @unittest.skipUnless(HAS_BROWSER, "needs node + Chromium")
    def test_nightly_posts_once_per_day(self):
        S = [sys.executable, str(HERE / "splitlog.py")]
        subprocess.run(S + ["add", "--chat", GROUP, "--payer", "A", "--amount", "100", "--desc", "x",
                            "--fx", "0.01"], env=self.env, check=False, capture_output=True)
        subprocess.run(S + ["member", "add", "--chat", GROUP, "--name", "A,B"], env=self.env,
                       check=True, capture_output=True)
        subprocess.run(S + ["add", "--chat", GROUP, "--payer", "A", "--amount", "100", "--desc", "x",
                            "--category", "food", "--fx", "0.01"], env=self.env, check=True, capture_output=True)
        r = self.run_py("report.py", "nightly", "--at", "00:00", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nightly report sent", r.stdout)
        self.assertEqual(len(FakeTelegram.sent), 3)  # text, chart, workbook
        r2 = self.run_py("report.py", "nightly", "--at", "00:00", timeout=120)
        self.assertIn("not due", r2.stdout)
        self.assertEqual(len(FakeTelegram.sent), 3)

    def test_excel_to_owner_private_chat(self):
        S = [sys.executable, str(HERE / "splitlog.py")]
        subprocess.run(S + ["member", "add", "--chat", GROUP, "--name", "A,B"], env=self.env,
                       check=True, capture_output=True)
        subprocess.run(S + ["add", "--chat", GROUP, "--payer", "A", "--amount", "100", "--desc", "x",
                            "--category", "food", "--fx", "0.01"], env=self.env, check=True, capture_output=True)
        listing = subprocess.run(S + ["trip", "list"], env=self.env, capture_output=True, text=True).stdout
        self.assertIn(f"chat {GROUP}", listing)
        r = self.run_py("report.py", "excel", "--chat", GROUP, "--send", "--to", OWNER)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f'name="chat_id"\r\n\r\n{OWNER}', FakeTelegram.sent[-1])
        self.assertIn("filename=", FakeTelegram.sent[-1])

    def test_second_listener_refuses_to_start(self):
        import fcntl
        home = Path(self.env["SPLITBOT_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        with open(home / "listen.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a listener is "running"
            r = self.run_py("listen.py", "5")
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertEqual(r.stdout, "")  # nothing emitted, so nothing handled twice

    @unittest.skipUnless(HAS_BROWSER, "needs node + Chromium")
    def test_chart_sent_once_per_request(self):
        S = [sys.executable, str(HERE / "splitlog.py")]
        subprocess.run(S + ["member", "add", "--chat", GROUP, "--name", "A,B"], env=self.env,
                       check=True, capture_output=True)
        subprocess.run(S + ["add", "--chat", GROUP, "--payer", "A", "--amount", "100", "--desc", "x",
                            "--category", "food", "--fx", "0.01"], env=self.env, check=True, capture_output=True)
        for _ in range(2):  # the same request handled twice
            r = self.run_py("report.py", "chart", "--chat", GROUP, "--send", "--update-id", "77", timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(FakeTelegram.sent), 1)
        self.assertIn("already sent", r.stdout)

    def test_missing_config(self):
        env = {k: v for k, v in self.env.items() if k != "SPLIT_BOT_TOKEN"}
        r = subprocess.run([sys.executable, str(HERE / "listen.py")], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
