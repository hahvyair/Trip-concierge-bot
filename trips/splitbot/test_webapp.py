"""Tests for the trip app's sync (webapp_sync.py) and send.py's link button.
Run: cd trips/splitbot && python3 -m pytest -q test_webapp.py

Everything runs in temp folders; the sync posts to a local fake Worker and the
button goes to the fake Telegram server from test_splitbot.py. Nothing leaves
the machine and no real token is used.
"""

import io
import json
import os
import subprocess
import sys
import threading
import tempfile
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_splitbot import GROUP, OWNER, FakeTelegram, _load, upd  # noqa: E402

splitlog = _load("splitbot_splitlog_web", "splitlog.py")
sync = _load("splitbot_webapp_sync", "webapp_sync.py")
plan = _load("splitbot_plan_web", "plan.py")

TOKEN = "123456:TEST-token_for-vectors"
# The same vector is checked in trips/webapp/test/worker.test.mjs (the Worker's side).
VECTOR_BODY = b'{"trip":{"id":"vector"},"generated_at":"2026-01-01T00:00:00+00:00","members":[]}'
VECTOR_KEY = "825e2d8a08cd3a3e390fb3be38d59beff891fc43734bfeb26c71c8b36bd81f38"
VECTOR_SIG = "3b8cec0892e10f8593b3f3d5ec34f28c761411eec546367378a3b8196867f2a3"


def run(mod, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = mod.main(list(args))
    return code, buf.getvalue()


class FakeWorker(BaseHTTPRequestHandler):
    got = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        ok = self.headers.get("X-Sync-Signature") == sync.signature(TOKEN, body)
        self.got.append((self.path, body, ok))
        self.send_response(200 if ok else 401)
        self.end_headers()
        self.wfile.write(b'{"ok":true}' if ok else b'{"error":"bad signature"}')


class WakeWords(unittest.TestCase):
    def test_app_requests_reach_the_session_but_app_chatter_does_not(self):
        listen = _load("splitbot_listen_web", "listen.py")
        for text in ("open the app", "can I see the dashboard", "trip app pls", "/app"):
            self.assertTrue(listen.KEYWORDS.search(text) or text.startswith("/"), text)
        self.assertIsNone(listen.KEYWORDS.search("grab app is down again"))


class Signature(unittest.TestCase):
    def test_fixed_vector_matches_the_worker(self):
        self.assertEqual(sync.sync_key(TOKEN).hex(), VECTOR_KEY)
        self.assertEqual(sync.signature(TOKEN, VECTOR_BODY), VECTOR_SIG)


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = d / "webapp" / "config.json"
        self.env = mock.patch.dict(os.environ, {"SPLIT_DATA": str(d / "data"), "SPLITBOT_HOME": str(d / "home"),
                                                "SPLIT_BOT_TOKEN": ""})
        self.env.start()
        self.cfg_patch = mock.patch.object(sync, "CONFIG", self.cfg)
        self.cfg_patch.start()
        self.ok(splitlog, "trip", "new", "--chat", GROUP, "--name", "Japan", "--currency", "JPY",
                "--members", "Ana:111:ana,Ben:222:benny,Chloe")
        for payer, amt, cat, extra in (("Ana", "9000", "food", []), ("Ben", "3000", "transport", ["--equal", "Ben,Chloe"]),
                                       ("Chloe", "12000", "drinks", ["--date", "2026-10-04"])):
            self.ok(splitlog, "add", "--chat", GROUP, "--payer", payer, "--amount", amt, "--desc", f"{cat} thing",
                    "--category", cat, "--fx", "0.0088", "--logged-by", "Ana Lim", "--note", "secret note",
                    "--update-id", f"9{amt}", *extra)
        self.ok(splitlog, "transfer", "--chat", GROUP, "--payer", "Chloe", "--to", "Ana", "--amount", "10",
                "--currency", "SGD")
        self.trip = splitlog.open_trip(GROUP)

    def tearDown(self):
        self.cfg_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def ok(self, mod, *args):
        code, out = run(mod, *args)
        self.assertEqual(code, 0, out)
        return out

    def test_shape_and_figures(self):
        s = sync.snapshot(self.trip)
        self.assertEqual(set(s), {"v", "generated_at", "trip", "members", "rows", "totals", "nets", "settle", "days",
                                  "by_day_cat", "cat_totals", "rate_note", "plan", "wrapped", "places"})
        self.assertEqual(s["trip"]["id"], self.trip["id"])
        self.assertEqual(s["members"], [{"name": "Ana", "tg_id": "111"}, {"name": "Ben", "tg_id": "222"},
                                        {"name": "Chloe", "tg_id": None}])
        self.assertEqual(len(s["rows"]), 4)
        self.assertEqual(s["totals"]["payments"], 3)
        self.assertEqual(s["totals"]["spent"], sum(r["sgd"] for r in s["rows"] if r["kind"] == "expense"))
        self.assertEqual(sum(v["net"] for v in s["nets"].values()), 0)
        self.assertEqual(sum(sum(d.values()) for d in s["by_day_cat"].values()), s["totals"]["spent"])
        self.assertIn("2026-10-04", s["days"])
        for r in s["rows"]:
            self.assertEqual(sum(r["shares"].values()), r["sgd"])
        self.assertEqual(s["rows"][0]["local"], "¥9,000")
        w = s["wrapped"]
        self.assertEqual(w["total"], s["totals"]["spent"])
        self.assertEqual(len(w["awards"]), 3)
        self.assertEqual(w["top_cat"], "drinks")
        self.assertIsNone(s["plan"])
        json.dumps(s)  # serialisable

    def test_nothing_private_leaks(self):
        text = json.dumps(sync.snapshot(self.trip), ensure_ascii=False)
        for word in ("benny", "ana\"", "Ana Lim", "secret note", "logged_by", "update_id", "username",
                     "fx_source", "chat_id", str(GROUP)):
            self.assertNotIn(word, text)

    def test_plan_and_no_wrapped_for_a_planning_trip_without_spending(self):
        self.ok(splitlog, "trip", "new", "--planning", "--chat", "-7007", "--name", "Bali", "--currency", "IDR",
                "--window", "Mar 2027", "--members", "Ana:111,Dan")
        self.ok(plan, "set", "--chat", "-7007", "--budget", "1500", "--destination", "Ubud,Canggu")
        self.ok(plan, "todo", "add", "--chat", "-7007", "--task", "Check visas", "--owner", "Dan")
        p = plan.load_plan(splitlog.open_trip("-7007"))
        p["polls"].append({"n": 1, "poll_id": "P9", "message_id": 5, "question": "Where?", "options": ["Ubud", "Canggu"],
                           "multi": False, "topic": "Stay", "votes": {"Ana": [1]}, "status": "open",
                           "outcome": None, "opened": "2026-10-01", "closed": None})
        p["reminded"] = {"todo:1": "2026-10-01"}
        plan.save_plan(splitlog.open_trip("-7007"), p)
        s = sync.snapshot(splitlog.open_trip("-7007"))
        self.assertEqual(s["trip"]["phase"], "planning")
        self.assertIsNone(s["wrapped"])
        self.assertEqual(s["plan"]["destinations"], ["Ubud", "Canggu"])
        poll = s["plan"]["polls"][0]
        self.assertEqual((poll["tally"], poll["not_voted"]), ([0, 1], ["Dan"]))
        self.assertNotIn("poll_id", poll)
        self.assertNotIn("reminded", s["plan"])

    def test_pick_trips_open_and_recently_closed(self):
        trips = splitlog.load_trips()
        trips.append({**trips[0], "id": "old", "status": "closed", "closed": "2020-01-01"})
        trips.append({**trips[0], "id": "recent", "status": "closed", "closed": "2026-10-01"})
        splitlog.save_trips(trips)
        from datetime import date
        ids = [t["id"] for t in sync.pick_trips(None, today=date(2026, 10, 6))]
        self.assertEqual(ids, [self.trip["id"], "recent"])

    def test_trips_closed_over_7_days_ago_are_removed(self):
        trips = splitlog.load_trips()
        for tid, closed in (("ancient", "2020-01-01"), ("expired", "2026-09-20"), ("week", "2026-09-29")):
            trips.append({**trips[0], "id": tid, "status": "closed", "closed": closed})
        splitlog.save_trips(trips)
        from datetime import date
        today = date(2026, 10, 6)
        self.assertEqual([t["id"] for t in sync.pick_trips(None, today=today)], [self.trip["id"], "week"])
        self.assertEqual([t["id"] for t in sync.expired_trips(today=today)], ["expired"])

    def test_dry_run_and_unconfigured_send_nothing(self):
        code, out = run(sync, "--dry-run")
        self.assertEqual(code, 0)
        self.assertRegex(out, rf"{self.trip['id']}: \d+\.\d KB \(dry run")
        code, out = run(sync)
        self.assertEqual(code, 0)
        self.assertIn("not configured", out)
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text(json.dumps({"url": "http://127.0.0.1:9"}))
        code, out = run(sync)
        self.assertEqual(code, 0)
        self.assertIn("no bot token", out)

    def test_posts_signed_snapshots_and_deletes(self):
        FakeWorker.got = []
        srv = HTTPServer(("127.0.0.1", 0), FakeWorker)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.cfg.parent.mkdir(parents=True)
            self.cfg.write_text(json.dumps({"url": f"http://127.0.0.1:{srv.server_port}/"}))
            with mock.patch.dict(os.environ, {"SPLIT_BOT_TOKEN": TOKEN, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                code, out = run(sync)
                self.assertEqual(code, 0, out)
                self.assertIn("synced", out)
                code, out = run(sync, "--delete", self.trip["id"])
                self.assertEqual(code, 0, out)
            self.assertEqual([(p, ok) for p, _, ok in FakeWorker.got], [("/api/sync", True), ("/api/sync", True)])
            self.assertEqual(json.loads(FakeWorker.got[0][1])["trip"]["id"], self.trip["id"])
            self.assertEqual(json.loads(FakeWorker.got[1][1])["delete"], self.trip["id"])
            with mock.patch.dict(os.environ, {"SPLIT_BOT_TOKEN": "999:WRONG", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                code, out = run(sync)
            self.assertEqual(code, 1)
            self.assertIn("HTTP 401", out)
            self.assertNotIn("999:WRONG", out)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_link_and_short_name(self):
        code, out = run(sync, "--link", "--chat", GROUP)
        self.assertEqual(code, 1)
        self.assertEqual(run(sync, "--set-short-name", "bad name!")[0], 1)
        self.assertEqual(run(sync, "--set-short-name", "trips")[0], 0)
        cfg = json.loads(self.cfg.read_text())
        cfg["bot_username"] = "tripsplit_bot"
        self.cfg.write_text(json.dumps(cfg))
        code, out = run(sync, "--link", "--chat", GROUP)
        self.assertEqual((code, out.strip()), (0, f"https://t.me/tripsplit_bot/trips?startapp={self.trip['id']}"))


class Button(unittest.TestCase):
    def setUp(self):
        FakeTelegram.sent = []
        self.srv = HTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "data").mkdir()
        (d / "data" / "trips.json").write_text(json.dumps({"trips": [
            {"id": "t", "name": "Japan", "chat_id": GROUP, "currency": "JPY", "status": "open",
             "created": "2026-10-01", "closed": None, "members": []}]}))
        self.env = {**{k: v for k, v in os.environ.items() if k != "TELEGRAM_CHAT_ID"},
                    "SPLIT_BOT_TOKEN": "TEST", "SPLIT_OWNER_ID": OWNER, "SPLIT_DATA": str(d / "data"),
                    "SPLITBOT_HOME": str(d / "home"), "SPLITBOT_API": f"http://127.0.0.1:{self.srv.server_port}",
                    "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def send(self, *args):
        return subprocess.run([sys.executable, str(HERE / "send.py"), *args], env=self.env, input="Trip app",
                              capture_output=True, text=True, timeout=30)

    def test_button_message(self):
        url = "https://t.me/tripsplit_bot/trips?startapp=2026-10-japan"
        r = self.send("--chat", GROUP, "--button-text", "Open the trip app", "--button-url", url)
        self.assertEqual(r.returncode, 0, r.stderr)
        markup = json.loads(parse_qs(FakeTelegram.sent[-1])["reply_markup"][0])
        self.assertEqual(markup, {"inline_keyboard": [[{"text": "Open the trip app", "url": url}]]})

    def test_button_rules(self):
        self.assertNotEqual(self.send("--chat", GROUP, "--button-text", "x").returncode, 0)
        self.assertNotEqual(self.send("--chat", GROUP, "--button-text", "x", "--button-url", "https://evil.example").returncode, 0)
        r = self.send("--chat", "-999", "--button-text", "x", "--button-url", "https://t.me/a/b")
        self.assertEqual(r.returncode, 2)  # not a trip group: the usual chat restriction applies
        self.assertEqual(FakeTelegram.sent, [])


if __name__ == "__main__":
    unittest.main()


class AutoSync(unittest.TestCase):
    """Every save of the real trip data queues one background app sync."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = d / "config.json"
        self.env = mock.patch.dict(os.environ, {"SPLITBOT_HOME": str(d / "home")})
        self.env.start()
        os.environ.pop("SPLIT_DATA", None)
        os.environ.pop("SPLITBOT_NO_SYNC", None)
        self.patches = [mock.patch.object(splitlog, "APP_CONFIG", self.cfg),
                        mock.patch.object(splitlog.subprocess, "Popen")]
        for p in self.patches:
            p.start()
        self.popen = splitlog.subprocess.Popen

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_nothing_until_the_app_is_deployed(self):
        splitlog.sync_app()
        self.popen.assert_not_called()

    def test_one_queued_sync_covers_several_saves(self):
        self.cfg.write_text("{}")
        splitlog.sync_app()
        splitlog.sync_app()
        self.assertEqual(self.popen.call_count, 1)
        self.assertIn("webapp_sync.py", self.popen.call_args[0][0][2])
        pending = Path(os.environ["SPLITBOT_HOME"]) / "sync.pending"
        os.utime(pending, (0, 0))  # a queued sync that never ran doesn't block the next
        splitlog.sync_app()
        self.assertEqual(self.popen.call_count, 2)

    def test_never_for_test_or_other_data(self):
        self.cfg.write_text("{}")
        with mock.patch.dict(os.environ, {"SPLIT_DATA": self.tmp.name}):
            splitlog.sync_app()
        with mock.patch.dict(os.environ, {"SPLITBOT_NO_SYNC": "1"}):
            splitlog.sync_app()
        self.popen.assert_not_called()


# ------------------------------------------------------------ app changes
ACK_BODY = b'{"op":"ack","ids":["1791000000000-0a1b2c3d"],"ts":"2026-01-01T00:00:00+00:00"}'
ACK_SIG = "8aab54f148ffb547bc2ad70105e4972784d532554e6cb4490f6ec91618aa5f9d"  # also in worker.test.mjs
A1, A2, A3 = "1791000000001-0000000a", "1791000000002-0000000b", "1791000000003-0000000c"


class FakeAppWorker(BaseHTTPRequestHandler):
    """The Worker's change queue: /api/actions (list) and /api/actions/ack, signed."""
    actions, got = [], []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        ok = self.headers.get("X-Sync-Signature") == sync.signature(TOKEN, body)
        msg = json.loads(body) if ok else {}
        self.got.append((self.path, msg, ok))
        if not ok:
            self.send_response(401)
            self.end_headers()
            return self.wfile.write(b'{"error":"bad signature"}')
        if self.path == "/api/actions" and msg.get("op") == "list":
            out = {"ok": True, "actions": self.actions}
        elif self.path == "/api/actions/ack" and msg.get("op") == "ack":
            FakeAppWorker.actions = [a for a in self.actions if a["id"] not in msg["ids"]]
            out = {"ok": True}
        else:
            self.send_response(400)
            self.end_headers()
            return self.wfile.write(b'{"error":"bad request"}')
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())


def action(aid, kind, name, to=None, by="222", trip="t"):
    return {"id": aid, "trip_id": trip, "action": kind, "name": name, "to": to,
            "by": {"tg_id": by, "name": "whoever", "member": None, "owner": False}, "at": "2026-10-06T01:00:00Z"}


TRIPS = [{"id": "t", "name": "Japan", "chat_id": GROUP, "currency": "JPY", "status": "open",
          "created": "2026-10-01", "closed": None,
          "members": [{"name": "Ana", "tg_id": OWNER, "username": None},
                      {"name": "Ben", "tg_id": "222", "username": None},
                      {"name": "Chloe", "tg_id": None, "username": None}]},
         {"id": "old", "name": "Bali", "chat_id": "-5005", "currency": "IDR", "status": "closed",
          "created": "2026-01-01", "closed": "2026-01-09", "members": [{"name": "Ben", "tg_id": "222"}]}]


class AppEvent(unittest.TestCase):
    """The listener rechecks each queued change against the repo's members."""

    def setUp(self):
        self.listen = _load("splitbot_listen_app", "listen.py")

    def ev(self, a):
        return self.listen.app_event(a, TRIPS, OWNER)

    def test_allowed_changes(self):
        e = self.ev(action(A1, "rename_member", "Ben", "Benji"))
        self.assertNotIn("refused", e)
        self.assertEqual((e["kind"], e["chat_id"], e["by"]["name"], e["by"]["is_owner"], e["name"], e["to"]),
                         ("app_action", GROUP, "Ben", False, "Ben", "Benji"))
        for a in (action(A1, "rename_member", "Chloe", "Clo", by=OWNER), action(A2, "add_member", "Mei", by=OWNER)):
            e = self.ev(a)
            self.assertNotIn("refused", e)
            self.assertTrue(e["by"]["is_owner"])
            self.assertEqual(e["by"]["name"], "Ana")
        self.assertIsNone(self.ev(action(A2, "add_member", "Mei", "x", by=OWNER))["to"])
        self.assertNotIn("refused", self.ev(action(A2, "add_member", "Zoë O'Brien-Lee", by=OWNER)))
        # a stranger's Telegram name never reaches the group as typed
        self.assertEqual(self.ev(action(A1, "add_member", "Mei", by="999") | {"by": {"tg_id": "999", "name": "$(x)"}})["by"]["name"], "Someone")

    def test_refused(self):
        cases = [
            (action(A1, "rename_member", "Chloe", "Clo"), "only rename themselves"),
            (action(A1, "add_member", "Mei"), "only the owner"),
            (action(A1, "rename_member", "Ben", "Benji", by="999"), "isn't on this trip"),
            (action(A1, "rename_member", "Ben", "Benji", trip="nope"), "no such trip"),
            (action(A1, "rename_member", "Ben", "B", trip="old"), "closed"),
            (action(A1, "delete_trip", "Ben"), "malformed"),
            (action(A1, "add_member", "Mei,Ben", by=OWNER), "malformed"),
            (action(A1, "rename_member", "Ben", "Ben\nhi"), "malformed"),
            (action(A1, "rename_member", "Ben", " Ben"), "malformed"),
            (action(A1, "rename_member", "Ben", "x" * 32), "malformed"),
            (action(A1, "rename_member", "Ben", "$(id)"), "malformed"),
            (action(A1, "add_member", "-rf", by=OWNER), "malformed"),
            (action(A1, "add_member", 'Mei"', by=OWNER), "malformed"),
        ]
        for a, why in cases:
            self.assertIn(why, self.ev(a).get("refused", ""), a)


class AppQueue(unittest.TestCase):
    """--ack, and the listener collecting queued changes from a fake Worker."""

    def setUp(self):
        FakeAppWorker.actions, FakeAppWorker.got = [], []
        FakeTelegram.updates, FakeTelegram.polled, FakeTelegram.sent = [], [], []
        self.srvs = [HTTPServer(("127.0.0.1", 0), FakeAppWorker), HTTPServer(("127.0.0.1", 0), FakeTelegram)]
        for s in self.srvs:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "data").mkdir()
        (d / "data" / "trips.json").write_text(json.dumps({"trips": TRIPS}))
        self.cfg = d / "config.json"
        self.cfg.write_text(json.dumps({"url": f"http://127.0.0.1:{self.srvs[0].server_port}"}))
        self.home = d / "home"
        self.env = {**{k: v for k, v in os.environ.items() if k != "TELEGRAM_CHAT_ID"},
                    "SPLIT_BOT_TOKEN": TOKEN, "SPLIT_OWNER_ID": OWNER, "SPLIT_DATA": str(d / "data"),
                    "SPLITBOT_HOME": str(self.home), "SPLITBOT_APP_CONFIG": str(self.cfg),
                    "SPLITBOT_API": f"http://127.0.0.1:{self.srvs[1].server_port}",
                    "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}

    def tearDown(self):
        for s in self.srvs:
            s.shutdown()
            s.server_close()
        self.tmp.cleanup()

    def py(self, script, *args, env=None, timeout=60):
        return subprocess.run([sys.executable, str(HERE / script), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=timeout)

    def test_ack_signature_vector(self):
        self.assertEqual(sync.signature(TOKEN, ACK_BODY), ACK_SIG)

    def test_ack_posts_a_signed_ack_and_remembers_it(self):
        FakeAppWorker.actions = [action(A1, "rename_member", "Ben", "Benji"), action(A2, "add_member", "Mei", by=OWNER)]
        r = self.py("webapp_sync.py", "--ack", f"{A1},{A2}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        path, msg, ok = FakeAppWorker.got[-1]
        self.assertEqual((path, ok, msg["op"], msg["ids"]), ("/api/actions/ack", True, "ack", [A1, A2]))
        self.assertEqual(FakeAppWorker.actions, [])
        self.assertEqual(set(json.loads((self.home / "app_acked.json").read_text())), {A1, A2})

    def test_ack_rules(self):
        self.assertEqual(self.py("webapp_sync.py", "--ack", "1;rm -rf").returncode, 1)
        self.assertEqual(self.py("webapp_sync.py", "--ack", "").returncode, 1)
        self.assertEqual(FakeAppWorker.got, [])
        bad = self.py("webapp_sync.py", "--ack", A1, env={**self.env, "SPLIT_BOT_TOKEN": "999:WRONG"})
        self.assertEqual(bad.returncode, 1)
        self.assertIn("HTTP 401", bad.stdout)
        self.assertNotIn("999:WRONG", bad.stdout + bad.stderr)
        self.assertIn(A1, json.loads((self.home / "app_acked.json").read_text()))  # kept for the listener to retry

    def test_listener_emits_app_changes_without_waiting_for_telegram(self):
        FakeAppWorker.actions = [action(A1, "rename_member", "Ben", "Benji"),
                                 action(A2, "rename_member", "Chloe", "Clo")]
        r = self.py("listen.py", "20")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[0], "=== SPLIT BOT EVENTS ===")
        evs = [json.loads(x) for x in lines[1:]]
        self.assertEqual([(e["kind"], e["action_id"], e["chat_id"]) for e in evs],
                         [("app_action", A1, GROUP), ("app_action", A2, GROUP)])
        self.assertNotIn("refused", evs[0])
        self.assertIn("only rename themselves", evs[1]["refused"])
        self.assertEqual(FakeTelegram.polled, [])  # emitted before the Telegram long poll
        self.assertEqual(FakeAppWorker.got[0][:3:2], ("/api/actions", True))
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_acked_changes_are_skipped_and_their_ack_resent(self):
        FakeAppWorker.actions = [action(A1, "rename_member", "Ben", "Benji")]
        self.home.mkdir(parents=True)
        (self.home / "app_acked.json").write_text(json.dumps({A1: 1.0}))  # acked long ago; the Worker still lists it
        r = self.py("listen.py", "3")
        self.assertEqual(r.returncode, 3, r.stderr)  # nothing new: polls Telegram until the deadline
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn(("/api/actions/ack", True), [(p, ok) for p, _, ok in FakeAppWorker.got])
        self.assertTrue(FakeTelegram.polled)

    def test_a_down_worker_never_stalls_telegram(self):
        self.cfg.write_text(json.dumps({"url": "http://127.0.0.1:9"}))  # nothing listens there
        FakeTelegram.updates = [upd(7, GROUP, 222, text="paid 100 for taxi")]
        r = self.py("listen.py", "20", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.splitlines()[1])["update_id"], 7)
        self.assertEqual(r.stderr.count("trip app queue not reached"), 1)

    def test_no_app_polling_for_test_data_without_an_explicit_config(self):
        FakeAppWorker.actions = [action(A1, "rename_member", "Ben", "Benji")]
        env = {k: v for k, v in self.env.items() if k != "SPLITBOT_APP_CONFIG"}
        r = self.py("listen.py", "2", env=env)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(FakeAppWorker.got, [])
