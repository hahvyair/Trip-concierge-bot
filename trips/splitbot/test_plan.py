"""Tests for trip planning (plan.py, the planning phase, polls, chat log, reminders).
Run: cd trips/splitbot && python3 -m pytest -q   (or python3 -m unittest test_plan.py)

Everything runs in temp folders, against the fake Telegram server from
test_splitbot.py, so no token is needed and nothing is sent.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from http.server import HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_splitbot import BOT, GROUP, OWNER, FakeTelegram, _load, upd  # noqa: E402

listen = _load("splitbot_listen_plan", "listen.py")
splitlog = _load("splitbot_splitlog_plan", "splitlog.py")
plan = _load("splitbot_plan", "plan.py")
report = _load("splitbot_report_plan", "report.py")

PLANNING = "-7007"   # a group planning its trip
FUTURE = (date.today() + timedelta(days=60)).isoformat()


def run(mod, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = mod.main(list(args))
    return code, buf.getvalue()


class TempData(unittest.TestCase):
    """A temp data folder with a live trip (GROUP) and a planning trip (PLANNING)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"SPLIT_DATA": str(d / "data"), "SPLITBOT_HOME": str(d / "home")})
        self.env.start()
        self.ok(splitlog, "trip", "new", "--chat", GROUP, "--name", "Hanoi", "--currency", "VND",
                "--members", "Sam:111,Kai")
        self.ok(splitlog, "trip", "new", "--planning", "--chat", PLANNING, "--name", "Japan",
                "--currency", "JPY", "--window", "Dec 2026", "--members", "Ana:111:ana,Ben,Chloe")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def ok(self, mod, *args):
        code, out = run(mod, *args)
        self.assertEqual(code, 0, out)
        return out

    def P(self, cmd, *args):
        """plan.py <cmd> --chat PLANNING <args>, which must succeed."""
        return self.ok(plan, cmd, "--chat", PLANNING, *args)

    def trip(self, chat=PLANNING):
        return splitlog.open_trip(chat)

    def plan(self):
        return plan.load_plan(self.trip())


class Phase(TempData):
    def test_phase_logic(self):
        self.assertEqual(splitlog.phase(self.trip(GROUP)), "live")          # no planning fields: as today
        self.assertEqual(splitlog.phase(self.trip()), "planning")           # /plan, no dates yet
        self.ok(splitlog, "trip", "dates", "--chat", PLANNING, "--start", FUTURE)
        self.assertEqual(splitlog.phase(self.trip()), "planning")           # start in the future
        self.assertEqual(splitlog.phase(self.trip(), today=FUTURE), "live")  # start day reached
        old = {"status": "open", "start": "2020-01-01", "currency": "SGD"}
        self.assertEqual(splitlog.phase(old), "live")
        self.assertEqual(splitlog.phase({**old, "status": "closed"}), "closed")
        self.assertEqual(splitlog.phase({"status": "open", "currency": "SGD", "start": FUTURE}), "planning")
        code, out = run(splitlog, "trip", "dates", "--chat", PLANNING, "--end", "2020-01-01")
        self.assertEqual(code, 1)
        self.assertIn("before the start", out)

    def test_live_trip_untouched_and_payments_allowed_while_planning(self):
        self.assertNotIn("planning", json.dumps(self.trip(GROUP)))
        self.assertNotIn("start", self.trip(GROUP))
        self.ok(splitlog, "add", "--chat", PLANNING, "--payer", "Ben", "--amount", "300", "--currency", "SGD",
                "--desc", "hotel deposit", "--category", "lodging")
        self.assertIn("Paid so far (deposits etc.): S$300.00", self.P("show"))
        listing = self.ok(splitlog, "trip", "list")
        self.assertIn("Japan\tchat -7007\tJPY", listing)
        self.assertTrue(listing.splitlines()[1].endswith("planning"))
        self.assertFalse(listing.splitlines()[0].endswith("planning"))

    def test_nightly_skips_planning_trips(self):
        for chat, payer in ((GROUP, "Sam"), (PLANNING, "Ben")):
            self.ok(splitlog, "add", "--chat", chat, "--payer", payer, "--amount", "10", "--currency", "SGD",
                    "--desc", "x", "--category", "food")
        with mock.patch.object(report, "post_daily", return_value=1) as post:
            code, out = run(report, "nightly", "--at", "00:00")
        self.assertEqual(code, 0)
        self.assertIn("Japan: planning; no nightly report", out)
        self.assertIn("Hanoi: nightly report sent", out)
        self.assertEqual([c.args[0]["name"] for c in post.call_args_list], ["Hanoi"])
        self.assertNotIn("last_report_day", self.trip())


class PlanData(TempData):
    def test_plan_ops_and_show(self):
        self.P("set", "--destination", "Osaka,Kyoto", "--budget", "2500", "--note", "Chloe can't fly before 10 Dec")
        self.P("set", "--start", FUTURE)
        self.P("decide", "--topic", "Airline", "--outcome", "SQ", "--by", "Ana,ben")
        self.assertIn("replaces 'SQ'", self.P("decide", "--topic", "airline", "--outcome", "ANA"))
        self.P("idea", "add", "--text", "Ryokan in Kinosaki", "--by", "Ben", "--kind", "stay", "--link", "https://x.y")
        self.P("idea", "add", "--text", "Universal Studios")
        self.P("idea", "update", "--n", "1", "--status", "shortlisted")
        self.P("todo", "add", "--task", "Check JR pass", "--owner", "@ana", "--due", FUTURE)
        self.P("todo", "add", "--task", "Ask about leave", "--owner", "Chloe")
        self.P("todo", "done", "--n", "2")
        self.P("booking", "add", "--item", "Flights SIN-KIX", "--kind", "flights", "--who", "Ben",
               "--est", "650", "--deadline", FUTURE)
        self.P("booking", "add", "--item", "Osaka hotel", "--kind", "stay")
        self.P("booking", "update", "--n", "1", "--status", "booked")
        self.P("itinerary", "add", "--day", "Day 1", "--what", "Dotonbori", "--time", "19:00", "--where", "Namba")
        self.P("itinerary", "add", "--day", "Day 1", "--what", "Check in", "--time", "15:00")
        self.assertIn("1. 15:00 Check in", self.P("itinerary", "show"))
        self.P("itinerary", "remove", "--day", "Day 1", "--n", "1")
        self.assertNotIn("Check in", self.P("itinerary", "show"))

        out = self.P("show")
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^Japan · planning, (59|60|61) days to go$")
        self.assertIn("Where: Osaka, Kyoto · Budget: S$2,500.00 pp", out)
        self.assertIn("Who: Ana, Ben, Chloe", out)
        self.assertIn("Note: Chloe can't fly before 10 Dec", out)
        self.assertIn("• Airline: ANA (agreed in chat", out)
        self.assertNotIn("Airline: SQ", out)                             # superseded
        self.assertIn("Shortlist: #1 Ryokan in Kinosaki", out)
        self.assertIn("Ideas: 1 on the board (#2 Universal Studios)", out)
        self.assertIn("• Ana: #1 Check JR pass", out)
        self.assertNotIn("Ask about leave", out)                         # done
        self.assertIn("Bookings: 1 to do, 1 booked:", out)
        self.assertIn("• #2 Osaka hotel: todo (nobody yet)", out)
        self.assertIn("Estimated bookings: S$650.00", out)
        self.assertLessEqual(len(lines), 20)
        saved = json.loads((Path(os.environ["SPLIT_DATA"]) / "plans" / f"{self.trip()['id']}.json").read_text())
        self.assertEqual(saved["decisions"][0]["confirmed_by"], ["Ana", "Ben"])
        self.assertTrue(saved["decisions"][0]["superseded"])

    def test_refuses_refs_ids_and_unknown_people(self):
        for text in ("booking ref ABC123", "passport E1234567", "card 4111 1111 1111 1111"):
            code, out = run(plan, "idea", "add", "--chat", PLANNING, "--text", text)
            self.assertEqual(code, 1, text)
            self.assertIn("never stores", out)
        self.P("idea", "add", "--text", "card games night on the shinkansen")  # not a ref
        code, out = run(plan, "todo", "add", "--chat", PLANNING, "--task", "x", "--owner", "Zed")
        self.assertIn("isn't on this trip", out)
        code, out = run(plan, "show", "--chat", "-555")
        self.assertIn("No open trip", out)

    def test_poll_votes_retraction_all_voted_and_close(self):
        p = self.plan()
        p["polls"].append({"n": 1, "poll_id": "P1", "message_id": 91, "question": "Where to stay?",
                           "options": ["Namba", "Umeda", "Kyoto"], "multi": False, "topic": "Stay",
                           "votes": {}, "status": "open", "outcome": None, "opened": "2026-10-06", "closed": None})
        plan.save_plan(self.trip(), p)
        vote = lambda uid, name, opts: self.P("poll", "vote", "--poll-id", "P1", "--user-id", uid,  # noqa: E731
                                              "--name", name, "--options", opts)
        out = vote("111", "Ana L", "0")
        self.assertIn("Ana voted Namba", out)
        self.assertIn("Waiting on: Ben, Chloe", out)
        out = vote("222", "Ben Tan", "1")                      # first name links Ben's Telegram id
        self.assertEqual(splitlog.find_member(self.trip(), "222")["name"], "Ben")
        out = vote("222", "Ben Tan", "")                       # retracted
        self.assertIn("Ben retracted their vote", out)
        self.assertIn("Waiting on: Ben, Chloe", out)
        vote("222", "Ben Tan", "0")
        self.assertIn("not voted: Chloe", self.P("show"))
        out = vote("333", "Chloe", "1")
        self.assertIn("ALL VOTED", out)
        self.assertIn("Namba leads with 2", out)
        self.assertIn("Tally: Namba 2 · Umeda 1 · Kyoto 0", out)
        out = self.P("poll", "close", "--n", "1", "--no-stop")
        self.assertIn("closed: Namba (2 of 3 votes)", out)
        self.assertIn("• Stay: Namba (poll #1", self.P("show"))
        self.assertIn("already closed", vote("444", "Dan", "2"))

    def test_tie_is_not_decided(self):
        p = self.plan()
        p["polls"].append({"n": 1, "poll_id": "P9", "message_id": 91, "question": "Dates?",
                           "options": ["12-19 Dec", "19-26 Dec"], "multi": True, "topic": "Dates",
                           "votes": {"Ana": [0, 1], "Ben": [0], "Chloe": [1]}, "status": "open",
                           "outcome": None, "opened": "2026-10-06", "closed": None})
        plan.save_plan(self.trip(), p)
        out = self.P("poll", "close", "--n", "1", "--no-stop")
        self.assertIn("tie between 12-19 Dec and 19-26 Dec (2 each). Not decided", out)
        self.assertEqual(self.plan()["decisions"], [])
        self.assertEqual(self.plan()["polls"][0]["outcome"], "tie: 12-19 Dec / 19-26 Dec")


class ListenerPlanning(unittest.TestCase):
    def test_poll_answer_only_for_known_polls_and_mapped_to_chat(self):
        polls = {"P1": {"chat_id": PLANNING, "n": 3}}
        resp = {"ok": True, "result": [
            {"update_id": 1, "poll_answer": {"poll_id": "P1", "user": {"id": 222, "first_name": "Ben"},
                                             "option_ids": [0, 2]}},
            {"update_id": 2, "poll_answer": {"poll_id": "ZZ", "user": {"id": 222}, "option_ids": [1]}},
            {"update_id": 3, "poll_answer": {"poll_id": "P1", "user": {"id": int(OWNER), "first_name": "Ana"},
                                             "option_ids": []}},
        ]}
        nxt, evs = listen.parse_updates(resp, OWNER, {GROUP}, BOT, polls)
        self.assertEqual(nxt, 4)
        self.assertEqual([e["update_id"] for e in evs], [1, 3])
        self.assertEqual((evs[0]["kind"], evs[0]["chat_id"], evs[0]["poll_n"], evs[0]["option_ids"]),
                         ("poll_answer", PLANNING, 3, [0, 2]))
        self.assertEqual(evs[1]["option_ids"], [])            # retracted
        self.assertTrue(evs[1]["from"]["is_owner"])
        self.assertEqual(listen.parse_updates(resp, OWNER, {GROUP}, BOT)[1], [])  # no plans: all dropped

    def test_planning_words_wake_only_planning_groups(self):
        resp = {"ok": True, "result": [upd(1, PLANNING, 222, text="can we vote on the itinerary"),
                                       upd(2, GROUP, 222, text="what's the plan tonight"),
                                       upd(3, PLANNING, 222, text="lol nice")]}
        _, evs = listen.parse_updates(resp, OWNER, {GROUP, PLANNING}, BOT, {}, {PLANNING})
        self.assertEqual([e["update_id"] for e in evs], [1])
        logged = listen.chatlog_entries(resp, {PLANNING})
        self.assertEqual([e["text"] for e in logged[PLANNING]], ["can we vote on the itinerary", "lol nice"])
        self.assertNotIn(GROUP, logged)
        self.assertEqual(set(logged[PLANNING][0]), {"name", "text", "date"})


class PlanTelegram(unittest.TestCase):
    """End to end against the fake Telegram server."""

    def setUp(self):
        FakeTelegram.updates, FakeTelegram.actions, FakeTelegram.sent, FakeTelegram.polled = [], [], [], []
        self.srv = HTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.vars = {"SPLIT_BOT_TOKEN": "TEST", "SPLIT_OWNER_ID": OWNER,
                     "SPLIT_DATA": str(d / "data"), "SPLITBOT_HOME": str(d / "home"),
                     "SPLITBOT_API": f"http://127.0.0.1:{self.srv.server_port}",
                     "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        self.envp = mock.patch.dict(os.environ, self.vars)
        self.envp.start()
        self.env = {**{k: v for k, v in os.environ.items() if k != "TELEGRAM_CHAT_ID"}}
        run(splitlog, "trip", "new", "--chat", GROUP, "--name", "Hanoi", "--currency", "VND", "--members", "Sam,Kai")
        run(splitlog, "trip", "new", "--planning", "--chat", PLANNING, "--name", "Japan", "--currency", "JPY",
            "--members", "Ana:111,Ben,Chloe")

    def tearDown(self):
        self.envp.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def run_py(self, script, *args, stdin=None):
        return subprocess.run([sys.executable, str(HERE / script), *args], env=self.env, input=stdin,
                              capture_output=True, text=True, timeout=60)

    def test_send_poll_payload_and_restrictions(self):
        r = self.run_py("send.py", "--chat", PLANNING, "--poll", "Dates?", "--option", "12-19 Dec",
                        "--option", "19-26 Dec", "--multi")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"message \d+\) poll_id=P1")
        body = {k: v[0] for k, v in parse_qs(FakeTelegram.sent[-1]).items()}
        self.assertEqual(body["is_anonymous"], "false")
        self.assertEqual(body["allows_multiple_answers"], "true")
        self.assertEqual(body["question"], "Dates?")
        self.assertEqual(json.loads(body["options"]), [{"text": "12-19 Dec"}, {"text": "19-26 Dec"}])
        r = self.run_py("send.py", "--chat", PLANNING, "--stop-poll", "91")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(parse_qs(FakeTelegram.sent[-1])["message_id"], ["91"])
        bad = self.run_py("send.py", "--chat", "-2002", "--poll", "Q", "--option", "a", "--option", "b")
        self.assertEqual(bad.returncode, 2)
        one = self.run_py("send.py", "--chat", PLANNING, "--poll", "Q", "--option", "a")
        self.assertEqual(one.returncode, 2)
        self.assertEqual(len(FakeTelegram.sent), 2)

    def test_poll_new_records_and_close_stops_it(self):
        code, out = run(plan, "poll", "new", "--chat", PLANNING, "--question", "Where to stay?",
                        "--option", "Namba", "--option", "Umeda", "--topic", "Stay")
        self.assertEqual(code, 0, out)
        self.assertIn("Poll #1 posted", out)
        p = plan.load_plan(splitlog.open_trip(PLANNING))["polls"][0]
        self.assertEqual((p["poll_id"], p["status"], p["multi"]), ("P1", "open", False))
        self.assertEqual(listen.known_polls(), {"P1": {"chat_id": PLANNING, "n": 1}})
        run(plan, "poll", "vote", "--chat", PLANNING, "--poll-id", "P1", "--user-id", "111", "--options", "1")
        code, out = run(plan, "poll", "close", "--chat", PLANNING, "--n", "1")
        self.assertIn("closed: Umeda", out)
        self.assertIn("message_id=", FakeTelegram.sent[-1])    # stopPoll went out
        self.assertEqual(listen.known_polls(), {})             # closed polls no longer accepted

    def test_due_reminds_once_per_item_per_day(self):
        soon = (date.today() + timedelta(days=1)).isoformat()
        far = (date.today() + timedelta(days=30)).isoformat()
        run(plan, "todo", "add", "--chat", PLANNING, "--task", "Book flights", "--owner", "Ben", "--due", soon)
        run(plan, "todo", "add", "--chat", PLANNING, "--task", "Pack", "--owner", "Ben", "--due", far)
        run(plan, "booking", "add", "--chat", PLANNING, "--item", "Ryokan", "--kind", "stay",
            "--who", "Chloe", "--deadline", "2020-01-01")
        r = self.run_py("plan.py", "due", "--send", "--at", "00:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Japan: reminder sent (2 items)", r.stdout)
        self.assertEqual(len(FakeTelegram.sent), 1)
        text = parse_qs(FakeTelegram.sent[0])["text"][0]
        self.assertIn("Ben: Book flights", text)
        self.assertIn("Ryokan (todo), due 1 Jan, overdue", text)
        self.assertNotIn("Pack", text)
        r2 = self.run_py("plan.py", "due", "--send", "--at", "00:00")
        self.assertIn("Japan: nothing due", r2.stdout)
        self.assertEqual(len(FakeTelegram.sent), 1)

    def test_listener_logs_planning_chat_wakes_on_votes(self):
        run(plan, "poll", "new", "--chat", PLANNING, "--question", "Q?", "--option", "a", "--option", "b")
        FakeTelegram.sent = []
        FakeTelegram.updates = [
            upd(1, PLANNING, 222, text="lol that ryokan looks insane"),      # chatter: logged, no wake
            upd(2, GROUP, 333, text="anyone up for karaoke"),               # live trip: not logged
            {"update_id": 3, "poll_answer": {"poll_id": "P1", "user": {"id": 222, "first_name": "Ben"},
                                             "option_ids": [1]}},
            {"update_id": 4, "poll_answer": {"poll_id": "OTHER", "user": {"id": 9}, "option_ids": [0]}},
        ]
        r = self.run_py("listen.py", "20")
        self.assertEqual(r.returncode, 0, r.stderr)
        evs = [json.loads(x) for x in r.stdout.strip().splitlines()[1:]]
        self.assertEqual([(e["update_id"], e.get("kind"), e["chat_id"]) for e in evs],
                         [(3, "poll_answer", PLANNING)])
        self.assertIn("poll_answer", FakeTelegram.polled[-1].replace("%22", ""))
        self.assertEqual(FakeTelegram.actions, [])               # no "typing" for a vote
        logdir = Path(self.vars["SPLITBOT_HOME"]) / "chatlog"
        self.assertEqual(sorted(p.name for p in logdir.iterdir()), [f"{PLANNING}.jsonl"])
        logged = [json.loads(x) for x in (logdir / f"{PLANNING}.jsonl").read_text().splitlines()]
        self.assertEqual(logged, [{"name": "U222", "text": "lol that ryokan looks insane",
                                   "date": 1791000001}])
        code, out = run(plan, "chatlog", "--chat", PLANNING)
        self.assertIn("U222: lol that ryokan looks insane", out)

    def test_chatter_alone_never_wakes(self):
        FakeTelegram.updates = [upd(i, PLANNING, 222, text=f"chatter {'x' * i}") for i in range(1, 6)]
        with mock.patch.object(listen, "CHATLOG_LINES", 3):
            listen.append_chatlog(listen.chatlog_entries({"ok": True, "result": FakeTelegram.updates},
                                                         {PLANNING}))
        p = Path(self.vars["SPLITBOT_HOME"]) / "chatlog" / f"{PLANNING}.jsonl"
        self.assertEqual(len(p.read_text().splitlines()), 3)     # rolling: last N lines only
        r = self.run_py("listen.py", "3")
        self.assertEqual(r.returncode, 3, r.stderr)              # deadline, nothing emitted
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(p.read_text().splitlines()), 8)     # 3 + 5 (limit 400)


if __name__ == "__main__":
    unittest.main()
