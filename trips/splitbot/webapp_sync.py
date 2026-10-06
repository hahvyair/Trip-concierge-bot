#!/usr/bin/env python3
"""Push trip snapshots to the Telegram trip app (Cloudflare Worker + KV).

The repo stays the record. After each push of trips/data, the bot session runs
this to send one JSON snapshot per trip to <url>/api/sync, signed with
HMAC_SHA256(key = HMAC_SHA256(key="TripSync", msg=bot token), msg = body), so
no extra secret is needed. The app (trips/webapp/) only shows these snapshots.

A snapshot holds the trip basics, members (name and Telegram id only), the
active ledger rows the app shows, balances, the settle-up plan, spending by day
and category, the plan (if any) and the Wrapped stats (if anything was spent).
Never the chat log, receipts, usernames, who logged what, or anything outside
trips/data.

Usage (from the repo root):
  webapp_sync.py                     open trips + trips closed in the last 7 days;
                                     removes trips closed longer ago from the app
  webapp_sync.py --trip ID           one trip
  webapp_sync.py --dry-run           build and print sizes only (no config or token needed)
  webapp_sync.py --delete ID         remove a trip from the app
  webapp_sync.py --link --chat ID    the app link for the group's trip (or --trip ID)
  webapp_sync.py --set-short-name NAME   record the BotFather app short name
  webapp_sync.py --ack ID[,ID...]    tell the Worker these app changes were handled
                                     (applied or refused), so it stops listing them

Changes asked for in the app (rename a member, add a member) wait in the
Worker's queue. listen.py collects them (fetch_actions) and emits them as
"app_action" events; the session applies them with splitlog.py and acks them
here. Acked ids are also kept locally for a day (~/.splitbot/app_acked.json),
so a stale Worker read can't make the bot apply one twice.

Not configured (no trips/webapp/config.json, or no bot token): prints one line
and exits 0, so it is safe to run after every push.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import report as rp  # noqa: E402
import send  # noqa: E402
import splitlog as sl  # noqa: E402
import wrapped as wr  # noqa: E402

CONFIG = sl.ROOT / "trips" / "webapp" / "config.json"
RECENT_DAYS = 7      # a closed trip stays in the app this long
PURGE_DAYS = 60      # ...and its removal is re-sent for this long, in case a sync was missed
SHORT_NAME = re.compile(r"^[A-Za-z0-9_]{3,30}$")
ACTION_ID = re.compile(r"^[0-9]{13}-[0-9a-f]{8}$")
ACKED_KEEP = 86400  # seconds an acked id is remembered locally


# --------------------------------------------------------------------- signing
def sync_key(token: str) -> bytes:
    return hmac.new(b"TripSync", token.encode(), hashlib.sha256).digest()


def signature(token: str, body: bytes) -> str:
    return hmac.new(sync_key(token), body, hashlib.sha256).hexdigest()


def encode(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# -------------------------------------------------------------------- snapshot
def row_view(r: dict) -> dict:
    return {"entry": int(r["entry"]), "date": r["date"], "kind": r["kind"], "payer": r["payer"],
            "desc": r["desc"], "category": r.get("category") or "other",
            "amount": float(r["amount"]), "currency": r["currency"],
            "local": sl.money(float(r["amount"]), r["currency"]), "sgd": rp.cents(r),
            "split_mode": r["split_mode"], "shares": sl.read_shares(r["shares_sgd"])}


def plan_view(trip: dict) -> dict | None:
    p = sl.data_dir() / "plans" / f"{trip['id']}.json"
    if not p.exists():
        return None
    import plan as pl
    plan = pl.load_plan(trip)
    polls = []
    for poll in plan["polls"]:
        polls.append({k: poll.get(k) for k in ("n", "question", "options", "multi", "topic", "status",
                                               "outcome", "opened", "closed")}
                     | {"tally": pl.tally(poll), "votes": poll.get("votes", {}),
                        "not_voted": pl.not_voted(trip, poll)})
    return {"destinations": plan["destinations"], "budget_pp_sgd": plan["budget_pp_sgd"],
            "notes": plan["notes"], "decisions": [d for d in plan["decisions"] if not d.get("superseded")],
            "polls": polls, "ideas": plan["ideas"], "todos": plan["todos"], "bookings": plan["bookings"],
            "itinerary": plan["itinerary"]}


def wrapped_view(trip: dict) -> dict | None:
    if not rp.expenses(trip):
        return None
    s = wr.stats(trip)
    top_cat = s["cats"][0][0]
    vibe, tag = wr.VIBE.get(top_cat, wr.VIBE["other"])
    peak = s["peak"]
    peak_rows = sorted((r for r in s["rows"] if r["date"] == peak), key=lambda r: -rp.cents(r))[:3]
    n_days = len(s["days"])
    return {
        "first": s["days"][0], "last": s["days"][-1], "n_days": n_days, "n_payments": len(s["rows"]),
        "total": s["total"], "per_day": round(s["total"] / max(n_days, 1)), "per_head": s["per_head"],
        "local_line": wr.local_line(trip, s), "ride_avg": s["ride_avg"],
        "rides": s["total"] // s["ride_avg"] if s["ride_avg"] else 0,
        "cats": [[c, v, wr.pct(v, s["total"])] for c, v in s["cats"]],
        "top_cat": top_cat, "vibe": vibe, "vibe_line": tag.format(trip=trip["name"]),
        "top": [{"desc": r["desc"], "payer": r["payer"], "date": r["date"], "sgd": rp.cents(r)}
                for r in s["top"]],
        "days": [[d, s["per_day"][d]] for d in s["days"]],
        "peak": peak, "peak_sgd": s["per_day"][peak], "peak_pct": wr.pct(s["per_day"][peak], s["total"]),
        "peak_highlights": [r["desc"] for r in peak_rows],
        "awards": s["awards"], "settle": [list(x) for x in s["plan"]],
        "bank": max(s["nets"].items(), key=lambda kv: kv[1]["paid"])[0],
    }


def places_view(trip: dict) -> list[dict]:
    """The bot's recent place suggestions, newest first (venues only; never people's positions)."""
    p = sl.data_dir() / "places" / f"{trip['id']}.json"
    try:
        sets = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    keep = ("name", "area", "why", "price", "hours_today", "km", "walk_km", "walk_min", "ride_min", "maps", "lat", "lon")
    return [{"at": s.get("at"), "asked_by": s.get("asked_by"), "request": s.get("request"), "near": s.get("near"),
             "places": [{k: pl.get(k) for k in keep} for pl in s.get("places", [])]}
            for s in reversed(sets[-15:])]


def snapshot(trip: dict) -> dict:
    live = sl.trip_rows(trip)
    exp = [r for r in live if r["kind"] == "expense"]
    ph = sl.phase(trip)
    # A trip in planning has only deposits: chart the days they were paid, not every day since /plan.
    days = wr.trip_span(trip, exp) if ph == "planning" else rp.trip_days(trip, exp)
    spent = sum(rp.cents(r) for r in exp)
    nets = sl.nets(trip)
    cat_sgd, cat_n = Counter(), Counter()
    for r in exp:
        cat_sgd[r.get("category") or "other"] += rp.cents(r)
        cat_n[r.get("category") or "other"] += 1
    n_days = len(rp.trip_days(trip, exp)) if exp else 0
    return {
        "v": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trip": {"id": trip["id"], "name": trip["name"], "currency": trip["currency"],
                 "tz": str(sl.trip_tz(trip)), "status": trip["status"], "phase": ph,
                 "created": trip.get("created"), "closed": trip.get("closed"),
                 "start": trip.get("start"), "end": trip.get("end"), "window": trip.get("window"),
                 "when": sl.when_text(trip)},
        "members": [{"name": m["name"], "tg_id": str(m["tg_id"]) if m.get("tg_id") else None}
                    for m in trip["members"]],
        "rows": [row_view(r) for r in sorted(live, key=lambda r: int(r["entry"]))],
        "totals": {"spent": spent, "payments": len(exp), "days": n_days,
                   "per_day": round(spent / n_days) if n_days else 0,
                   "per_head": round(spent / max(len(trip["members"]), 1))},
        "nets": nets,
        "settle": [list(x) for x in sl.settle_plan({k: v["net"] for k, v in nets.items()})],
        "days": days,
        "by_day_cat": {d: dict(v) for d, v in rp.by_day_cat(exp).items()},
        "cat_totals": [[c, v, cat_n[c]] for c, v in cat_sgd.most_common()],
        "rate_note": rp.rate_note(trip),
        "plan": plan_view(trip),
        "wrapped": wrapped_view(trip),
        "places": places_view(trip),
    }


def pick_trips(trip_id: str | None, today: date | None = None) -> list[dict]:
    trips = sl.load_trips()
    if trip_id:
        hit = [t for t in trips if t["id"] == trip_id]
        if not hit:
            raise sl.SplitError(f"No trip with id {trip_id}.")
        return hit
    cutoff = ((today or datetime.now(sl.SGT).date()) - timedelta(days=RECENT_DAYS)).isoformat()
    return [t for t in trips if t["status"] == "open" or (t.get("closed") or "") >= cutoff]


def expired_trips(today: date | None = None) -> list[dict]:
    """Trips closed more than RECENT_DAYS ago (within PURGE_DAYS): to remove from the app."""
    today = today or datetime.now(sl.SGT).date()
    lo = (today - timedelta(days=PURGE_DAYS)).isoformat()
    hi = (today - timedelta(days=RECENT_DAYS)).isoformat()
    return [t for t in sl.load_trips() if t["status"] == "closed" and lo <= (t.get("closed") or "") < hi]


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def delete_body(trip_id: str) -> bytes:
    return encode({"delete": trip_id, "ts": now_ts()})


# ---------------------------------------------------------------------- config
def config_path() -> Path:
    return Path(os.environ.get("SPLITBOT_APP_CONFIG") or CONFIG)


def load_config() -> dict | None:
    p = config_path()
    return json.loads(p.read_text()) if p.exists() else None


def post(url: str, body: bytes, sig: str, path: str = "/api/sync", max_time: int = 30) -> tuple[int, str]:
    r = subprocess.run(["curl", "-sS", "--max-time", str(max_time), "-X", "POST",
                        "-H", "Content-Type: application/json", "-H", f"X-Sync-Signature: {sig}",
                        "--data-binary", "@-", "-w", "\n%{http_code}", url.rstrip("/") + path],
                       input=body, capture_output=True)
    out = r.stdout.decode("utf-8", "replace")
    text, _, code = out.rpartition("\n")
    return (int(code) if code.isdigit() else 0), (text or r.stderr.decode("utf-8", "replace")).strip()


# ----------------------------------------------------------- app change queue
def fetch_actions(cfg: dict, token: str, max_time: int = 8) -> list[dict]:
    """The changes waiting in the Worker's queue. Raises RuntimeError on any failure."""
    body = encode({"op": "list", "ts": now_ts()})
    code, text = post(cfg["url"], body, signature(token, body), "/api/actions", max_time)
    if code != 200:
        raise RuntimeError(f"HTTP {code}")
    try:
        actions = json.loads(text)["actions"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise RuntimeError("bad reply") from None
    if not isinstance(actions, list):
        raise RuntimeError("bad reply")
    return [a for a in actions if isinstance(a, dict) and ACTION_ID.match(str(a.get("id", "")))]


def acked_path() -> Path:
    return Path(os.environ.get("SPLITBOT_HOME", Path.home() / ".splitbot")) / "app_acked.json"


def acked() -> dict[str, float]:
    """Locally remembered acks: action id -> when it was acked (unix time)."""
    try:
        got = json.loads(acked_path().read_text())
        return {str(k): float(v) for k, v in got.items()} if isinstance(got, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def remember_acked(ids: list[str]) -> None:
    now = time.time()
    keep = {k: v for k, v in acked().items() if now - v < ACKED_KEEP}
    keep.update({i: now for i in ids})
    p = acked_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep))
    tmp.replace(p)


def ack(cfg: dict, token: str, ids: list[str], max_time: int = 20) -> tuple[int, str]:
    """Remember the ids locally, then tell the Worker. Returns (HTTP code, text)."""
    remember_acked(ids)
    body = encode({"op": "ack", "ids": ids, "ts": now_ts()})
    return post(cfg["url"], body, signature(token, body), "/api/actions/ack", max_time)


def app_link(cfg: dict, trip: dict) -> str:
    if not cfg.get("app_short_name"):
        raise sl.SplitError("The app has no short name yet: the owner creates it with BotFather /newapp, "
                            "then run webapp_sync.py --set-short-name NAME.")
    bot = cfg.get("bot_username")
    if not bot:
        bot = (send.call("getMe", {}).get("result") or {}).get("username")
    if not bot:
        raise sl.SplitError("Couldn't get the bot's username from Telegram.")
    return f"https://t.me/{bot}/{cfg['app_short_name']}?startapp={trip['id']}"


# --------------------------------------------------------------------- command
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trip", help="trip id (default: open trips + those closed in the last 7 days)")
    ap.add_argument("--chat", help="with --link: the group whose trip to link")
    ap.add_argument("--dry-run", action="store_true", help="build the snapshots and print sizes only")
    ap.add_argument("--delete", metavar="ID", help="remove this trip from the app")
    ap.add_argument("--link", action="store_true", help="print the app link for a trip (--trip or --chat)")
    ap.add_argument("--set-short-name", metavar="NAME", help="record the BotFather app short name")
    ap.add_argument("--ack", metavar="ID[,ID]", help="mark app changes (app_action events) as handled")
    a = ap.parse_args(argv)
    try:
        if a.ack is not None:
            ids = [x.strip() for x in a.ack.split(",") if x.strip()]
            if not ids or not all(ACTION_ID.match(x) for x in ids):
                raise sl.SplitError("--ack takes action ids from app_action events, comma-separated.")
            cfg, token = load_config(), send.token()
            if not cfg or not cfg.get("url") or not token:
                remember_acked(ids)
                print("webapp_sync: app not configured or no bot token; ack kept locally only")
                return 0
            code, text = ack(cfg, token, ids)
            if code == 200:
                print(f"webapp_sync: acked {len(ids)} app change(s)")
                return 0
            print(f"webapp_sync: ack failed: HTTP {code} {text[:200]} (kept locally; the listener retries it)")
            return 1
        if a.set_short_name:
            if not SHORT_NAME.match(a.set_short_name):
                raise sl.SplitError("A short name is 3-30 letters, digits or underscores.")
            cfg = load_config() or {}
            cfg["app_short_name"] = a.set_short_name
            config_path().parent.mkdir(parents=True, exist_ok=True)
            config_path().write_text(json.dumps(cfg, indent=1) + "\n")
            print(f"webapp: short name set to {a.set_short_name}. Commit trips/webapp/config.json.")
            return 0
        if a.link:
            cfg = load_config()
            if not cfg:
                print("webapp: not set up yet (no trips/webapp/config.json)")
                return 1
            if a.chat:
                trip = rp.latest_trip(a.chat, sl.load_trips())
            elif a.trip:
                trip = pick_trips(a.trip)[0]
            else:
                raise sl.SplitError("--link needs --chat or --trip.")
            print(app_link(cfg, trip))
            return 0

        if a.dry_run:
            for t in pick_trips(a.trip):
                print(f"{t['id']}: {len(encode(snapshot(t))) / 1024:.1f} KB (dry run, not sent)")
            return 0

        cfg, token = load_config(), send.token()
        if not cfg or not cfg.get("url"):
            print("webapp_sync: not configured (no trips/webapp/config.json); nothing sent")
            return 0
        if not token:
            print("webapp_sync: no bot token (SPLIT_BOT_TOKEN); nothing sent")
            return 0

        if a.delete:
            body = delete_body(a.delete)
            code, text = post(cfg["url"], body, signature(token, body))
            print(f"webapp_sync: deleted {a.delete}" if code == 200
                  else f"webapp_sync: delete {a.delete} failed: HTTP {code} {text[:200]}")
            return 0 if code == 200 else 1

        failed = 0
        trips = pick_trips(a.trip)
        for t in trips:
            body = encode(snapshot(t))
            code, text = post(cfg["url"], body, signature(token, body))
            if code == 200:
                print(f"webapp_sync: {t['id']} synced ({len(body) / 1024:.1f} KB)")
            else:
                failed += 1
                print(f"webapp_sync: {t['id']} failed: HTTP {code} {text[:200]}")
        if not trips:
            print("webapp_sync: no open or recent trips")
        if not a.trip:
            for t in expired_trips():
                body = delete_body(t["id"])
                code, _ = post(cfg["url"], body, signature(token, body))
                if code == 200:
                    print(f"webapp_sync: {t['id']} removed (closed {t['closed']})")
        return 1 if failed else 0
    except sl.SplitError as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
