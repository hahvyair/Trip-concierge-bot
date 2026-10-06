#!/usr/bin/env python3
"""Trip Wrapped: a PDF with year-in-review style cards up front and the full
expense report behind them.

Every figure comes from splitlog.py (SGD cents), like the chart and the
workbook. Called through `report.py wrapped`; see that file for options.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import report as rp
import splitlog as sl

HERE = Path(__file__).resolve().parent
FONT = "'Liberation Sans','DejaVu Sans','Noto Sans',Arial,sans-serif"
EMOJI = "'Noto Color Emoji'"

# Each card: background, ink, and two decorative shapes. Ink is chosen for
# contrast on its background (all pairs >= 4.5:1).
CARDS = {
    "cover":  ("#141414", "#fcfcfb", "#ff5c39", "#4a3aa7"),
    "total":  ("#ff5c39", "#141414", "#ffd23f", "#141414"),
    "genre":  ("#4a3aa7", "#fcfcfb", "#e87ba4", "#ffd23f"),
    "top":    ("#ffd23f", "#141414", "#ff5c39", "#4a3aa7"),
    "peak":   ("#1baf7a", "#141414", "#141414", "#fcfcfb"),
    "awards": ("#e87ba4", "#141414", "#4a3aa7", "#ffd23f"),
    "finale": ("#141414", "#fcfcfb", "#1baf7a", "#ff5c39"),
}

VIBE = {
    "food": ("The Grazers", "You ate your way through {trip}."),
    "drinks": ("The Night Shift", "{trip} after dark was the main event."),
    "transport": ("The Road Crew", "Always on the way somewhere."),
    "lodging": ("The Homebodies", "A good bed was worth it."),
    "activities": ("The Explorers", "You came to do things, and did them."),
    "shopping": ("The Haulers", "Hope there was room in the suitcase."),
    "other": ("The Free Spirits", "A trip that refused to be categorised."),
}

# (key, emoji, title, line). The line takes {v} (the formatted stat).
AWARDS = [
    ("paid", "🏦", "The Bank", "Fronted {v} for the group"),
    ("drinks", "🍸", "Head of Bar", "Bought {v} of drinks"),
    ("food", "🍜", "Head Chef", "Picked up {v} of food"),
    ("transport_n", "🛵", "Ride Royalty", "Booked {v}"),
    ("activities", "🎟️", "Fun Director", "Paid {v} for things to do"),
    ("lodging", "🏨", "The Landlord", "Covered {v} of beds"),
    ("shopping", "🛍️", "Retail Therapist", "Spent {v} shopping for the group"),
    ("biggest", "💸", "Big Splurge", "Dropped {v} in one go"),
    ("count", "🤠", "Quickest Draw", "Reached for the wallet {v}"),
]
BALANCED = ("⚖️", "Perfectly Balanced", "Ends the trip within {v} of square")
VIBES = ("✨", "Chief Vibes Officer", "Kept the mood high, whatever the bill")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole) if whole else 0}%"


# ------------------------------------------------------------------- the stats
def trip_span(trip: dict, rows: list[dict]) -> list[str]:
    """Days from the first payment to the last (the trip as it happened)."""
    if not rows:
        return []
    d0 = date.fromisoformat(min(r["date"] for r in rows))
    d1 = date.fromisoformat(max(r["date"] for r in rows))
    return [date.fromordinal(d0.toordinal() + i).isoformat() for i in range((d1 - d0).days + 1)]


def awards(trip: dict, rows: list[dict], nets: dict) -> list[dict]:
    """One award per member: the trait where they stand out most.

    Each award scores members by their share of that stat; the strongest
    (member, award) pairs are taken first, one award per member and one
    member per award. Ties go to the award listed first.
    """
    names = [m["name"] for m in trip["members"]]
    stat: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        p, c, cat = r["payer"], rp.cents(r), r.get("category") or "other"
        stat["paid"][p] += c
        stat["count"][p] += 1
        stat["biggest"][p] = max(stat["biggest"][p], c)
        if cat == "transport":
            stat["transport_n"][p] += 1
        if cat in ("drinks", "food", "activities", "lodging", "shopping"):
            stat[cat][p] += c

    def fmt(key: str, v: int) -> str:
        if key == "transport_n":
            return plural(v, "ride")
        if key == "count":
            return plural(v, "time")
        return sl.sgd(v)

    pairs = []
    for rank, (key, emoji, title, line) in enumerate(AWARDS):
        whole = sum(stat[key].values())
        if not whole:
            continue
        top = max(stat[key].values())
        for n in names:
            v = stat[key].get(n, 0)
            if v:
                # Share of the stat, with a bonus for being the outright leader.
                score = v / whole + (0.25 if v == top else 0)
                pairs.append((-score, rank, n, key, emoji, title, line.format(v=fmt(key, v))))
    out: dict[str, dict] = {}
    used = set()
    for _, _, n, key, emoji, title, line in sorted(pairs):
        if n in out or key in used:
            continue
        out[n] = {"name": n, "emoji": emoji, "title": title, "line": line}
        used.add(key)
    rest = [n for n in names if n not in out]
    if rest:
        n = min(rest, key=lambda k: abs(nets[k]["net"]))
        if abs(nets[n]["net"]) <= 2000:
            emoji, title, line = BALANCED
            out[n] = {"name": n, "emoji": emoji, "title": title,
                      "line": line.format(v=sl.sgd(abs(nets[n]["net"])))}
    for n in names:
        if n not in out:
            emoji, title, line = VIBES
            out[n] = {"name": n, "emoji": emoji, "title": title, "line": line}
    return [out[n] for n in names]


def stats(trip: dict) -> dict:
    rows = rp.expenses(trip)
    if not rows:
        raise sl.SplitError(f"{trip['name']}: nothing logged yet, so there's nothing to wrap.")
    total = sum(rp.cents(r) for r in rows)
    days = trip_span(trip, rows)
    nets = sl.nets(trip)
    cats = Counter()
    cat_n = Counter()
    for r in rows:
        cats[r.get("category") or "other"] += rp.cents(r)
        cat_n[r.get("category") or "other"] += 1
    per_day = defaultdict(int)
    for r in rows:
        per_day[r["date"]] += rp.cents(r)
    peak = max(days, key=lambda d: (per_day[d], d))
    rides = [rp.cents(r) for r in rows if (r.get("category") or "other") == "transport"]
    local = sum(float(r["amount"]) for r in rows if r["currency"] == trip["currency"])
    return {
        "rows": rows, "total": total, "days": days, "nets": nets,
        "cats": cats.most_common(), "cat_n": cat_n,
        "per_day": per_day, "peak": peak,
        "top": sorted(rows, key=lambda r: (-rp.cents(r), int(r["entry"])))[:5],
        "per_head": round(total / max(len(trip["members"]), 1)),
        "ride_avg": round(sum(rides) / len(rides)) if len(rides) >= 3 else 0,
        "local": local if all(r["currency"] == trip["currency"] for r in rows) else 0,
        "plan": sl.settle_plan({k: v["net"] for k, v in nets.items()}),
        "awards": awards(trip, rows, nets),
        "transfers": [r for r in sl.trip_rows(trip) if r["kind"] == "transfer"],
    }


# ------------------------------------------------------------------- the cards
def card(kind: str, body: str) -> str:
    bg, ink, a, b = CARDS[kind]
    return (f"<section class='card' style='background:{bg};color:{ink}'>"
            f"<i class='blob b1' style='background:{a}'></i><i class='blob b2' style='background:{b}'></i>"
            f"<div class='inner'>{body}</div></section>")


def local_line(trip: dict, s: dict) -> str:
    ccy, x = trip["currency"], s["local"]
    if not x or ccy == "SGD":
        return ""
    if x >= 1_000_000:
        return f"or {x / 1e6:,.1f} million {ccy}. Millionaires, briefly."
    return f"or {sl.money(x, ccy)}."


def cards(trip: dict, s: dict) -> str:
    esc, name = rp.esc, rp.esc(trip["name"])
    n_days, members = len(s["days"]), [m["name"] for m in trip["members"]]
    out = []

    out.append(card("cover", f"""
      <p class='kicker'>{esc(rp.day_label(s['days'][0]))} – {esc(rp.day_label(s['days'][-1]))}</p>
      <h1 class='mega'>{name},<br>Wrapped</h1>
      <p class='lead'>{plural(len(members), 'traveller')} · {plural(n_days, 'day')} ·
      {plural(len(s['rows']), 'payment')}</p>
      <p class='names'>{' · '.join(esc(m) for m in members)}</p>"""))

    extra = ""
    if s["ride_avg"]:
        extra = (f"<p class='aside'>That's {s['total'] // s['ride_avg']:,} rides at your average fare of "
                 f"{sl.sgd(s['ride_avg'])}.</p>")
    out.append(card("total", f"""
      <p class='kicker'>Together you spent</p>
      <p class='huge'>{sl.sgd(s['total'])}</p>
      <p class='lead'>{esc(local_line(trip, s))}</p>
      <div class='pair'><div><b>{sl.sgd(round(s['total'] / max(n_days, 1)))}</b><span>a day</span></div>
      <div><b>{sl.sgd(s['per_head'])}</b><span>each, on average</span></div></div>{extra}"""))

    top_cat = s["cats"][0][0]
    vibe, tag = VIBE.get(top_cat, VIBE["other"])
    bars = "".join(
        f"<li><span class='rank'>{i}</span><span class='lab'>{c.capitalize()}</span>"
        f"<span class='bar'><i style='width:{max(4, 100 * v / s['cats'][0][1]):.0f}%'></i></span>"
        f"<span class='val'>{pct(v, s['total'])}</span></li>"
        for i, (c, v) in enumerate(s["cats"], 1))
    out.append(card("genre", f"""
      <p class='kicker'>Your top category</p>
      <p class='huge'>{top_cat.capitalize()}</p>
      <p class='lead'>{pct(s['cats'][0][1], s['total'])} of everything. You were <b>{vibe}</b>.
      {esc(tag.format(trip=trip['name']))}</p>
      <ol class='genres'>{bars}</ol>"""))

    items = "".join(
        f"<li><span class='no'>{i}</span><span class='what'><b>{esc(r['desc'])}</b>"
        f"<small>{esc(r['payer'])} paid · {esc(rp.day_label(r['date']))}</small></span>"
        f"<span class='amt'>{sl.sgd(rp.cents(r))}</span></li>"
        for i, r in enumerate(s["top"], 1))
    out.append(card("top", f"""
      <p class='kicker'>Your top {len(s['top'])}</p>
      <h2 class='title'>The bills you'll remember</h2>
      <ol class='tracks'>{items}</ol>"""))

    peak = s["peak"]
    pmax = max(s["per_day"].values())
    days = "".join(
        f"<li class='{'on' if d == peak else ''}'><span class='lab'>{esc(rp.day_label(d)[:-4])}</span>"
        f"<span class='bar'><i style='width:{max(2, 100 * s['per_day'][d] / pmax):.0f}%'></i></span>"
        f"<span class='val'>{sl.sgd(s['per_day'][d])}</span></li>" for d in s["days"])
    peak_rows = sorted((r for r in s["rows"] if r["date"] == peak), key=lambda r: -rp.cents(r))[:3]
    out.append(card("peak", f"""
      <p class='kicker'>Your biggest day</p>
      <p class='huge'>{esc(rp.day_label(peak))}</p>
      <p class='lead'>{sl.sgd(s['per_day'][peak])}, {pct(s['per_day'][peak], s['total'])} of the whole trip.
      The highlights: {esc(', '.join(r['desc'] for r in peak_rows))}.</p>
      <ol class='days'>{days}</ol>"""))

    tiles = "".join(
        f"<div class='tile'><span class='emo'>{a['emoji']}</span><b>{esc(a['title'])}</b>"
        f"<span class='who'>{esc(a['name'])}</span><small>{esc(a['line'])}</small></div>"
        for a in s["awards"])
    out.append(card("awards", f"""
      <p class='kicker'>The awards</p>
      <h2 class='title'>Everyone brought something</h2>
      <div class='tiles n{min(len(s['awards']), 9)}'>{tiles}</div>"""))

    if s["plan"]:
        moves = "".join(f"<li><b>{esc(d)}</b> <span>→</span> <b>{esc(c)}</b><em>{sl.sgd(x)}</em></li>"
                        for d, c, x in s["plan"])
        settle = (f"<p class='kicker'>Closing the tab</p><h2 class='title'>"
                  f"{plural(len(s['plan']), 'transfer')} and you're all square</h2><ol class='moves'>{moves}</ol>")
    else:
        settle = "<p class='kicker'>Closing the tab</p><h2 class='title'>Everyone's already square. Legendary.</h2>"
    bank = max(s["nets"].items(), key=lambda kv: kv[1]["paid"])[0]
    out.append(card("finale", f"""{settle}
      <div class='summary'>
        <div><span>Spent</span><b>{sl.sgd(s['total'])}</b></div>
        <div><span>Top category</span><b>{top_cat.capitalize()}</b></div>
        <div><span>Biggest day</span><b>{esc(rp.day_label(peak))}</b></div>
        <div><span>Paid the most</span><b>{esc(bank)}</b></div>
      </div>
      <p class='aside'>Same again next year?</p>"""))
    return "".join(out)


# ------------------------------------------------------------------ the report
def report_pages(trip: dict, s: dict) -> str:
    esc, total = rp.esc, s["total"]
    n_days = len(s["days"])
    cat_rows = "".join(
        f"<tr><td><i class='sw' style='background:{rp.CAT_COLOUR.get(c, rp.CAT_COLOUR['other'])}'></i>"
        f"{c.capitalize()}</td><td class='n'>{s['cat_n'][c]}</td><td class='n'>{sl.sgd(v)}</td>"
        f"<td class='n'>{pct(v, total)}</td></tr>" for c, v in s["cats"])
    people = "".join(
        f"<tr><td>{esc(k)}</td><td class='n'>{sl.sgd(v['paid'])}</td><td class='n'>{sl.sgd(v['share'])}</td>"
        f"<td class='n {'pos' if v['net'] > 0 else 'neg' if v['net'] < 0 else ''}'>"
        f"{'+' if v['net'] > 0 else '−' if v['net'] < 0 else ''}{sl.sgd(abs(v['net']))}</td></tr>"
        for k, v in sorted(s["nets"].items(), key=lambda kv: -kv[1]["net"]))
    plan = ("".join(f"<li>{esc(d)} pays {esc(c)} <b>{sl.sgd(x)}</b></li>" for d, c, x in s["plan"])
            or "<li>Everyone is square.</li>")

    def split_txt(r: dict) -> str:
        sh = sl.read_shares(r["shares_sgd"])
        if len(sh) == len(trip["members"]) and r["split_mode"] == "equal":
            return "All"
        return ", ".join(sh)

    pay_rows = "".join(
        f"<tr><td class='n'>{r['entry']}</td><td>{esc(rp.day_label(r['date']))}</td><td>{esc(r['desc'])}</td>"
        f"<td>{esc((r.get('category') or 'other').capitalize())}</td><td>{esc(r['payer'])}</td>"
        f"<td>{esc(split_txt(r))}</td><td class='n'>{sl.money(float(r['amount']), r['currency'])}</td>"
        f"<td class='n'>{sl.sgd(rp.cents(r))}</td></tr>"
        for r in sorted(s["rows"], key=lambda r: (r["date"], int(r["entry"]))))
    transfers = ""
    if s["transfers"]:
        tr = "".join(
            f"<tr><td class='n'>{r['entry']}</td><td>{esc(rp.day_label(r['date']))}</td><td>{esc(r['desc'])}</td>"
            f"<td>{esc(r['payer'])} → {esc(', '.join(sl.read_shares(r['shares_sgd'])))}</td>"
            f"<td class='n'>{sl.money(float(r['amount']), r['currency'])}</td>"
            f"<td class='n'>{sl.sgd(rp.cents(r))}</td></tr>" for r in s["transfers"])
        transfers = (f"<h2>Settle-ups already made</h2><table><thead><tr><th class='n'>#</th><th>Date</th>"
                     f"<th>Note</th><th>From → to</th><th class='n'>Amount</th><th class='n'>SGD</th></tr>"
                     f"</thead><tbody>{tr}</tbody></table>")
    status = "final" if trip["status"] == "closed" else f"as of {rp.day_label(sl.trip_today(trip))}"
    return f"""<section class='report'>
      <h1>{esc(trip['name'])}: expense report</h1>
      <p class='sub'>{esc(rp.day_label(s['days'][0]))} – {esc(rp.day_label(s['days'][-1]))} ·
      {', '.join(esc(m['name']) for m in trip['members'])} · {status}</p>
      <div class='kpis'>
        <div><span>Total spent</span><b>{sl.sgd(total)}</b></div>
        <div><span>Payments</span><b>{len(s['rows'])}</b></div>
        <div><span>Per day</span><b>{sl.sgd(round(total / max(n_days, 1)))}</b></div>
        <div><span>Per person</span><b>{sl.sgd(s['per_head'])}</b></div>
      </div>
      <h2>Spending per day (SGD), by category</h2>
      <div class='chart'>{rp.svg_daily(trip, s['rows'], '')}</div>
      <div class='cols'>
        <div><h2>By category</h2><table><thead><tr><th>Category</th><th class='n'>Payments</th>
          <th class='n'>SGD</th><th class='n'>Share</th></tr></thead><tbody>{cat_rows}</tbody></table></div>
        <div><h2>By person</h2><table><thead><tr><th>Name</th><th class='n'>Paid</th><th class='n'>Share</th>
          <th class='n'>Net</th></tr></thead><tbody>{people}</tbody></table>
          <p class='note'>Net: + is owed, − owes.</p></div>
      </div>
      <h2>To settle up</h2><ul class='plan'>{plan}</ul>
      <h2>Every payment</h2>
      <table class='all'><thead><tr><th class='n'>#</th><th>Date</th><th>What</th><th>Category</th><th>Paid by</th>
        <th>Split</th><th class='n'>Amount</th><th class='n'>SGD</th></tr></thead><tbody>{pay_rows}</tbody>
        <tfoot><tr><td colspan='7'>Total</td><td class='n'>{sl.sgd(total)}</td></tr></tfoot></table>
      {transfers}
      <p class='foot'>{esc(rp.rate_note(trip)[0].upper() + rp.rate_note(trip)[1:])}. Shares are split to the
      cent and always add up to the payment. Settle-ups are not counted as spending.</p>
    </section>"""


CSS = f"""
@page {{ size: A4; margin: 0; }}
@page report {{ size: A4; margin: 16mm 15mm 18mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: {FONT}; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.card {{ width: 210mm; height: 297mm; position: relative; overflow: hidden; break-after: page; }}
.inner {{ position: relative; z-index: 1; height: 100%; padding: 26mm 20mm; display: flex;
  flex-direction: column; justify-content: center; }}
.blob {{ position: absolute; border-radius: 50%; z-index: 0; }}
.b1 {{ width: 150mm; height: 150mm; right: -55mm; top: -55mm; opacity: .9; }}
.b2 {{ width: 95mm; height: 95mm; left: -35mm; bottom: -30mm; opacity: .85; }}
.kicker {{ font-size: 15pt; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; margin: 0 0 6mm; }}
.mega {{ font-size: 70pt; line-height: .95; margin: 0 0 10mm; letter-spacing: -.02em; }}
.huge {{ font-size: 60pt; font-weight: 700; line-height: 1; margin: 0 0 7mm; letter-spacing: -.02em; }}
.title {{ font-size: 30pt; line-height: 1.1; margin: 0 0 10mm; }}
.lead {{ font-size: 17pt; line-height: 1.35; margin: 0 0 9mm; max-width: 150mm; }}
.names {{ font-size: 14pt; opacity: .85; margin: 0; }}
.aside {{ font-size: 14pt; margin: 10mm 0 0; font-style: italic; }}
.pair {{ display: flex; gap: 14mm; }}
.pair b {{ display: block; font-size: 30pt; }}
.pair span {{ font-size: 13pt; }}
ol {{ list-style: none; padding: 0; margin: 0; }}
.genres li, .days li {{ display: grid; grid-template-columns: 10mm 38mm 1fr 26mm; align-items: center;
  gap: 4mm; font-size: 15pt; margin: 0 0 4mm; }}
.days li {{ grid-template-columns: 28mm 1fr 32mm; font-size: 14pt; opacity: .75; }}
.days li.on {{ opacity: 1; font-weight: 700; }}
.rank {{ font-weight: 700; }}
.bar {{ height: 7mm; }}
.bar i {{ display: block; height: 100%; border-radius: 1.5mm; background: currentColor; }}
.val {{ text-align: right; font-weight: 700; }}
.tracks li {{ display: grid; grid-template-columns: 16mm 1fr auto; align-items: center; gap: 4mm;
  padding: 4.5mm 0; border-bottom: .5mm solid rgba(20,20,20,.25); }}
.tracks .no {{ font-size: 30pt; font-weight: 700; }}
.tracks b {{ display: block; font-size: 17pt; }}
.tracks small {{ font-size: 12pt; }}
.tracks .amt {{ font-size: 18pt; font-weight: 700; }}
.tiles {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6mm; }}
.tiles.n1 {{ grid-template-columns: 1fr; }}
.tile {{ background: rgba(255,255,255,.55); border-radius: 4mm; padding: 6mm; }}
.tiles.n7 .tile, .tiles.n8 .tile, .tiles.n9 .tile {{ padding: 4mm; }}
.tile .emo {{ font-family: {EMOJI}; font-size: 24pt; display: block; margin-bottom: 2mm; }}
.tile b {{ display: block; font-size: 16pt; }}
.tile .who {{ display: block; font-size: 22pt; font-weight: 700; margin: 1mm 0 2mm; }}
.tile small {{ font-size: 11.5pt; line-height: 1.3; }}
.moves li {{ font-size: 20pt; margin: 0 0 5mm; display: flex; gap: 4mm; align-items: baseline; }}
.moves em {{ font-style: normal; margin-left: auto; font-weight: 700; }}
.summary {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6mm; margin-top: 12mm;
  border-top: .6mm solid currentColor; padding-top: 8mm; }}
.summary span {{ display: block; font-size: 12pt; text-transform: uppercase; letter-spacing: .08em; opacity: .8; }}
.summary b {{ font-size: 22pt; }}

.report {{ page: report; color: {rp.INK}; font-size: 9.5pt; }}
.report h1 {{ font-size: 20pt; margin: 0 0 1mm; }}
.report h2 {{ font-size: 12pt; margin: 7mm 0 2.5mm; }}
.report .sub {{ color: {rp.INK2}; margin: 0 0 5mm; }}
.kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 4mm; }}
.kpis div {{ border: .3mm solid {rp.GRID}; border-radius: 2mm; padding: 3mm 4mm; }}
.kpis span {{ display: block; color: {rp.INK2}; font-size: 8.5pt; }}
.kpis b {{ font-size: 15pt; }}
.chart svg {{ width: 100%; height: auto; }}
.chart .legend {{ display: flex; gap: 12px; flex-wrap: wrap; font-size: 8.5pt; color: {rp.INK2}; }}
.chart .key i, .sw {{ display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8mm; break-inside: avoid; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ text-align: left; font-weight: 700; color: {rp.INK2}; border-bottom: .4mm solid {rp.AXIS}; padding: 1.5mm 2mm; }}
td {{ padding: 1.4mm 2mm; border-bottom: .2mm solid {rp.GRID}; vertical-align: top; }}
tfoot td {{ font-weight: 700; border-top: .4mm solid {rp.AXIS}; border-bottom: none; }}
tr {{ break-inside: avoid; }}
.n {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.pos {{ color: {rp.POS}; }} .neg {{ color: {rp.NEG}; }}
.note {{ color: {rp.MUTED}; font-size: 8.5pt; margin: 1.5mm 0 0; }}
.plan {{ margin: 0; padding-left: 5mm; }} .plan li {{ margin: 0 0 1mm; }}
.foot {{ color: {rp.MUTED}; font-size: 8.5pt; margin-top: 6mm; }}
"""


def build_html(trip: dict) -> str:
    s = stats(trip)
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{rp.esc(trip['name'])} Wrapped</title>"
            f"<style>{CSS}</style></head><body>{cards(trip, s)}{report_pages(trip, s)}</body></html>")


def pdf_name(trip: dict) -> str:
    stamp = (trip.get("closed") or sl.trip_today(trip)).replace("-", ".")
    return f"{stamp}_{trip['name']} Trip Wrapped.pdf"


def render(trip: dict) -> Path:
    d = rp.out_dir(trip)
    src, pdf = d / "wrapped.html", d / pdf_name(trip)
    src.write_text(build_html(trip), encoding="utf-8")
    env = dict(os.environ)
    if "NODE_PATH" not in env:
        env["NODE_PATH"] = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["node", str(HERE / "render_pdf.js"), str(src), str(pdf)],
                   check=True, capture_output=True, text=True, env=env)
    return pdf


def caption(trip: dict) -> str:
    s = stats(trip)
    return (f"{trip['name']}, Wrapped: {sl.sgd(s['total'])} over {plural(len(s['days']), 'day')}. "
            f"The fun part first, the full expense report at the back.")
