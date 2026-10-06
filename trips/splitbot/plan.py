#!/usr/bin/env python3
"""Trip planning board: one source of truth for what the group has decided.

Each trip being planned has a plan file, trips/data/plans/<trip_id>.json
(SPLIT_DATA overrides the data folder), holding the basics (destinations,
budget, notes), the decisions log, polls and their votes, the ideas board,
to-dos, the bookings checklist and the itinerary. Dates live on the trip
itself (trips.json; splitlog.py trip dates). `--chat <group id>` picks the
group's open trip, as in splitlog.py. Output is short plain text to relay.

Never store booking references, passport/ID numbers, card or payment details,
or addresses beyond a venue name: text that looks like one is refused.

Usage (from the repo root):
  plan.py show --chat ID                         where are we: the summary
  plan.py set --chat ID [--destination Osaka,Kyoto] [--budget 2500] [--note ..] [--start/--end/--window]
  plan.py decide --chat ID --topic Dates --outcome "12-19 Dec" [--how "agreed in chat"] [--by Ana,Ben]
  plan.py idea add --chat ID --text "Ryokan in Kinosaki" --by Ben [--link URL] [--kind stay]
                       [--why "onsen town, 2h from Kyoto"] [--price mid] [--area Kinosaki]
  plan.py idea update --chat ID --n 3 --status shortlisted|dropped|chosen|idea
  plan.py todo add --chat ID --task "Check visa rules" --owner Ben [--due 2026-10-20]
  plan.py todo done --chat ID --n 2
  plan.py booking add --chat ID --item "Flights SIN-KIX" --kind flights [--who Ben] [--status todo]
                          [--est 650] [--deadline 2026-10-31] [--link URL]
  plan.py booking update --chat ID --n 1 [--status held|booked|todo] [--who ..] [--est ..] [--deadline ..]
  plan.py itinerary add --chat ID --day 2026-12-13 --what "Fushimi Inari" [--time 08:00] [--where Kyoto] [--booked]
                       [--note "go early, before the crowds"] [--link URL] [--cost 25]
  plan.py itinerary import --chat ID [--replace] < items.jsonl
      bulk add (a drafted itinerary): JSON lines {day, time, what, where, note, link, cost_sgd, booked};
      --replace clears the days being imported first
  plan.py itinerary remove --chat ID --day 2026-12-13 --n 2      plan.py itinerary show --chat ID
  plan.py poll new --chat ID --question "Where to stay?" --option Namba --option Umeda [--multi] [--topic Stay]
  plan.py poll vote --chat ID --poll-id ID --user-id TGID --name "Ben Tan" --options 0,2   (empty = retracted)
  plan.py poll close --chat ID --n 2 [--no-stop]
  plan.py due [--chat ID] [--send] [--at 10:00]  reminders: to-dos and bookings due within 2 days or overdue
  plan.py chatlog --chat ID [--n 150]            the group's recent messages (planning phase; local, not committed)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import splitlog as sl  # noqa: E402

PlanError = sl.SplitError
IDEA_STATUS = ["idea", "shortlisted", "dropped", "chosen"]
BOOKING_STATUS = ["todo", "held", "booked"]
BOOKING_KINDS = ["flights", "stay", "transport", "activity", "other"]
IDEA_KINDS = ["destination", "stay", "food", "activity", "transport", "other"]
SPECIAL = {"all", "everyone", "group", "bot"}
DUE_DAYS = 2
# Things that must never be stored: card numbers, passport/NRIC-style ids, booking refs.
SENSITIVE = [re.compile(r"\b(?:\d[ -]?){13,19}\b"),
             re.compile(r"\b[A-Z]{1,2}\d{6,9}[A-Z]?\b"),
             re.compile(r"\b(?:booking|confirmation|conf|reservation|pnr|ref(?:erence)?|passport|nric|"
                        r"cvv|card)\s*(?:no\.?|number|num|code|#)?\s*[:#]?\s*(?=[A-Z0-9-]*\d)[A-Z0-9-]{5,}\b", re.I)]


# --------------------------------------------------------------------- storage
def plan_path(trip: dict) -> Path:
    d = sl.data_dir() / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{trip['id']}.json"


def load_plan(trip: dict) -> dict:
    p = plan_path(trip)
    plan = json.loads(p.read_text()) if p.exists() else {}
    base = {"trip_id": trip["id"], "destinations": [], "budget_pp_sgd": None, "notes": [],
            "decisions": [], "polls": [], "ideas": [], "todos": [], "bookings": [],
            "itinerary": {}, "reminded": {}}
    return {**base, **plan}


def save_plan(trip: dict, plan: dict) -> None:
    p = plan_path(trip)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(p)
    sl.sync_app()


def nxt(items: list[dict]) -> int:
    return 1 + max([i["n"] for i in items] or [0])


def get(items: list[dict], n: int, what: str) -> dict:
    for i in items:
        if i["n"] == n:
            return i
    raise PlanError(f"There's no {what} #{n}.")


# ------------------------------------------------------------------ validation
def clean(text: str | None) -> str | None:
    """Refuse anything that looks like a booking ref, ID or card number."""
    if text is None:
        return None
    text = text.strip()
    if any(rx.search(text) for rx in SENSITIVE):
        raise PlanError("That looks like a booking reference, ID or card number. "
                        "The plan never stores those; keep them in your own email/app.")
    return text


def iso(x: str | None) -> str | None:
    if not x:
        return None
    try:
        return date.fromisoformat(x).isoformat()
    except ValueError:
        raise PlanError(f"'{x}' isn't a date; use YYYY-MM-DD.") from None


def who(trip: dict, name: str | None) -> str | None:
    """A member's name (or all/group/bot)."""
    if not name:
        return None
    if name.strip().lower() in SPECIAL:
        return name.strip().lower()
    return sl.find_member(trip, name)["name"]


def today(trip: dict) -> str:
    return sl.trip_today(trip)


# ------------------------------------------------------------------- formatting
def d_short(x: str) -> str:
    v = date.fromisoformat(x)
    return f"{v.day} {v:%b}"


def due_text(x: str | None, now: str) -> str:
    if not x:
        return ""
    days = (date.fromisoformat(x) - date.fromisoformat(now)).days
    rel = ("overdue" if days < 0 else "today" if days == 0 else "tomorrow" if days == 1
           else f"in {days} days")
    return f"due {d_short(x)}, {rel}"


def tally(poll: dict) -> list[int]:
    counts = [0] * len(poll["options"])
    for ids in poll["votes"].values():
        for i in ids:
            if 0 <= i < len(counts):
                counts[i] += 1
    return counts


def tally_text(poll: dict) -> str:
    c = tally(poll)
    order = sorted(range(len(c)), key=lambda i: (-c[i], i))
    return " · ".join(f"{poll['options'][i]} {c[i]}" for i in order)


def not_voted(trip: dict, poll: dict) -> list[str]:
    return [m["name"] for m in trip["members"] if m["name"] not in poll["votes"]]


def leaders(poll: dict) -> tuple[list[str], int]:
    c = tally(poll)
    top = max(c or [0])
    return ([poll["options"][i] for i, v in enumerate(c) if v == top] if top else []), top


def show_text(trip: dict, plan: dict) -> str:
    now = today(trip)
    ph = sl.phase(trip)
    lines = [f"{trip['name']} · {ph}" + (f", {(date.fromisoformat(trip['start']) - date.fromisoformat(now)).days}"
                                          " days to go" if ph == "planning" and trip.get("start") else "")]
    basics = [f"When: {sl.when_text(trip)}"]
    if plan["destinations"]:
        basics.append("Where: " + ", ".join(plan["destinations"]))
    if plan["budget_pp_sgd"]:
        basics.append(f"Budget: {sl.sgd(round(plan['budget_pp_sgd'] * 100))} pp")
    lines.append(" · ".join(basics))
    lines.append("Who: " + (", ".join(m["name"] for m in trip["members"]) or "nobody yet"))
    for n in plan["notes"][-3:]:
        lines.append(f"Note: {n}")
    head = len(lines)

    live = [d for d in plan["decisions"] if not d.get("superseded")]
    if live:
        lines.append("Decided:")
        lines += [f"• {d['topic']}: {d['outcome']} ({d['how']}, {d_short(d['date'])})" for d in live]
    polls = [p for p in plan["polls"] if p["status"] == "open"]
    if polls:
        lines.append("Open polls:")
        for p in polls:
            nv = not_voted(trip, p)
            lines.append(f"• #{p['n']} {p['question']}: {tally_text(p)}"
                         + (f"; not voted: {', '.join(nv)}" if nv else "; everyone voted"))
    short = [i for i in plan["ideas"] if i["status"] == "shortlisted"]
    if short:
        lines.append("Shortlist: " + "; ".join(f"#{i['n']} {i['text']}" for i in short))
    other = [i for i in plan["ideas"] if i["status"] == "idea"]
    if other:
        lines.append(f"Ideas: {len(other)} on the board (" + "; ".join(f"#{i['n']} {i['text']}"
                                                                       for i in other[:5])
                     + ("; ..." if len(other) > 5 else "") + ")")
    todos = [t for t in plan["todos"] if t["status"] != "done"]
    if todos:
        lines.append("To-dos:")
        by: dict[str, list[dict]] = {}
        for t in todos:
            by.setdefault(t["owner"] or "unassigned", []).append(t)
        for owner, items in by.items():
            lines.append(f"• {owner}: " + "; ".join(
                f"#{t['n']} {t['task']}" + (f" ({due_text(t['due'], now)})" if t.get("due") else "")
                for t in items))
    if plan["bookings"]:
        counts = {s: sum(b["status"] == s for b in plan["bookings"]) for s in BOOKING_STATUS}
        lines.append("Bookings: " + ", ".join(f"{v} {k.replace('todo', 'to do')}"
                                              for k, v in counts.items() if v) + ":")
        for b in plan["bookings"]:
            extra = [b["who"] or "nobody yet"]
            if b.get("est_sgd"):
                extra.append(f"~{sl.sgd(round(b['est_sgd'] * 100))}")
            if b.get("deadline") and b["status"] != "booked":
                extra.append(due_text(b["deadline"], now))
            lines.append(f"• #{b['n']} {b['item']}: {b['status']} ({', '.join(extra)})")
        est = sum(b.get("est_sgd") or 0 for b in plan["bookings"])
        if est:
            lines.append(f"Estimated bookings: {sl.sgd(round(est * 100))} in total")
    if plan["itinerary"]:
        lines.append(f"Itinerary: {len(plan['itinerary'])} day(s) with plans (itinerary show)")
    paid = sum(round(float(r["amount_sgd"]) * 100) for r in sl.trip_rows(trip) if r["kind"] == "expense")
    if paid:
        lines.append(f"Paid so far (deposits etc.): {sl.sgd(paid)}; see balances")
    if len(lines) == head and ph == "planning":
        lines.append("Nothing decided yet. Suggest a poll for the dates or the destination.")
    return "\n".join(lines)


# --------------------------------------------------------------------- telegram
def send_py(*args: str, text: str | None = None) -> str:
    r = subprocess.run([sys.executable, str(HERE / "send.py"), *args], input=text,
                       capture_output=True, text=True)
    if r.returncode:
        raise PlanError(f"Telegram didn't take it: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


# ---------------------------------------------------------------------- commands
def cmd_show(a, trip, plan) -> bool:
    print(show_text(trip, plan))
    return False


def cmd_set(a, trip, plan) -> bool:
    changed = []
    if a.destination:
        plan["destinations"] = [clean(x) for x in a.destination.split(",") if x.strip()]
        changed.append("destinations")
    if a.budget is not None:
        if a.budget <= 0:
            raise PlanError("The budget must be positive (SGD per person).")
        plan["budget_pp_sgd"] = a.budget
        changed.append("budget")
    if a.note:
        plan["notes"].append(clean(a.note))
        changed.append("note")
    if a.clear_notes:
        plan["notes"] = []
        changed.append("notes cleared")
    if a.start or a.end or a.window:
        trips = sl.load_trips()
        t = sl.open_trip(trip["chat_id"], trips)
        sl.set_dates(t, iso(a.start), iso(a.end), a.window)
        sl.save_trips(trips)
        trip.update(t)
        changed.append("dates")
    if not changed:
        raise PlanError("Nothing to set: give --destination, --budget, --note or dates.")
    print(f"{trip['name']}: updated {', '.join(changed)}.")
    print(show_text(trip, plan).split("\n")[1])
    return True


def record_decision(trip, plan, topic: str, outcome: str, how: str, by: list[str]) -> dict:
    for d in plan["decisions"]:
        if d["topic"].lower() == topic.lower() and not d.get("superseded"):
            d["superseded"], topic = True, d["topic"]  # keep the topic's first wording
    d = {"n": nxt(plan["decisions"]), "topic": topic, "outcome": outcome, "how": how,
         "date": today(trip), "confirmed_by": by}
    plan["decisions"].append(d)
    return d


def cmd_decide(a, trip, plan) -> bool:
    by = [who(trip, x) for x in (a.by or "").split(",") if x.strip()]
    old = [d for d in plan["decisions"] if d["topic"].lower() == a.topic.strip().lower() and not d.get("superseded")]
    d = record_decision(trip, plan, clean(a.topic), clean(a.outcome), clean(a.how), by)
    print(f"Decided: {d['topic']}: {d['outcome']} ({d['how']})"
          + (f"; replaces '{old[0]['outcome']}'" if old else ""))
    return True


def cmd_idea(a, trip, plan) -> bool:
    if a.action == "add":
        if not a.text:
            raise PlanError("idea add needs --text.")
        i = {"n": nxt(plan["ideas"]), "text": clean(a.text), "kind": a.kind or "other",
             "by": who(trip, a.by) or "bot", "link": a.link, "status": a.status or "idea",
             "added": today(trip), "why": clean(a.why), "price": a.price, "area": clean(a.area)}
        plan["ideas"].append(i)
        print(f"Idea #{i['n']} added: {i['text']}")
        return True
    i = get(plan["ideas"], a.n, "idea")
    if a.text:
        i["text"] = clean(a.text)
    if a.link:
        i["link"] = a.link
    if a.status:
        i["status"] = a.status
    for k in ("why", "area"):
        if getattr(a, k):
            i[k] = clean(getattr(a, k))
    if a.price:
        i["price"] = a.price
    if a.kind:
        i["kind"] = a.kind
    print(f"Idea #{i['n']} {i['text']}: {i['status']}")
    return True


def cmd_todo(a, trip, plan) -> bool:
    if a.action == "add":
        if not a.task:
            raise PlanError("todo add needs --task.")
        t = {"n": nxt(plan["todos"]), "task": clean(a.task), "owner": who(trip, a.owner),
             "due": iso(a.due), "status": "open", "added": today(trip)}
        plan["todos"].append(t)
        print(f"To-do #{t['n']}: {t['task']}" + (f", {t['owner']}" if t["owner"] else "")
              + (f", {due_text(t['due'], today(trip))}" if t["due"] else ""))
        return True
    t = get(plan["todos"], a.n, "to-do")
    if a.action == "done":
        t["status"], t["done"] = "done", today(trip)
        print(f"Done: #{t['n']} {t['task']}")
    else:  # update
        if a.task:
            t["task"] = clean(a.task)
        if a.owner:
            t["owner"] = who(trip, a.owner)
        if a.due:
            t["due"] = iso(a.due)
        print(f"To-do #{t['n']}: {t['task']}, {t['owner'] or 'unassigned'}"
              + (f", {due_text(t['due'], today(trip))}" if t.get("due") else ""))
    return True


def cmd_booking(a, trip, plan) -> bool:
    if a.action == "add":
        if not a.item:
            raise PlanError("booking add needs --item.")
        b = {"n": nxt(plan["bookings"]), "item": clean(a.item), "kind": a.kind or "other",
             "who": who(trip, a.who), "status": a.status or "todo", "est_sgd": a.est,
             "deadline": iso(a.deadline), "link": a.link}
        plan["bookings"].append(b)
    else:
        b = get(plan["bookings"], a.n, "booking")
        if a.item:
            b["item"] = clean(a.item)
        if a.kind:
            b["kind"] = a.kind
        if a.who:
            b["who"] = who(trip, a.who)
        if a.status:
            b["status"] = a.status
        if a.est is not None:
            b["est_sgd"] = a.est
        if a.deadline:
            b["deadline"] = iso(a.deadline)
        if a.link:
            b["link"] = a.link
    est = f", ~{sl.sgd(round(b['est_sgd'] * 100))}" if b.get("est_sgd") else ""
    print(f"Booking #{b['n']} {b['item']}: {b['status']} ({b['who'] or 'nobody yet'}{est})")
    return True


def day_key(x: str) -> str:
    try:
        return date.fromisoformat(x).isoformat()
    except ValueError:
        return x.strip()  # "Day 2" before the dates are fixed


def itinerary_text(plan: dict, day: str | None = None) -> str:
    days = [day] if day else sorted(plan["itinerary"])
    out = []
    for d in days:
        items = plan["itinerary"].get(d) or []
        label = d_short(d) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else d
        out.append(label + ":" + ("" if items else " nothing yet"))
        out += [f"  {k}. " + " ".join(x for x in (it.get("time"), it["what"]) if x)
                + (f" @ {it['where']}" if it.get("where") else "") + (" (booked)" if it.get("booked") else "")
                for k, it in enumerate(items, 1)]
    return "\n".join(out) or "Itinerary: nothing yet."


def item(time_, what, where=None, booked=False, note=None, link=None, cost=None) -> dict:
    it = {"time": time_ or None, "what": clean(what), "where": clean(where), "booked": bool(booked)}
    if note:
        it["note"] = clean(note)
    if link:
        if not re.match(r"^https?://", str(link)):
            raise PlanError(f"'{link}' isn't a web link.")
        it["link"] = str(link)
    if cost not in (None, ""):
        it["cost_sgd"] = float(cost)
    return it


def itinerary_import(plan: dict, text: str, replace: bool) -> bool:
    """Bulk-add itinerary items from JSON lines; all or nothing."""
    rows = []
    for n, raw in enumerate(text.splitlines(), 1):
        raw = raw.strip().rstrip(",")
        if not raw or not raw.startswith("{"):
            continue
        try:
            r = json.loads(raw)
        except json.JSONDecodeError:
            raise PlanError(f"Line {n} isn't valid JSON.") from None
        if not r.get("day") or not r.get("what"):
            raise PlanError(f"Line {n} needs a day and a what.")
        rows.append((day_key(str(r["day"])), item(r.get("time"), r["what"], r.get("where"), r.get("booked"),
                                                   r.get("note"), r.get("link"), r.get("cost_sgd"))))
    if not rows:
        raise PlanError("Nothing to import: send JSON lines with day and what.")
    if replace:
        for d in {d for d, _ in rows}:
            plan["itinerary"].pop(d, None)
    for d, it in rows:
        plan["itinerary"].setdefault(d, []).append(it)
    for d in {d for d, _ in rows}:
        plan["itinerary"][d].sort(key=lambda it: it.get("time") or "99:99")
    print(f"Itinerary: {len(rows)} items over {len({d for d, _ in rows})} days.")
    print(itinerary_text(plan))
    return True


def cmd_itinerary(a, trip, plan) -> bool:
    if a.action == "show":
        print(itinerary_text(plan, day_key(a.day) if a.day else None))
        return False
    if a.action == "import":
        return itinerary_import(plan, sys.stdin.read(), a.replace)
    if not a.day:
        raise PlanError(f"itinerary {a.action} needs --day.")
    d = day_key(a.day)
    items = plan["itinerary"].setdefault(d, [])
    if a.action == "add":
        if not a.what:
            raise PlanError("itinerary add needs --what.")
        items.append(item(a.time, a.what, a.where, a.booked, a.note, a.link, a.cost))
        items.sort(key=lambda it: it.get("time") or "99:99")
    else:
        if not a.n or not 1 <= a.n <= len(items):
            raise PlanError(f"There's no item {a.n} on {a.day}.")
        items.pop(a.n - 1)
        if not items:
            del plan["itinerary"][d]
    print(itinerary_text(plan, d) if d in plan["itinerary"] else f"{a.day}: nothing planned now.")
    return True


def find_voter(trip: dict, user_id: str | None, name: str | None) -> tuple[dict | None, bool]:
    """(member, newly linked). Match the Telegram id, else an unambiguous first name."""
    for m in trip["members"]:
        if user_id and str(m.get("tg_id") or "") == str(user_id):
            return m, False
    if name:
        first = name.split()[0].lower()
        hits = [m for m in trip["members"] if not m.get("tg_id")
                and m["name"].lower() in (name.lower(), first)]
        if len(hits) == 1:
            return hits[0], True
    return None, False


def cmd_poll(a, trip, plan) -> bool:
    if a.action == "new":
        opts = [clean(o) for o in a.option if o.strip()]
        if not a.question or not 2 <= len(opts) <= 10:
            raise PlanError("A poll needs --question and 2 to 10 --option.")
        args = ["--chat", trip["chat_id"], "--poll", clean(a.question)]
        for o in opts:
            args += ["--option", o]
        if a.multi:
            args.append("--multi")
        out = send_py(*args)
        msg = re.search(r"message (\d+)", out)
        pid = re.search(r"poll_id=(\S+)", out)
        if not (msg and pid):
            raise PlanError("The poll was sent but Telegram gave no poll id.")
        p = {"n": nxt(plan["polls"]), "poll_id": pid.group(1), "message_id": int(msg.group(1)),
             "question": a.question.strip(), "options": opts, "multi": a.multi,
             "topic": clean(a.topic) or a.question.strip(), "votes": {}, "status": "open",
             "outcome": None, "opened": today(trip), "closed": None}
        plan["polls"].append(p)
        print(f"Poll #{p['n']} posted: {p['question']} ({len(opts)} options, "
              f"{'several answers allowed' if a.multi else 'one answer each'}).")
        return True

    if a.action == "vote":
        polls = [p for p in plan["polls"] if p["poll_id"] == str(a.poll_id)]
        if not polls:
            raise PlanError(f"Poll {a.poll_id} isn't in this trip's plan.")
        p = polls[0]
        if p["status"] != "open":
            print(f"Poll #{p['n']} is already closed; vote ignored.")
            return False
        m, linked = find_voter(trip, a.user_id, a.name)
        if linked:  # first time we see this friend's Telegram id: remember it
            trips = sl.load_trips()
            for t in trips:
                if t["id"] == trip["id"]:
                    for mm in t["members"]:
                        if mm["name"] == m["name"]:
                            mm["tg_id"] = str(a.user_id)
            sl.save_trips(trips)
        voter = m["name"] if m else (a.name or str(a.user_id))
        ids = [int(x) for x in (a.options or "").split(",") if x.strip() != ""]
        if any(not 0 <= i < len(p["options"]) for i in ids):
            raise PlanError("Option number out of range.")
        if ids:
            p["votes"][voter] = ids
            what = f"{voter} voted {', '.join(p['options'][i] for i in ids)}"
        else:
            p["votes"].pop(voter, None)
            what = f"{voter} retracted their vote"
        lines = [f"Poll #{p['n']} {p['question']}: {what}" + ("" if m else " (not on the trip)"),
                 f"Tally: {tally_text(p)}"]
        nv = not_voted(trip, p)
        if nv or not trip["members"]:
            lines.append(f"Waiting on: {', '.join(nv) or 'nobody listed on the trip'}")
        else:
            top, k = leaders(p)
            lead = (f"{top[0]} leads with {k}" if len(top) == 1 else
                    f"tie between {' and '.join(top)} ({k} each)")
            lines.append(f"ALL VOTED: everyone on the trip has voted; {lead}. "
                         f"Announce it and close: plan.py poll close --chat {trip['chat_id']} --n {p['n']}")
        print("\n".join(lines))
        return True

    # close
    p = get(plan["polls"], a.n, "poll")
    if p["status"] != "open":
        print(f"Poll #{p['n']} was already closed: {p['outcome']}")
        return False
    warn = ""
    if not a.no_stop:
        try:
            send_py("--chat", trip["chat_id"], "--stop-poll", str(p["message_id"]))
        except PlanError as e:  # already stopped or deleted in Telegram: still close it here
            warn = f" (Telegram: {e})"
    p["status"], p["closed"] = "closed", today(trip)
    top, k = leaders(p)
    if not top:
        p["outcome"] = "no votes"
        print(f"Poll #{p['n']} closed with no votes; nothing decided.{warn}")
    elif len(top) > 1:
        p["outcome"] = "tie: " + " / ".join(top)
        print(f"Poll #{p['n']} closed: tie between {' and '.join(top)} ({k} each). Not decided; "
              f"take it back to the group (a run-off poll, or the owner decides).{warn}")
    else:
        p["outcome"] = top[0]
        voters = [n for n, ids in p["votes"].items() if p["options"].index(top[0]) in ids]
        record_decision(trip, plan, p["topic"], top[0], f"poll #{p['n']}", voters)
        print(f"Poll #{p['n']} closed: {top[0]} ({k} of {len(p['votes'])} votes). "
              f"Recorded as decided: {p['topic']}.{warn}\nTally: {tally_text(p)}")
    return True


def due_items(trip: dict, plan: dict, now: str) -> list[tuple[str, str]]:
    """(key, line) for open to-dos and unbooked bookings due within DUE_DAYS or overdue."""
    limit = (date.fromisoformat(now) + timedelta(days=DUE_DAYS)).isoformat()
    out = []
    for t in plan["todos"]:
        if t["status"] != "done" and t.get("due") and t["due"] <= limit:
            out.append((f"todo:{t['n']}", f"• {t['owner'] or 'Someone'}: {t['task']}, "
                                          f"{due_text(t['due'], now)} (to-do #{t['n']})"))
    for b in plan["bookings"]:
        if b["status"] != "booked" and b.get("deadline") and b["deadline"] <= limit:
            out.append((f"booking:{b['n']}", f"• {b['who'] or 'Nobody yet'}: {b['item']} ({b['status']}), "
                                             f"{due_text(b['deadline'], now)} (booking #{b['n']})"))
    return out


def cmd_due(a) -> int:
    hh, mm = (int(x) for x in a.at.split(":"))
    trips = [sl.open_trip(a.chat)] if a.chat else [t for t in sl.load_trips() if t["status"] == "open"]
    trips = [t for t in trips if (sl.data_dir() / "plans" / f"{t['id']}.json").exists()]
    if not trips:
        print("due: no trip has a plan")
    for trip in trips:
        plan = load_plan(trip)
        clock = datetime.now(sl.trip_tz(trip))
        now = clock.date().isoformat()
        items = [(k, line) for k, line in due_items(trip, plan, now) if plan["reminded"].get(k) != now]
        if not items:
            print(f"{trip['name']}: nothing due")
            continue
        if not a.send:
            print(f"{trip['name']}: due\n" + "\n".join(line for _, line in items))
            continue
        if (clock.hour, clock.minute) < (hh, mm):
            print(f"{trip['name']}: {len(items)} due; reminders go out after {a.at} local")
            continue
        send_py("--chat", trip["chat_id"], text="Reminder:\n" + "\n".join(line for _, line in items)
                + "\nReply 'done #N' when it's sorted.")
        for k, _ in items:
            plan["reminded"][k] = now
        save_plan(trip, plan)
        print(f"{trip['name']}: reminder sent ({len(items)} item{'s' * (len(items) != 1)})")
    return 0


def cmd_chatlog(a, trip, plan) -> bool:
    p = Path(os.environ.get("SPLITBOT_HOME", Path.home() / ".splitbot")) / "chatlog" / f"{trip['chat_id']}.jsonl"
    if not p.exists():
        print("No chat log yet (it is kept only during the planning phase, since this run started).")
        return False
    tz = sl.trip_tz(trip)
    lines = []
    for raw in p.read_text(encoding="utf-8").splitlines()[-a.n:]:
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        when = datetime.fromtimestamp(int(e["date"]), tz).strftime("%d %b %H:%M") if e.get("date") else "?"
        lines.append(f"{when} {e['name']}: {e['text'][:300]}")
    print("\n".join(lines))
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def chat(p, required=True):
        p.add_argument("--chat", required=required, help="Telegram group chat id")

    p = sub.add_parser("show", help="where are we: the plan summary")
    chat(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("set", help="basics: destinations, budget, notes, dates")
    chat(p)
    p.add_argument("--destination", help="comma-separated, replaces the list")
    p.add_argument("--budget", type=float, help="SGD per person")
    p.add_argument("--note")
    p.add_argument("--clear-notes", action="store_true")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--window", help="a loose window, e.g. 'Dec 2026'")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("decide", help="record a decision")
    chat(p)
    p.add_argument("--topic", required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--how", default="agreed in chat")
    p.add_argument("--by", help="who confirmed, comma-separated names")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("idea", help="add, update")
    p.add_argument("action", choices=["add", "update"])
    chat(p)
    p.add_argument("--n", type=int)
    p.add_argument("--text")
    p.add_argument("--by", help="who suggested it (a member, or 'bot')")
    p.add_argument("--link")
    p.add_argument("--kind", choices=IDEA_KINDS)
    p.add_argument("--status", choices=IDEA_STATUS)
    p.add_argument("--why", help="one line on why it fits the group")
    p.add_argument("--price", choices=["budget", "mid", "high"])
    p.add_argument("--area", help="neighbourhood or town")
    p.set_defaults(func=cmd_idea)

    p = sub.add_parser("todo", help="add, done, update")
    p.add_argument("action", choices=["add", "done", "update"])
    chat(p)
    p.add_argument("--n", type=int)
    p.add_argument("--task")
    p.add_argument("--owner")
    p.add_argument("--due", help="YYYY-MM-DD")
    p.set_defaults(func=cmd_todo)

    p = sub.add_parser("booking", help="add, update (a checklist; the bot never books or pays)")
    p.add_argument("action", choices=["add", "update"])
    chat(p)
    p.add_argument("--n", type=int)
    p.add_argument("--item")
    p.add_argument("--kind", choices=BOOKING_KINDS)
    p.add_argument("--who", help="who's booking it")
    p.add_argument("--status", choices=BOOKING_STATUS)
    p.add_argument("--est", type=float, help="estimated cost, SGD (total)")
    p.add_argument("--deadline", help="YYYY-MM-DD")
    p.add_argument("--link", help="a public listing link; never a booking confirmation")
    p.set_defaults(func=cmd_booking)

    p = sub.add_parser("itinerary", help="add, remove, show")
    p.add_argument("action", choices=["add", "remove", "show", "import"])
    chat(p)
    p.add_argument("--day", help="YYYY-MM-DD, or 'Day 2' before dates are fixed")
    p.add_argument("--note")
    p.add_argument("--link")
    p.add_argument("--cost", type=float, help="estimated cost per person, SGD")
    p.add_argument("--replace", action="store_true", help="import: clear the imported days first")
    p.add_argument("--time")
    p.add_argument("--what")
    p.add_argument("--where", help="a venue or area name; never a home address")
    p.add_argument("--booked", action="store_true")
    p.add_argument("--n", type=int, help="for remove: the item number on that day")
    p.set_defaults(func=cmd_itinerary)

    p = sub.add_parser("poll", help="new, vote, close")
    p.add_argument("action", choices=["new", "vote", "close"])
    chat(p)
    p.add_argument("--question")
    p.add_argument("--option", action="append", default=[])
    p.add_argument("--multi", action="store_true", help="several answers allowed (e.g. dates)")
    p.add_argument("--topic", help="what the decision is about, for the decisions log")
    p.add_argument("--poll-id", help="vote: the event's poll_id")
    p.add_argument("--user-id", help="vote: the voter's Telegram id (from.id)")
    p.add_argument("--name", help="vote: the voter's name (from.name)")
    p.add_argument("--options", default="", help="vote: option_ids, comma-separated; empty = retracted")
    p.add_argument("--n", type=int, help="close: the poll number")
    p.add_argument("--no-stop", action="store_true", help="close: don't stop it in Telegram")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("due", help="reminders for to-dos and bookings due within 2 days or overdue")
    chat(p, required=False)
    p.add_argument("--send", action="store_true", help="post to the group (once per item per day)")
    p.add_argument("--at", default="10:00", help="local time after which reminders go out")
    p.set_defaults(func=None)

    p = sub.add_parser("chatlog", help="the group's recent messages, for 'catch us up'")
    chat(p)
    p.add_argument("--n", type=int, default=150)
    p.set_defaults(func=cmd_chatlog)

    a = ap.parse_args(argv)
    try:
        if a.cmd == "due":
            return cmd_due(a)
        if getattr(a, "action", None) in ("update", "done", "close") and a.cmd != "itinerary" and not a.n:
            raise PlanError(f"{a.cmd} {a.action} needs --n.")
        trips = sl.load_trips()
        trip = sl.open_trip(a.chat, trips)
        plan = load_plan(trip)
        if a.func(a, trip, plan):
            save_plan(trip, plan)
    except PlanError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
