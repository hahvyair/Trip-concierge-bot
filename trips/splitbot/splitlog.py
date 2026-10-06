#!/usr/bin/env python3
"""Bill-split ledger for trips. All the arithmetic lives here.

Each trip belongs to one Telegram group (one open trip per group). Payments are
logged in the currency paid, converted to SGD at that day's rate, and split in
SGD cents so the shares always add up exactly to the payment. Balances and the
settle-up plan are in SGD.

Data (in the repo, so it is versioned):
  trips/data/trips.json     trips, their group chat and members
  trips/data/expenses.csv   one row per payment or settle-up transfer
  trips/data/fx_cache.json  rates used, so a re-run gives the same answer

A row is never removed: undo and delete set status=deleted, and edit marks
the old row edited and appends the corrected one under the same entry number.

Usage (from the repo root):
  splitlog.py trip new --chat ID --name "Japan" --currency JPY --members "Ana:123:ana,Ben"
  splitlog.py add --chat ID --payer Ben --amount 8400 --currency JPY --desc taxi [--equal "Ben,Ana"]
  splitlog.py balances --chat ID      splitlog.py settle --chat ID
  splitlog.py trip new --planning --chat ID --name Japan --currency JPY --window "Dec 2026" --members ...
  splitlog.py trip dates --chat ID --start 2026-12-12 --end 2026-12-19   (planning until the start date)
  splitlog.py --help                  (and <command> --help)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SGT = timezone(timedelta(hours=8))
ZERO_DP = {"JPY", "KRW", "VND", "IDR", "CLP", "ISK", "HUF", "TWD"}
SYMBOL = {"SGD": "S$", "JPY": "¥", "USD": "US$", "EUR": "€", "GBP": "£", "THB": "฿",
          "KRW": "₩", "AUD": "A$", "HKD": "HK$", "MYR": "RM", "TWD": "NT$", "CNY": "CN¥",
          "VND": "₫", "IDR": "Rp", "PHP": "₱", "INR": "₹", "NZD": "NZ$", "CHF": "CHF "}
CATEGORIES = ["food", "drinks", "transport", "lodging", "activities", "shopping", "other"]
# Default trip timezone by local currency (dates follow the trip's clock).
TZ_BY_CCY = {"SGD": "Asia/Singapore", "JPY": "Asia/Tokyo", "VND": "Asia/Ho_Chi_Minh",
             "THB": "Asia/Bangkok", "IDR": "Asia/Jakarta", "MYR": "Asia/Kuala_Lumpur",
             "KRW": "Asia/Seoul", "TWD": "Asia/Taipei", "HKD": "Asia/Hong_Kong",
             "CNY": "Asia/Shanghai", "PHP": "Asia/Manila", "AUD": "Australia/Sydney",
             "NZD": "Pacific/Auckland", "GBP": "Europe/London", "EUR": "Europe/Paris",
             "USD": "America/New_York", "INR": "Asia/Kolkata", "CHF": "Europe/Zurich"}
COLS = ["trip_id", "entry", "update_id", "date", "kind", "payer", "desc", "category", "amount",
        "currency", "fx_to_sgd", "fx_date", "fx_source", "amount_sgd", "split_mode",
        "split", "shares_sgd", "logged_by", "source", "status", "note", "logged_at"]


class SplitError(Exception):
    """A problem the agent should relay to the group (unknown name, bad split...)."""


# --------------------------------------------------------------------- storage
def data_dir() -> Path:
    d = Path(os.environ.get("SPLIT_DATA", ROOT / "trips" / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def inbox() -> Path:
    return Path(os.environ.get("SPLITBOT_HOME", Path.home() / ".splitbot")) / "inbox"


def load_trips() -> list[dict]:
    p = data_dir() / "trips.json"
    return json.loads(p.read_text())["trips"] if p.exists() else []


APP_CONFIG = Path(__file__).resolve().parents[1] / "webapp" / "config.json"


def sync_app() -> None:
    """Refresh the Telegram app's copy of the trips in the background.

    Runs webapp_sync.py a few seconds after a save, so one command's several
    saves become one sync. Only for the real data (never when SPLIT_DATA points
    elsewhere, as in tests), only once the app is deployed, and never blocks or
    fails the save. SPLITBOT_NO_SYNC=1 turns it off.
    """
    here = Path(__file__).resolve().parent
    if (os.environ.get("SPLIT_DATA") or os.environ.get("SPLITBOT_NO_SYNC")
            or not APP_CONFIG.exists()):
        return
    home = Path(os.environ.get("SPLITBOT_HOME", Path.home() / ".splitbot"))
    try:
        home.mkdir(parents=True, exist_ok=True)
        pending = home / "sync.pending"
        if pending.exists() and time.time() - pending.stat().st_mtime > 60:
            pending.unlink(missing_ok=True)  # a queued sync that never started; don't block the next
        # One queued sync at a time: it picks up every save made before it starts.
        fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        cmd = f"sleep 3; rm -f '{pending}'; exec '{sys.executable}' '{here / 'webapp_sync.py'}'"
        with open(home / "sync.log", "a") as log:
            subprocess.Popen(["sh", "-c", cmd], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except FileExistsError:
        pass  # a sync is already queued and will include this save
    except OSError:
        pass


def save_trips(trips: list[dict]) -> None:
    p = data_dir() / "trips.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"trips": trips}, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(p)
    sync_app()


def load_rows() -> list[dict]:
    p = data_dir() / "expenses.csv"
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def save_rows(rows: list[dict]) -> None:
    p = data_dir() / "expenses.csv"
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(p)
    sync_app()


def open_trip(chat: str, trips: list[dict] | None = None) -> dict:
    trips = load_trips() if trips is None else trips
    for t in trips:
        if t["chat_id"] == str(chat) and t["status"] == "open":
            return t
    raise SplitError("No open trip in this group. The owner can start one with "
                     "/newtrip <name> <currency> <members>.")


def trip_rows(trip: dict, rows: list[dict] | None = None) -> list[dict]:
    rows = load_rows() if rows is None else rows
    return [r for r in rows if r["trip_id"] == trip["id"] and r["status"] == "active"]


# ------------------------------------------------------------------- formatting
def dp(ccy: str) -> int:
    return 0 if ccy in ZERO_DP else 2


def money(x: float, ccy: str) -> str:
    sym = SYMBOL.get(ccy, ccy + " ")
    sign = "-" if x < 0 else ""
    return f"{sign}{sym}{abs(x):,.{dp(ccy)}f}"


def sgd(cents: int) -> str:
    return money(cents / 100, "SGD")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "trip"


# ------------------------------------------------------------------------- people
def find_member(trip: dict, who: str) -> dict:
    """Match a name, @username or Telegram id, case-insensitively."""
    key = who.strip().lstrip("@").lower()
    for m in trip["members"]:
        if key in {m["name"].lower(), (m.get("username") or "").lower(), str(m.get("tg_id") or "")}:
            return m
    names = ", ".join(m["name"] for m in trip["members"])
    raise SplitError(f"'{who}' isn't on this trip. Members: {names}.")


def parse_members(spec: str) -> list[dict]:
    """'Ana:123:ana,Ben' -> name, optional Telegram id, optional username."""
    out = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        name, tg_id, user = (part.split(":") + ["", ""])[:3]
        out.append({"name": name.strip(), "tg_id": tg_id.strip() or None,
                    "username": user.strip().lstrip("@") or None})
    return out


# ---------------------------------------------------------------------------- fx
def _curl_json(url: str) -> dict:
    out = subprocess.run(["curl", "-sS", "--max-time", "20", "-A", "Mozilla/5.0", url],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def fetch_rate(ccy: str, day: str) -> tuple[float, str, str]:
    """SGD per 1 unit of ccy on day: ECB via Frankfurter, else Yahoo (latest)."""
    try:  # quoted as units per SGD and inverted: the API rounds small rates (IDR) otherwise
        d = _curl_json(f"https://api.frankfurter.dev/v1/{day}?base=SGD&symbols={ccy}")
        if d.get("rates", {}).get(ccy):
            return 1 / float(d["rates"][ccy]), d["date"], "ECB (frankfurter.dev)"
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    d = _curl_json(f"https://query1.finance.yahoo.com/v8/finance/chart/SGD{ccy}=X?range=5d&interval=1d")
    px = d["chart"]["result"][0]["meta"]["regularMarketPrice"]
    return 1 / float(px), date.today().isoformat(), "Yahoo SGD" + ccy + "=X (latest)"


def rate(ccy: str, day: str, override: float | None = None) -> tuple[float, str, str]:
    if ccy == "SGD":
        return 1.0, day, "identity"
    if override:
        return override, day, "given"
    cache_p = data_dir() / "fx_cache.json"
    cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    key = f"{ccy}:{day}"
    if key not in cache:
        r, d, src = fetch_rate(ccy, day)
        cache[key] = {"rate": r, "date": d, "source": src}
        cache_p.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
    c = cache[key]
    return c["rate"], c["date"], c["source"]


# ------------------------------------------------------------------------ splits
def allocate(total: int, weights: dict[str, float]) -> dict[str, int]:
    """Split integer cents by weight; largest remainders get the spare cents."""
    if not weights or sum(weights.values()) <= 0:
        raise SplitError("Nobody to split between.")
    wsum = sum(weights.values())
    raw = {k: total * w / wsum for k, w in weights.items()}
    out = {k: int(v // 1) for k, v in raw.items()}
    spare = total - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), k))[:spare]:
        out[k] += 1
    return out


def parse_pairs(spec: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        if "=" not in part:
            raise SplitError(f"'{part}' should look like Name=amount.")
        k, v = part.split("=", 1)
        try:
            out[k.strip()] = out.get(k.strip(), 0.0) + float(v.replace(",", ""))
        except ValueError:
            raise SplitError(f"'{v}' isn't a number.") from None
    return out


def build_shares(trip: dict, amount: float, ccy: str, fx: float, mode: str,
                 spec: str | None, prorate: bool) -> tuple[int, dict[str, int], str]:
    """Return (amount in SGD cents, {member: SGD cents}, canonical split text)."""
    total = round(amount * fx * 100)
    if mode == "equal":
        names = [find_member(trip, n)["name"] for n in spec.split(",") if n.strip()] if spec \
            else [m["name"] for m in trip["members"]]
        names = list(dict.fromkeys(names))
        return total, allocate(total, {n: 1 for n in names}), ",".join(names)
    pairs = {find_member(trip, k)["name"]: v for k, v in parse_pairs(spec or "").items()}
    if any(v < 0 for v in pairs.values()):
        raise SplitError("Shares can't be negative.")
    if mode == "shares":
        return total, allocate(total, pairs), ",".join(f"{k}={v:g}" for k, v in pairs.items())
    # exact amounts in the payment currency
    given = sum(pairs.values())
    tol = 10 ** -dp(ccy) / 2 + 1e-9
    if abs(given - amount) > tol and not prorate:
        raise SplitError(f"The amounts add up to {money(given, ccy)}, not {money(amount, ccy)}. "
                         "Fix them, or prorate the difference (tax, service, tip).")
    return total, allocate(total, pairs), ",".join(f"{k}={v:g}" for k, v in pairs.items())


def fmt_shares(shares: dict[str, int]) -> str:
    return ";".join(f"{k}={v / 100:.2f}" for k, v in sorted(shares.items()))


def read_shares(text: str) -> dict[str, int]:
    out = {}
    for part in filter(None, text.split(";")):
        k, v = part.split("=")
        out[k] = round(float(v) * 100)
    return out


# ---------------------------------------------------------------------- balances
def nets(trip: dict, rows: list[dict] | None = None) -> dict[str, dict[str, int]]:
    """Per member: paid, share and net in SGD cents (net > 0 means they are owed)."""
    out = {m["name"]: {"paid": 0, "share": 0, "net": 0} for m in trip["members"]}
    for r in trip_rows(trip, rows):
        payer = out.setdefault(r["payer"], {"paid": 0, "share": 0, "net": 0})
        payer["paid"] += round(float(r["amount_sgd"]) * 100)
        for k, v in read_shares(r["shares_sgd"]).items():
            out.setdefault(k, {"paid": 0, "share": 0, "net": 0})["share"] += v
    for v in out.values():
        v["net"] = v["paid"] - v["share"]
    return out


def settle_plan(net: dict[str, int]) -> list[tuple[str, str, int]]:
    """Fewest-ish transfers: biggest debtor pays biggest creditor, repeat."""
    debt = sorted([[k, -v] for k, v in net.items() if v < 0], key=lambda x: (-x[1], x[0]))
    cred = sorted([[k, v] for k, v in net.items() if v > 0], key=lambda x: (-x[1], x[0]))
    plan = []
    while debt and cred:
        d, c = debt[0], cred[0]
        x = min(d[1], c[1])
        plan.append((d[0], c[0], x))
        d[1] -= x
        c[1] -= x
        if d[1] == 0:
            debt.pop(0)
        if c[1] == 0:
            cred.pop(0)
        debt.sort(key=lambda x: (-x[1], x[0]))
        cred.sort(key=lambda x: (-x[1], x[0]))
    return plan


def balance_text(trip: dict, rows: list[dict] | None = None) -> str:
    n = nets(trip, rows)
    spent = sum(round(float(r["amount_sgd"]) * 100) for r in trip_rows(trip, rows)
                if r["kind"] == "expense")
    lines = [f"{trip['name']}: {sgd(spent)} spent so far"]
    for name, v in sorted(n.items(), key=lambda kv: -kv[1]["net"]):
        tag = "is owed" if v["net"] > 0 else "owes" if v["net"] < 0 else "is square"
        amt = f" {sgd(abs(v['net']))}" if v["net"] else ""
        lines.append(f"• {name} {tag}{amt} (paid {sgd(v['paid'])}, share {sgd(v['share'])})")
    return "\n".join(lines)


def settle_text(trip: dict, rows: list[dict] | None = None) -> str:
    plan = settle_plan({k: v["net"] for k, v in nets(trip, rows).items()})
    if not plan:
        return f"{trip['name']}: everyone is square."
    lines = [f"{trip['name']}: to settle up ({len(plan)} transfer{'s' * (len(plan) != 1)}, SGD)"]
    lines += [f"• {d} → {c}: {sgd(x)}" for d, c, x in plan]
    return "\n".join(lines)


# ---------------------------------------------------------------------- commands
def trip_tz(trip: dict) -> ZoneInfo:
    return ZoneInfo(trip.get("tz") or TZ_BY_CCY.get(trip["currency"], "Asia/Singapore"))


def trip_today(trip: dict) -> str:
    return datetime.now(trip_tz(trip)).date().isoformat()


def phase(trip: dict, today: str | None = None) -> str:
    """'planning' before the trip's start date (or while a /plan trip has no
    dates yet), 'live' from the start date, 'closed' once ended. A trip without
    the planning fields (start, planning) is live, as before."""
    if trip.get("status") == "closed":
        return "closed"
    if trip.get("start"):
        return "planning" if (today or trip_today(trip)) < trip["start"] else "live"
    return "planning" if trip.get("planning") else "live"


def when_text(trip: dict) -> str:
    """'12 Dec – 19 Dec 2026', 'from 12 Dec 2026', 'Dec 2026, dates not set' or 'dates not set'."""
    def d(x: str, year: bool = True) -> str:
        v = date.fromisoformat(x)
        return f"{v.day} {v:%b}" + (f" {v.year}" if year else "")
    if trip.get("start") and trip.get("end"):
        return f"{d(trip['start'], trip['start'][:4] != trip['end'][:4])} – {d(trip['end'])}"
    if trip.get("start"):
        return f"from {d(trip['start'])}"
    if trip.get("window"):
        return f"{trip['window']}, dates not set"
    return "dates not set"


def set_dates(trip: dict, start: str | None, end: str | None, window: str | None) -> None:
    for x in (start, end):
        if x:
            try:
                date.fromisoformat(x)
            except ValueError:
                raise SplitError(f"'{x}' isn't a date; use YYYY-MM-DD.") from None
    if start:
        trip["start"] = start
    if end:
        trip["end"] = end
    if window:
        trip["window"] = window
    if trip.get("start") and trip.get("end") and trip["end"] < trip["start"]:
        raise SplitError("The end date is before the start date.")


def event_date(update_id: str | None, tz=SGT) -> str | None:
    if not update_id:
        return None
    p = inbox() / f"{update_id}.json"
    if p.exists():
        ts = json.loads(p.read_text()).get("date")
        if ts:
            return datetime.fromtimestamp(int(ts), tz).date().isoformat()
    return None


def entry_line(r: dict) -> str:
    amt = float(r["amount"])
    if r["kind"] == "transfer":
        to = next(iter(read_shares(r["shares_sgd"])))
        return f"#{r['entry']} {r['payer']} paid {to} {money(amt, r['currency'])} (settle-up)"
    local = money(amt, r["currency"])
    conv = "" if r["currency"] == "SGD" else f" ({sgd(round(float(r['amount_sgd']) * 100))})"
    shares = read_shares(r["shares_sgd"])
    vals = set(shares.values())
    if r["split_mode"] == "equal" and max(vals) - min(vals) <= 1:
        split = f"split {len(shares)} ways, ~{sgd(max(vals))} each"
    else:
        split = ", ".join(f"{k} {sgd(v)}" for k, v in sorted(shares.items(), key=lambda kv: -kv[1]))
    return f"#{r['entry']} {r['desc']}: {local}{conv}, paid by {r['payer']}; {split}"


def cmd_trip(a) -> int:
    trips = load_trips()
    if a.action == "list":  # open trips, for requests that arrive outside the trip's group
        live = [t for t in trips if t["status"] == "open"]
        print("\n".join(f"{t['name']}\tchat {t['chat_id']}\t{t['currency']}\tsince {t['created']}"
                        + ("\tplanning" if phase(t) == "planning" else "")
                        for t in live) or "No open trips.")
        return 0
    if not a.chat:
        raise SplitError("--chat is required")
    if a.action == "new":
        if any(t["chat_id"] == str(a.chat) and t["status"] == "open" for t in trips):
            raise SplitError("This group already has an open trip. End it first (/endtrip).")
        start = a.start or datetime.now(SGT).date().isoformat()
        created = datetime.now(SGT).date().isoformat() if a.planning else start
        tid = f"{start[:7]}-{slug(a.name)}"
        n = 2
        while any(t["id"] == tid for t in trips):
            tid, n = f"{start[:7]}-{slug(a.name)}-{n}", n + 1
        members = parse_members(a.members or "")
        ccy = a.currency.upper()
        tz = a.tz or TZ_BY_CCY.get(ccy, "Asia/Singapore")
        ZoneInfo(tz)  # fail early on a typo
        trip = {"id": tid, "name": a.name, "chat_id": str(a.chat), "currency": ccy, "tz": tz,
                "status": "open", "created": created, "closed": None, "members": members}
        names = ", ".join(m["name"] for m in members) or "nobody yet"
        if a.planning:  # dates are the trip's own; --start is the first day, not the creation day
            trip["planning"] = True
            set_dates(trip, a.start, a.end, a.window)
            trips.append(trip)
            save_trips(trips)
            print(f"Planning '{a.name}' ({when_text(trip)}; local currency {ccy}, settling in SGD). "
                  f"Members: {names}.")
            return 0
        trips.append(trip)
        save_trips(trips)
        print(f"Trip '{a.name}' started (local currency {trip['currency']}, settling in SGD). "
              f"Members: {names}.")
        return 0
    trip = open_trip(a.chat, trips)
    if a.action == "show":
        print(json.dumps(trip, indent=1, ensure_ascii=False))
    elif a.action == "currency":
        trip["currency"] = a.currency.upper()
        save_trips(trips)
        print(f"Default currency for {trip['name']} is now {trip['currency']}.")
    elif a.action == "tz":
        ZoneInfo(a.tz)
        trip["tz"] = a.tz
        save_trips(trips)
        print(f"{trip['name']} now uses {a.tz} for dates and the nightly summary.")
    elif a.action == "dates":
        if not (a.start or a.end or a.window):
            raise SplitError("Give --start, --end or --window.")
        set_dates(trip, a.start, a.end, a.window)
        save_trips(trips)
        print(f"{trip['name']}: {when_text(trip)} ({phase(trip)}).")
    elif a.action == "move":
        trip["chat_id"] = str(a.to)
        save_trips(trips)
        print(f"{trip['name']} now follows chat {a.to}.")
    elif a.action == "close":
        text = settle_text(trip)
        trip["status"], trip["closed"] = "closed", datetime.now(SGT).date().isoformat()
        save_trips(trips)
        print(f"Trip ended.\n{text}")
    return 0


def cmd_member(a) -> int:
    trips = load_trips()
    trip = open_trip(a.chat, trips)
    if a.action == "add":
        for m in parse_members(a.name):
            try:
                existing = find_member(trip, m["name"])
            except SplitError:
                existing = None
            if existing:  # fill in a missing Telegram id or username
                existing["tg_id"] = existing.get("tg_id") or m["tg_id"]
                existing["username"] = existing.get("username") or m["username"]
            else:
                trip["members"].append(m)
    elif a.action == "remove":
        m = find_member(trip, a.name)
        if nets(trip).get(m["name"], {}).get("paid") or nets(trip).get(m["name"], {}).get("share"):
            raise SplitError(f"{m['name']} already has payments on this trip; they stay on it.")
        trip["members"].remove(m)
    elif a.action == "rename":
        m = find_member(trip, a.name)
        old, new = m["name"], (a.to or "").strip()
        rename_member(trip, trips, old, new)
        print(f"Renamed {old} to {new} on every entry and in the plan.")
    save_trips(trips)
    print("Members: " + ", ".join(m["name"] for m in trip["members"]))
    return 0


def swap_name(text: str, old: str, new: str, sep: str) -> str:
    """Rename one member inside a separated list like 'Sam,Kai' or 'Kai=1.61;Sam=1.60'."""
    out = []
    for part in text.split(sep) if text else []:
        key, eq, rest = part.partition("=")
        out.append((new if key.strip() == old else key) + eq + rest)
    return sep.join(out)


def rename_member(trip: dict, trips: list[dict], old: str, new: str) -> None:
    """Rename a member everywhere: the member list, every entry's payer, split and
    shares, and the trip's plan (to-do owners, bookings, votes, decisions)."""
    if not re.fullmatch(r"[^\s=;,:@][^=;,:@]{0,30}", new or "") or new.strip() != new:
        raise SplitError("A name is 1-31 characters, without = ; , : or @.")
    if any(m["name"].lower() == new.lower() for m in trip["members"] if m["name"] != old):
        raise SplitError(f"{new} is already on this trip.")
    if new == old:
        return
    rows = load_rows()
    for r in rows:
        if r["trip_id"] != trip["id"]:
            continue
        if r["payer"] == old:
            r["payer"] = new
        r["split"] = swap_name(r["split"], old, new, ",")
        r["shares_sgd"] = swap_name(r["shares_sgd"], old, new, ";")
    for m in trip["members"]:
        if m["name"] == old:
            m["name"] = new
    plan_file = data_dir() / "plans" / f"{trip['id']}.json"
    if plan_file.exists():
        plan = json.loads(plan_file.read_text())
        for t in plan.get("todos", []):
            t["owner"] = new if t.get("owner") == old else t.get("owner")
        for b in plan.get("bookings", []):
            b["who"] = new if b.get("who") == old else b.get("who")
        for i in plan.get("ideas", []):
            i["by"] = new if i.get("by") == old else i.get("by")
        for d in plan.get("decisions", []):
            d["by"] = [new if x == old else x for x in d.get("by") or []] if isinstance(d.get("by"), list) else d.get("by")
        for q in plan.get("polls", []):
            if old in (q.get("votes") or {}):
                q["votes"][new] = q["votes"].pop(old)
        tmp = plan_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
        tmp.replace(plan_file)
    save_rows(rows)


def make_row(trip: dict, entry: int, a, kind: str) -> dict:
    day = a.date or event_date(a.update_id, trip_tz(trip)) or trip_today(trip)
    ccy = (a.currency or trip["currency"]).upper()
    if a.amount <= 0:
        raise SplitError("The amount must be positive.")
    fx, fx_day, src = rate(ccy, day, a.fx)
    payer = find_member(trip, a.payer)["name"]
    if kind == "transfer":
        to = find_member(trip, a.to)["name"]
        if to == payer:
            raise SplitError("Payer and recipient are the same person.")
        total = round(a.amount * fx * 100)
        shares, mode, split, desc = {to: total}, "transfer", to, a.desc or f"settle-up to {to}"
    else:
        mode = "exact" if a.exact else "shares" if a.shares else "equal"
        spec = a.exact or a.shares or a.equal
        total, shares, split = build_shares(trip, a.amount, ccy, fx, mode, spec, a.prorate)
        desc = a.desc
    category = "settle-up" if kind == "transfer" else (a.category or "other")
    return {"trip_id": trip["id"], "entry": str(entry), "update_id": a.update_id or "",
            "date": day, "kind": kind, "payer": payer, "desc": desc, "category": category,
            "amount": f"{a.amount:.{dp(ccy)}f}", "currency": ccy, "fx_to_sgd": f"{fx:.8g}",
            "fx_date": fx_day, "fx_source": src, "amount_sgd": f"{total / 100:.2f}",
            "split_mode": mode, "split": split, "shares_sgd": fmt_shares(shares),
            "logged_by": a.logged_by or "", "source": a.source, "status": "active",
            "note": a.note or "", "logged_at": datetime.now(SGT).isoformat(timespec="seconds")}


def _add(a, kind: str) -> int:
    trip = open_trip(a.chat)
    rows = load_rows()
    if a.update_id and any(r["update_id"] == str(a.update_id) and r["trip_id"] == trip["id"]
                           and r["status"] == "active" for r in rows):
        print(f"Already logged (message {a.update_id}).")
        return 0
    entry = 1 + max([int(r["entry"]) for r in rows if r["trip_id"] == trip["id"]] or [0])
    row = make_row(trip, entry, a, kind)
    rows.append(row)
    save_rows(rows)
    print(entry_line(row))
    return 0


def cmd_add(a) -> int:
    return _add(a, "expense")


def cmd_transfer(a) -> int:
    return _add(a, "transfer")


def _find_entry(trip: dict, rows: list[dict], entry: int | None) -> dict:
    live = [r for r in rows if r["trip_id"] == trip["id"] and r["status"] == "active"]
    if not live:
        raise SplitError("Nothing logged on this trip yet.")
    if entry is None:
        return max(live, key=lambda r: (r["logged_at"], int(r["entry"])))
    for r in live:
        if int(r["entry"]) == entry:
            return r
    raise SplitError(f"There's no entry #{entry}.")


def cmd_delete(a) -> int:
    trip = open_trip(a.chat)
    rows = load_rows()
    r = _find_entry(trip, rows, a.entry)
    r["status"] = "deleted"
    save_rows(rows)
    print(f"Removed {entry_line(r)}")
    return 0


def cmd_edit(a) -> int:
    trip = open_trip(a.chat)
    rows = load_rows()
    old = _find_entry(trip, rows, a.entry)
    if old["kind"] == "transfer":
        raise SplitError("Delete a settle-up and log it again instead of editing it.")
    keep_split = not (a.equal or a.shares or a.exact)
    mode, spec = old["split_mode"], old["split"]
    ns = argparse.Namespace(
        update_id=old["update_id"], date=a.date or old["date"],
        currency=a.currency or old["currency"],
        amount=a.amount if a.amount is not None else float(old["amount"]),
        fx=a.fx or (float(old["fx_to_sgd"]) if not (a.currency or a.date) else None),
        payer=a.payer or old["payer"], desc=a.desc or old["desc"],
        category=a.category or old.get("category") or "other",
        equal=a.equal or (spec if keep_split and mode == "equal" else None),
        shares=a.shares or (spec if keep_split and mode == "shares" else None),
        exact=a.exact or (spec if keep_split and mode == "exact" else None),
        prorate=a.prorate or (keep_split and mode == "exact" and a.amount is not None),
        logged_by=a.logged_by or old["logged_by"], source=old["source"],
        note=(a.note or old["note"]), to=None)
    new = make_row(trip, int(old["entry"]), ns, "expense")
    if not (a.fx or a.currency or a.date):  # same rate as before, same provenance
        new["fx_date"], new["fx_source"] = old["fx_date"], old["fx_source"]
    old["status"] = "edited"
    rows.append(new)
    save_rows(rows)
    print("Corrected " + entry_line(new))
    return 0


def cmd_list(a) -> int:
    trip = open_trip(a.chat)
    live = sorted(trip_rows(trip), key=lambda r: int(r["entry"]))
    if not live:
        print(f"{trip['name']}: nothing logged yet.")
        return 0
    print(f"{trip['name']}: last {min(a.n, len(live))} of {len(live)}")
    for r in live[-a.n:]:
        print(entry_line(r))
    return 0


def cmd_balances(a) -> int:
    print(balance_text(open_trip(a.chat)))
    return 0


def cmd_settle(a) -> int:
    print(settle_text(open_trip(a.chat)))
    return 0


def cmd_pending(a) -> int:
    box = inbox()
    items = sorted(box.glob("*.json"), key=lambda p: int(p.stem)) if box.exists() else []
    print("\n".join(p.read_text() for p in items) if items else "inbox empty")
    return 0


def cmd_done(a) -> int:
    box = inbox()
    for uid in a.update_id:
        for p in box.glob(f"{uid}.*"):
            p.unlink()
    print(f"cleared {len(a.update_id)} event(s)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def chat(p):
        p.add_argument("--chat", required=True, help="Telegram group chat id")

    t = sub.add_parser("trip", help="new, show, list, currency, tz, dates, close, move")
    t.add_argument("action", choices=["new", "show", "list", "currency", "tz", "dates", "close", "move"])
    t.add_argument("--chat", help="Telegram group chat id (not needed for list)")
    t.add_argument("--name")
    t.add_argument("--currency", default="SGD")
    t.add_argument("--members", help="'Ana:123:ana,Ben' (name[:telegram id[:username]])")
    t.add_argument("--start", help="YYYY-MM-DD (default today, SGT). With --planning or for dates: "
                                   "the trip's first day")
    t.add_argument("--end", help="for --planning or dates: the trip's last day, YYYY-MM-DD")
    t.add_argument("--window", help="for --planning or dates: a loose window, e.g. 'Dec 2026'")
    t.add_argument("--planning", action="store_true",
                   help="for new: a trip being planned (/plan); planning until its start date")
    t.add_argument("--to", help="for move: the group's new chat id (Telegram upgraded it)")
    t.add_argument("--tz", help="IANA timezone, e.g. Asia/Ho_Chi_Minh (default: from the currency)")
    t.set_defaults(func=cmd_trip)

    m = sub.add_parser("member", help="add, remove, rename")
    m.add_argument("action", choices=["add", "remove", "rename"])
    chat(m)
    m.add_argument("--name", required=True, help="for add: same format as trip --members")
    m.add_argument("--to", help="new name, for rename")
    m.set_defaults(func=cmd_member)

    def payment(p, transfer=False):
        chat(p)
        p.add_argument("--payer", required=True)
        p.add_argument("--amount", type=float, required=True)
        p.add_argument("--currency", help="default: the trip's currency")
        p.add_argument("--desc", required=not transfer)
        if not transfer:
            p.add_argument("--category", choices=CATEGORIES, default="other")
        p.add_argument("--date", help="YYYY-MM-DD (default: when the message was sent)")
        p.add_argument("--fx", type=float, help="SGD per unit, if a rate was stated (card statement)")
        p.add_argument("--update-id", help="Telegram update id; makes the add idempotent")
        p.add_argument("--logged-by")
        p.add_argument("--source", default="text", choices=["text", "photo", "photo+text", "owner"])
        p.add_argument("--note")
        if transfer:
            p.add_argument("--to", required=True)
            p.set_defaults(equal=None, shares=None, exact=None, prorate=False, category=None)
        else:
            g = p.add_mutually_exclusive_group()
            g.add_argument("--equal", help="'Ben,Ana' (default: everyone on the trip)")
            g.add_argument("--shares", help="'Ben=2,Ana=1' weights")
            g.add_argument("--exact", help="'Ben=3000,Ana=5400' in the payment currency")
            p.add_argument("--prorate", action="store_true",
                           help="with --exact: spread the gap to the total (tax, tip) pro rata")

    p = sub.add_parser("add", help="log a payment")
    payment(p)
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("transfer", help="log a settle-up payment between two people")
    payment(p, transfer=True)
    p.set_defaults(func=cmd_transfer)

    p = sub.add_parser("edit", help="correct an entry (the last one if --entry is omitted)")
    chat(p)
    p.add_argument("--entry", type=int)
    for f in ("payer", "currency", "desc", "date", "logged-by", "note"):
        p.add_argument(f"--{f}")
    p.add_argument("--category", choices=CATEGORIES)
    p.add_argument("--amount", type=float)
    p.add_argument("--fx", type=float)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--equal")
    g.add_argument("--shares")
    g.add_argument("--exact")
    p.add_argument("--prorate", action="store_true")
    p.set_defaults(func=cmd_edit)

    for name, fn in (("undo", cmd_delete), ("delete", cmd_delete)):
        p = sub.add_parser(name, help="remove the last entry" if name == "undo" else "remove entry N")
        chat(p)
        p.add_argument("--entry", type=int, required=name == "delete")
        p.set_defaults(func=fn)

    p = sub.add_parser("list")
    chat(p)
    p.add_argument("--n", type=int, default=10)
    p.set_defaults(func=cmd_list)
    for name, fn in (("balances", cmd_balances), ("settle", cmd_settle)):
        p = sub.add_parser(name)
        chat(p)
        p.set_defaults(func=fn)
    p = sub.add_parser("pending", help="events still in the inbox")
    p.set_defaults(func=cmd_pending)
    p = sub.add_parser("done", help="clear handled events (and delete their photos)")
    p.add_argument("update_id", nargs="+")
    p.set_defaults(func=cmd_done)

    a = ap.parse_args(argv)
    if a.cmd == "trip" and a.action == "new" and not a.name:
        ap.error("trip new needs --name")
    try:
        return a.func(a)
    except SplitError as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
