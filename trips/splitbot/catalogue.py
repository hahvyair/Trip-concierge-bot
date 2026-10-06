#!/usr/bin/env python3
"""Trip catalogue: the plan as a PDF to share while a trip is being planned.

A cover, the trip at a glance, the itinerary day by day (with Maps links),
the shortlist as a catalogue of places grouped by kind, the bookings
checklist, what's been decided and the open to-dos. Everything comes from
the plan file (plan.py) and the trip record, so the PDF never disagrees with
"where are we". Called through `report.py catalogue`; see that file.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import plan as pl
import report as rp
import splitlog as sl

HERE = Path(__file__).resolve().parent
FONT = "'Liberation Sans','DejaVu Sans','Noto Sans',Arial,sans-serif"
KIND_TITLE = {"stay": "Where to stay", "food": "Eat and drink", "activity": "Things to do",
              "destination": "Places to go", "transport": "Getting around", "other": "Also on the list"}
KIND_ORDER = ["destination", "stay", "activity", "food", "transport", "other"]
STATUS_ORDER = {"chosen": 0, "shortlisted": 1, "idea": 2}
INK, INK2, MUTED, GRID, ACCENT = "#141414", "#52514e", "#898781", "#e1e0d9", "#ff5c39"


def esc(x) -> str:
    return rp.esc("" if x is None else x)


def is_day(d: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or ""))


def day_title(d: str) -> str:
    if not is_day(d):
        return d
    x = date.fromisoformat(d)
    return f"{x:%A} {x.day} {x:%B}"


def maps(query: str) -> str:
    return "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": query})


def link(url: str | None, label: str) -> str:
    if not url or not re.match(r"^https?://", url):
        return ""
    return f"<a href='{esc(url)}'>{esc(label)}</a>"


def sort_days(keys: list[str]) -> list[str]:
    """Real dates in order, then 'Day 1', 'Day 2'... in number order."""
    def key(k: str):
        if is_day(k):
            return (0, k, 0)
        m = re.search(r"\d+", k)
        return (1, "", int(m.group()) if m else 999)
    return sorted(keys, key=key)


# ------------------------------------------------------------------ sections
def cover(trip: dict, plan: dict) -> str:
    where = ", ".join(plan.get("destinations") or []) or trip["name"]
    who = ", ".join(m["name"] for m in trip["members"])
    return f"""<section class='cover'>
      <i class='blob b1'></i><i class='blob b2'></i>
      <div class='inner'>
        <p class='kicker'>Trip catalogue</p>
        <h1>{esc(where)}</h1>
        <p class='lead'>{esc(sl.when_text(trip))}</p>
        <p class='names'>{esc(who)}</p>
      </div></section>"""


def glance(trip: dict, plan: dict) -> str:
    now = sl.trip_today(trip)
    facts = [("When", sl.when_text(trip))]
    if is_day(trip.get("start") or "") and trip["start"] >= now:
        n = (date.fromisoformat(trip["start"]) - date.fromisoformat(now)).days
        facts.append(("Countdown", f"{n} day{'s' * (n != 1)} to go"))
    if plan.get("destinations"):
        facts.append(("Where", ", ".join(plan["destinations"])))
    facts.append(("Who", f"{len(trip['members'])}: " + ", ".join(m["name"] for m in trip["members"])))
    if plan.get("budget_pp_sgd"):
        facts.append(("Budget", f"{sl.sgd(round(plan['budget_pp_sgd'] * 100))} per person"))
    bk = plan.get("bookings") or []
    if bk:
        done = sum(b["status"] == "booked" for b in bk)
        est = sum(b.get("est_sgd") or 0 for b in bk)
        line = f"{done} of {len(bk)} booked"
        if est:
            line += f" · ~{sl.sgd(round(est * 100))} estimated in total"
            if plan.get("budget_pp_sgd") and trip["members"]:
                line += f" (~{sl.sgd(round(est * 100 / len(trip['members'])))} each)"
        facts.append(("Bookings", line))
    open_polls = [p for p in plan.get("polls") or [] if p.get("status") == "open"]
    if open_polls:
        facts.append(("Still deciding", "; ".join(p["question"] for p in open_polls)))
    rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in facts)
    notes = "".join(f"<p class='note'>{esc(n)}</p>" for n in (plan.get("notes") or [])[-3:])
    return f"<section class='page'><h2>At a glance</h2><table class='facts'>{rows}</table>{notes}</section>"


def itinerary(trip: dict, plan: dict) -> str:
    it = plan.get("itinerary") or {}
    days = [d for d in sort_days(list(it)) if it.get(d)]
    if not days:
        return ("<section class='flow'><h2>Day by day</h2><p class='muted'>No days planned yet. "
                "Ask the bot to draft an itinerary, or add stops in the chat.</p></section>")
    area = (plan.get("destinations") or [trip["name"]])[0]
    blocks = []
    for n, d in enumerate(days, 1):
        rows = []
        for x in it[d]:
            where = x.get("where") or ""
            extras = [link(maps(f"{where or x['what']}, {area}"), "Map")]
            if x.get("link"):
                extras.append(link(x["link"], "Info"))
            meta = [esc(where)] if where else []
            if x.get("cost_sgd"):
                meta.append(f"~{sl.sgd(round(x['cost_sgd'] * 100))} pp")
            if x.get("booked"):
                meta.append("<span class='badge'>booked</span>")
            rows.append(
                f"<li><span class='time'>{esc(x.get('time') or '')}</span><div><b>{esc(x['what'])}</b>"
                + (f"<div class='meta'>{' · '.join(meta)}</div>" if meta else "")
                + (f"<div class='note'>{esc(x['note'])}</div>" if x.get("note") else "")
                + f"<div class='links'>{' · '.join(e for e in extras if e)}</div></div></li>")
        label = f"Day {n}" if is_day(d) else ""
        blocks.append(f"<div class='day'><h3><span class='dayno'>{label}</span>{esc(day_title(d))}</h3>"
                      f"<ol class='stops'>{''.join(rows)}</ol></div>")
    return f"<section class='flow'><h2>Day by day</h2>{''.join(blocks)}</section>"


def catalogue(trip: dict, plan: dict) -> str:
    ideas = [i for i in plan.get("ideas") or [] if i.get("status") != "dropped"]
    if not ideas:
        return ""
    area = (plan.get("destinations") or [trip["name"]])[0]
    groups = []
    for kind in KIND_ORDER:
        items = sorted((i for i in ideas if (i.get("kind") or "other") == kind),
                       key=lambda i: (STATUS_ORDER.get(i.get("status"), 3), i["n"]))
        if not items:
            continue
        cards = []
        for i in items:
            badge = {"chosen": "chosen", "shortlisted": "shortlisted"}.get(i.get("status"), "")
            meta = [x for x in (esc(i.get("area")), esc(i.get("price"))) if x]
            links = [link(i.get("link"), "Info"), link(maps(f"{i['text']}, {i.get('area') or area}"), "Map")]
            cards.append(
                f"<div class='item {badge}'>"
                + (f"<span class='badge'>{badge}</span>" if badge else "")
                + f"<b>{esc(i['text'])}</b>"
                + (f"<p>{esc(i['why'])}</p>" if i.get("why") else "")
                + (f"<div class='meta'>{' · '.join(meta)}</div>" if meta else "")
                + f"<div class='links'>{' · '.join(x for x in links if x)}"
                + (f" · <span class='by'>from {esc(i['by'])}</span>" if i.get("by") and i["by"] != "bot" else "")
                + "</div></div>")
        groups.append(f"<h3>{KIND_TITLE[kind]}</h3><div class='grid'>{''.join(cards)}</div>")
    return f"<section class='page'><h2>The shortlist</h2>{''.join(groups)}</section>"


def logistics(trip: dict, plan: dict) -> str:
    out = []
    bk = plan.get("bookings") or []
    if bk:
        rows = "".join(
            f"<tr><td>{'✓' if b['status'] == 'booked' else '◐' if b['status'] == 'held' else '○'}</td>"
            f"<td><b>{esc(b['item'])}</b>{('<br>' + link(b.get('link'), 'listing')) if b.get('link') else ''}</td>"
            f"<td>{esc(b.get('who') or '—')}</td><td>{esc('to do' if b['status'] == 'todo' else b['status'])}</td>"
            f"<td class='n'>{sl.sgd(round(b['est_sgd'] * 100)) if b.get('est_sgd') else ''}</td>"
            f"<td>{esc(b.get('deadline') or '')}</td></tr>" for b in bk)
        out.append("<h2>Bookings checklist</h2><table class='list'><thead><tr><th></th><th>What</th><th>Who</th>"
                   f"<th>Status</th><th class='n'>Est.</th><th>By</th></tr></thead><tbody>{rows}</tbody></table>"
                   "<p class='muted small'>The bot keeps the checklist; it never books or pays.</p>")
    dec = plan.get("decisions") or []
    if dec:
        out.append("<div class='block'><h2>Decided</h2><ul class='plain'>" + "".join(
            f"<li><b>{esc(d['topic'])}:</b> {esc(d['outcome'])} <span class='muted'>({esc(d.get('how', ''))})</span></li>"
            for d in dec) + "</ul></div>")
    todos = [t for t in plan.get("todos") or [] if t.get("status") != "done"]
    if todos:
        out.append("<div class='block'><h2>Still to do</h2><ul class='plain'>" + "".join(
            f"<li><b>{esc(t.get('owner') or 'unassigned')}:</b> {esc(t['task'])}"
            + (f" <span class='muted'>(by {esc(t['due'])})</span>" if t.get("due") else "") + "</li>"
            for t in todos) + "</ul></div>")
    if not out:
        return ""
    return f"<section class='flow'>{''.join(out)}</section>"


CSS = f"""
@page {{ size: A4; margin: 16mm 15mm 18mm; }}
@page cover {{ size: A4; margin: 0; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: {FONT}; color: {INK}; font-size: 10.5pt; line-height: 1.4;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
a {{ color: #2a78d6; text-decoration: none; }}
.cover {{ page: cover; width: 210mm; height: 297mm; position: relative; overflow: hidden; background: #0f3d4c;
  color: #fcfcfb; break-after: page; }}
.cover .inner {{ position: relative; z-index: 1; height: 100%; padding: 30mm 20mm; display: flex;
  flex-direction: column; justify-content: flex-end; }}
.blob {{ position: absolute; border-radius: 50%; }}
.b1 {{ width: 170mm; height: 170mm; right: -60mm; top: -50mm; background: #ffd23f; opacity: .9; }}
.b2 {{ width: 110mm; height: 110mm; left: -40mm; top: 70mm; background: {ACCENT}; opacity: .85; }}
.kicker {{ font-size: 14pt; font-weight: 700; letter-spacing: .14em; text-transform: uppercase; margin: 0 0 4mm; }}
.cover h1 {{ font-size: 54pt; line-height: 1; margin: 0 0 6mm; letter-spacing: -.02em; }}
.lead {{ font-size: 18pt; margin: 0 0 4mm; }}
.names {{ font-size: 13pt; opacity: .85; margin: 0; }}
.page {{ break-before: page; }}
.flow {{ margin-top: 10mm; }}
.block {{ break-inside: avoid; margin-top: 9mm; }}
h2, h3 {{ break-after: avoid; }}
h2 {{ font-size: 20pt; margin: 0 0 5mm; border-bottom: .8mm solid {ACCENT}; padding-bottom: 2mm; }}
.page h2 + h2, .page table + h2, .page ul + h2, .page p + h2 {{ margin-top: 9mm; }}
h3 {{ font-size: 13pt; margin: 7mm 0 3mm; }}
.facts th {{ text-align: left; color: {INK2}; font-weight: 700; padding: 2mm 6mm 2mm 0; vertical-align: top; width: 34mm; }}
.facts td {{ padding: 2mm 0; }}
.facts tr {{ border-bottom: .2mm solid {GRID}; }}
table {{ width: 100%; border-collapse: collapse; }}
.note {{ color: {INK2}; font-style: italic; margin: 1mm 0 0; }}
.muted {{ color: {MUTED}; }} .small {{ font-size: 9pt; }}
.day {{ break-inside: avoid; margin-bottom: 6mm; }}
.day h3 {{ display: flex; gap: 3mm; align-items: baseline; border-bottom: .3mm solid {GRID}; padding-bottom: 1.5mm; }}
.dayno {{ background: {INK}; color: #fff; font-size: 9pt; border-radius: 1.5mm; padding: .6mm 2mm; }}
.stops {{ list-style: none; padding: 0; margin: 0; }}
.stops li {{ display: grid; grid-template-columns: 16mm 1fr; gap: 3mm; padding: 2mm 0; break-inside: avoid; }}
.time {{ font-weight: 700; color: {INK2}; font-variant-numeric: tabular-nums; }}
.meta {{ color: {INK2}; font-size: 9.5pt; }}
.links {{ font-size: 9pt; margin-top: .8mm; }}
.badge {{ display: inline-block; background: {ACCENT}; color: #141414; font-size: 8pt; font-weight: 700;
  border-radius: 1.2mm; padding: .3mm 1.6mm; text-transform: uppercase; letter-spacing: .04em; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 4mm; }}
.item {{ border: .3mm solid {GRID}; border-radius: 2.5mm; padding: 4mm; break-inside: avoid; }}
.item.chosen {{ border: .6mm solid {ACCENT}; }}
.item .badge {{ float: right; }}
.item.shortlisted .badge {{ background: #ffd23f; }}
.item b {{ display: block; font-size: 11.5pt; margin-bottom: 1mm; }}
.item p {{ margin: 0 0 1.5mm; }}
.by {{ color: {MUTED}; }}
.list th {{ text-align: left; color: {INK2}; border-bottom: .4mm solid #c3c2b7; padding: 1.5mm 2mm; font-size: 9pt; }}
.list td {{ padding: 1.8mm 2mm; border-bottom: .2mm solid {GRID}; vertical-align: top; }}
.n, .list th.n {{ text-align: right; white-space: nowrap; }}
ul.plain {{ padding-left: 5mm; margin: 0; }} ul.plain li {{ margin: 0 0 1.5mm; }}
.foot {{ color: {MUTED}; font-size: 8.5pt; margin-top: 8mm; }}
"""


def build_html(trip: dict) -> str:
    plan = pl.load_plan(trip)
    body = cover(trip, plan) + glance(trip, plan) + itinerary(trip, plan) + catalogue(trip, plan) + logistics(trip, plan)
    foot = ("<p class='foot'>Made by the trip bot from the group's plan. Prices, hours and availability change; "
            "check before booking. Maps links open Google Maps.</p>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(trip['name'])} catalogue</title>"
            f"<style>{CSS}</style></head><body>{body}{foot}</body></html>")


def pdf_name(trip: dict) -> str:
    return f"{sl.trip_today(trip).replace('-', '.')}_{trip['name']} Trip Catalogue.pdf"


def render(trip: dict) -> Path:
    d = rp.out_dir(trip)
    src, pdf = d / "catalogue.html", d / pdf_name(trip)
    src.write_text(build_html(trip), encoding="utf-8")
    env = dict(os.environ)
    if "NODE_PATH" not in env:
        env["NODE_PATH"] = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["node", str(HERE / "render_pdf.js"), str(src), str(pdf)],
                   check=True, capture_output=True, text=True, env=env)
    return pdf


def caption(trip: dict) -> str:
    plan = pl.load_plan(trip)
    days = len([d for d, v in (plan.get("itinerary") or {}).items() if v])
    ideas = len([i for i in plan.get("ideas") or [] if i.get("status") != "dropped"])
    bits = [f"{days} day{'s' * (days != 1)} planned" if days else "no days planned yet",
            f"{ideas} place{'s' * (ideas != 1)} on the shortlist"]
    return f"{trip['name']} trip catalogue: " + ", ".join(bits) + "."
