#!/usr/bin/env python3
"""Where people are, and how far suggested places are, for on-trip requests.

Locations shared in the chat are kept only in ~/.splitbot/locations.json
(never the repo) and expire after an hour. Lookups use OpenStreetMap's free
services (Nominatim to name an area, Photon to find a venue); no API key.

  places.py remember --chat ID --user TGID --lat LAT --lon LON
      store the sender's location and print the area name
  places.py last --chat ID --user TGID [--max-age 3600]
      the sender's last location (or anyone's in that chat) if still fresh
  places.py locate --lat LAT --lon LON --name "Venue" [--area "District 1"]
      distance and a Google Maps link; the distance only when the venue
      match is confident (name matches and it's within 15 km)
  places.py suggest --chat ID --user TGID --name "Ben" --request "dessert"
                    [--lat LAT --lon LON] < places.jsonl
      for each researched place (JSON lines: name, area_or_street, why,
      price, hours_today): locate it, add walking and ride times from the
      asker, print the reply lines, and log the set for the trip app
      (trips/data/places/<trip>.json; venue positions only, never people's)

Travel times come from OpenStreetMap routing (routing.openstreetmap.de, no
key). Ride time is the car route without traffic, padded for pickup and
city traffic, so it is a rough Grab estimate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlencode

UA = "personal-trip-bot/1.0 (private use)"
MAX_KM = 15
KEEP_SETS = 40          # suggestion sets kept per trip for the app


def home() -> Path:
    return Path(os.environ.get("SPLITBOT_HOME", Path.home() / ".splitbot"))


def store() -> Path:
    return home() / "locations.json"


def get_json(url: str) -> object:
    out = subprocess.run(["curl", "-sS", "--max-time", "15", "-A", UA, url],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance."""
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def fold(s: str) -> set[str]:
    """Lower-case words without accents, for comparing venue names."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", s)
            if w not in {"the", "bar", "cafe", "restaurant", "and", "of", "quan", "nha", "hang"}}


def same_place(wanted: str, found: str) -> bool:
    w, f = fold(wanted), fold(found)
    return bool(w) and len(w & f) / len(w) >= 0.6


def maps_link(name: str, area: str = "") -> str:
    return "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": ", ".join(x for x in (name, area) if x)})


def area_name(lat: float, lon: float) -> str:
    try:
        d = get_json(f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=jsonv2&zoom=16")
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return ""
    a = d.get("address", {}) if isinstance(d, dict) else {}
    parts = [a.get(k) for k in ("road", "suburb", "quarter", "city_district", "city", "town")]
    return ", ".join(dict.fromkeys(p for p in parts if p))


def load() -> dict:
    try:
        return json.loads(store().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def remember(chat: str, user: str, lat: float, lon: float, now: float | None = None) -> dict:
    data = load()
    now = time.time() if now is None else now
    data = {k: v for k, v in data.items() if now - v["at"] < 6 * 3600}  # keep the file small
    entry = {"lat": lat, "lon": lon, "at": now, "area": area_name(lat, lon)}
    data[f"{chat}:{user}"] = entry
    home().mkdir(parents=True, exist_ok=True)
    store().write_text(json.dumps(data))
    os.chmod(store(), 0o600)
    return entry


def last(chat: str, user: str | None, max_age: int = 3600, now: float | None = None) -> dict | None:
    """The user's last fresh location in this chat, else the freshest anyone shared there."""
    now = time.time() if now is None else now
    data = {k: v for k, v in load().items() if now - v["at"] <= max_age}
    if user and f"{chat}:{user}" in data:
        return {**data[f"{chat}:{user}"], "whose": "yours"}
    mine = [v for k, v in data.items() if k.startswith(f"{chat}:")]
    return {**max(mine, key=lambda v: v["at"]), "whose": "someone else's"} if mine else None


def locate(lat: float, lon: float, name: str, area: str = "") -> dict:
    out = {"name": name, "maps": maps_link(name, area), "km": None}
    # Name alone first (results are biased to the asker's position), then with
    # the street or area: extra words often make the search miss.
    for text in dict.fromkeys(x for x in (name, f"{name} {area}".strip()) if x):
        try:
            feats = get_json(f"https://photon.komoot.io/api/?q={quote(text)}&lat={lat}&lon={lon}&limit=5"
                             ).get("features", [])
        except (subprocess.CalledProcessError, json.JSONDecodeError, AttributeError):
            continue
        for f in feats:
            props, (flon, flat) = f.get("properties", {}), f["geometry"]["coordinates"]
            d = km(lat, lon, flat, flon)
            if same_place(name, props.get("name", "")) and d <= MAX_KM:
                out["km"] = round(d, 1)
                out["lat"], out["lon"] = flat, flon
                out["maps"] = f"https://www.google.com/maps/search/?api=1&query={flat},{flon}"
                return out
    return out


def route(lat: float, lon: float, vlat: float, vlon: float) -> dict:
    """Walking time and a rough ride time; empty if routing is unavailable."""
    out = {}
    for profile, key in (("routed-foot", "walk"), ("routed-car", "car")):
        try:
            r = get_json(f"https://routing.openstreetmap.de/{profile}/route/v1/driving/"
                         f"{lon},{lat};{vlon},{vlat}?overview=false")["routes"][0]
            out[f"{key}_min"] = r["duration"] / 60
            out[f"{key}_km"] = r["distance"] / 1000
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            pass
    res = {}
    if "walk_min" in out:
        res["walk_min"] = max(1, round(out["walk_min"]))
        res["walk_km"] = round(out["walk_km"], 1)
    if "car_min" in out:
        res["ride_min"] = max(3, round(out["car_min"] * 1.6 + 3))  # pickup + city traffic
    return res


def line(i: int, p: dict) -> str:
    bits = [f"{i}. {p['name']}"]
    if p.get("km") is not None:
        bits.append(f"{p.get('walk_km', p['km'])} km")
    if p.get("walk_min") and p["walk_min"] <= 30:
        bits.append(f"🚶 {p['walk_min']} min")
    if p.get("ride_min"):
        bits.append(f"🛵 ~{p['ride_min']} min")
    bits += [x for x in (p.get("why"), p.get("price"), p.get("hours_today")) if x]
    return " · ".join(bits) + f"\n   {p['maps']}"


def plan_store(trip: dict) -> Path:
    import splitlog
    d = splitlog.data_dir() / "places"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{trip['id']}.json"


def suggest(chat: str, user: str, name: str, request: str, items: list[dict],
            lat: float | None = None, lon: float | None = None) -> str:
    import splitlog
    here = None
    if lat is not None and lon is not None:
        here = {"lat": lat, "lon": lon, "area": area_name(lat, lon)}
    else:
        here = last(chat, user)
    out = []
    for it in items[:3]:
        p = {"name": it.get("name", "?"), "area": it.get("area_or_street", ""),
             "why": it.get("why", ""), "price": it.get("price", ""), "hours_today": it.get("hours_today", "")}
        if here:
            p.update(locate(here["lat"], here["lon"], p["name"], p["area"]))
            if p.get("lat") is not None:
                p.update(route(here["lat"], here["lon"], p["lat"], p["lon"]))
        else:
            p["maps"], p["km"] = maps_link(p["name"], p["area"]), None
        out.append(p)
    known = [p for p in out if p.get("km") is not None]
    out = sorted(known, key=lambda p: p.get("walk_min", p["km"] * 12)) + [p for p in out if p.get("km") is None]
    try:
        trip = splitlog.open_trip(chat)
        f = plan_store(trip)
        sets = json.loads(f.read_text()) if f.exists() else []
        sets.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "asked_by": name, "request": request,
                     "near": (here or {}).get("area", ""),
                     "places": [{k: p.get(k) for k in ("name", "area", "why", "price", "hours_today", "km",
                                                       "walk_km", "walk_min", "ride_min", "maps", "lat", "lon")}
                                for p in out]})
        f.write_text(json.dumps(sets[-KEEP_SETS:], ensure_ascii=False, indent=1) + "\n")
    except splitlog.SplitError:
        pass  # the owner's private chat with no trip: nothing to log
    return "\n".join(line(i, p) for i, p in enumerate(out, 1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("remember")
    r.add_argument("--chat", required=True); r.add_argument("--user", required=True)
    r.add_argument("--lat", type=float, required=True); r.add_argument("--lon", type=float, required=True)
    l_ = sub.add_parser("last")
    l_.add_argument("--chat", required=True); l_.add_argument("--user")
    l_.add_argument("--max-age", type=int, default=3600)
    lo = sub.add_parser("locate")
    lo.add_argument("--lat", type=float, required=True); lo.add_argument("--lon", type=float, required=True)
    lo.add_argument("--name", required=True); lo.add_argument("--area", default="")
    sg = sub.add_parser("suggest")
    sg.add_argument("--chat", required=True); sg.add_argument("--user", required=True)
    sg.add_argument("--name", default=""); sg.add_argument("--request", default="")
    sg.add_argument("--lat", type=float); sg.add_argument("--lon", type=float)
    a = ap.parse_args(argv)
    if a.cmd == "remember":
        e = remember(a.chat, a.user, a.lat, a.lon)
        print(f"location saved for an hour; area: {e['area'] or 'unknown'}")
    elif a.cmd == "last":
        e = last(a.chat, a.user, a.max_age)
        if not e:
            print("no fresh location: ask them to share one (📎 → Location)")
            return 1
        mins = int((time.time() - e["at"]) / 60)
        print(json.dumps({"lat": e["lat"], "lon": e["lon"], "area": e["area"],
                          "whose": e["whose"], "minutes_ago": mins}, ensure_ascii=False))
    elif a.cmd == "locate":
        print(json.dumps(locate(a.lat, a.lon, a.name, a.area), ensure_ascii=False))
    else:
        items = []
        for raw in sys.stdin.read().splitlines():
            raw = raw.strip().rstrip(",")
            if raw.startswith("{"):
                try:
                    items.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        if not items:
            print("error: no places on stdin (JSON lines)")
            return 1
        print(suggest(a.chat, a.user, a.name, a.request, items, a.lat, a.lon))
    return 0


if __name__ == "__main__":
    sys.exit(main())
