#!/usr/bin/env python3
"""Bill-split bot listener: wait for relevant group messages, then exit.

Long-polls getUpdates on the split bot (SPLIT_BOT_TOKEN). The process is curl,
not a model, so it costs nothing while idle. When relevant messages arrive it
saves each one as an event in the inbox (receipt photos downloaded beside it),
shows "typing" in the group, prints the events and exits 0. Run it in the
background: the harness wakes the agent with this output. The agent logs each
payment, replies, deletes the photo (splitlog.py done) and relaunches the
listener.

The inbox is the durable queue. If the session dies, the next start prints
the leftover events first. The offset only advances once a batch is safely in
the inbox, and Telegram keeps updates for 24 hours, so payments sent while the
session was down are still logged, on the day they were sent.

Exit codes: 0 events printed · 6 another listener is running · 3 deadline reached · 4 owner sent /stopbot · 2 config ·
5 the bot's code changed on disk (a git pull brought an update): relaunch to run it.

SECURITY: a message is accepted only if
  - it comes from a group that has an open trip (trips/data/trips.json), or
  - it comes from a group the owner (SPLIT_OWNER_ID, default TELEGRAM_CHAT_ID)
    is a member of (checked with getChatMember, cached for an hour), or
  - it was sent by the owner.
Everything else is dropped without being echoed, so a stranger who adds the
bot to their own group cannot wake the agent. Inside a trip group anyone
can log a payment; their text is untrusted input that can only become ledger
entries, never other actions.

Ordinary chatter is dropped here too, so the agent only wakes for messages
that could be payments or bot commands: a photo, a number, a /command, a
mention of the bot, a reply to the bot, or a keyword (balances, settle...).
Groups whose trip is in its planning phase (see splitlog.phase) also wake it
for planning words (poll, vote, idea, itinerary, booking, catch us up...).

Planning-phase groups: every text message is appended to a local rolling log,
~/.splitbot/chatlog/<chat_id>.jsonl (last 400 lines; name, text, date only),
without waking the agent, so "catch us up" and "what did we decide" can be
answered. The log is never committed and is lost when the run's container
restarts. Votes on the bot's polls (poll_answer updates, which carry no chat)
are mapped to their group through trips/data/plans/*.json; votes on polls not
in a plan are dropped. They are emitted as events with "kind": "poll_answer".

Trip app changes: when trips/webapp/config.json has the app's url, each loop
also asks the Worker (signed, short timeout, at most every APP_POLL_EVERY
seconds) for changes people asked for in the app: rename a member, add a
member. They are emitted as events with "kind": "app_action"; the session
applies them with splitlog.py and acks them (webapp_sync.py --ack). The
Worker already checked who may do what; this rechecks it against the repo's
members and sets "refused" when it doesn't hold. A Worker that is down or slow
never holds up Telegram polling. Not used when SPLIT_DATA points elsewhere
(tests) unless SPLITBOT_APP_CONFIG names a config.

Usage: listen.py [deadline_seconds]
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import splitlog  # noqa: E402  (phase(): which trips are in planning)
import transcribe  # noqa: E402  (voice notes -> text, locally)
import webapp_sync  # noqa: E402  (the trip app's signed change queue)

POLL_TIMEOUT = 45
KEYWORDS = re.compile(r"\b(bot|balances?|settle[sd]?|owes?|owed|undo|delete|remove|"
                      r"summary|totals?|help|split|paid|pay|join|list|fix|wrong|endtrip|newtrip|trip|"
                      r"excel|xlsx|csv|spreadsheets?|sheets?|log|charts?|graphs?|visuali[sz]\w*|"
                      r"plots?|breakdown|reports?|wrapped|pdf|recap|stats|spen[dt]\w*|costs?|how much|"
                      r"dashboard|(?:trip|mini|web) ?app|open (?:the )?app)\b", re.I)
# Extra wake words for groups whose trip is in its planning phase only, so a
# live trip's chatter ("what's the plan tonight") doesn't wake the agent.
PLAN_KEYWORDS = re.compile(r"\b(plans?|planning|polls?|votes?|voted|voting|ideas?|to-?dos?|itinerar(?:y|ies)|"
                           r"bookings?|booked|suggest(?:ions?|ed)?|recommend\w*|catch (?:us|me) up|"
                           r"decided|decisions?|budget|dates|catalog(?:ue)?s?|brochure|travel guide|day by day)\b", re.I)
# On-trip requests ("dessert near me", "where should we eat") in any trip group.
ASK_KEYWORDS = re.compile(r"\b(near\s?(?:by|me|us|here)|around here|recommend\w*|suggest\w*|"
                          r"where (?:should|can|to|do) (?:we|i)|find (?:us|me|a|some)|"
                          r"open now|good place|best (?:place|spot))\b", re.I)
CHATLOG_LINES = 400
APP_POLL_EVERY = 20      # seconds between checks of the trip app's change queue
APP_TIMEOUT = 8          # seconds; never let the app hold up Telegram polling
APP_REACK_AFTER = 120    # re-send an ack the Worker still lists after this long
APP_ACTIONS = ("rename_member", "add_member")
ROOT = Path(__file__).resolve().parents[2]


def home() -> Path:
    return Path(os.environ.get("SPLITBOT_HOME", Path.home() / ".splitbot"))


def bot_token() -> str:
    """SPLIT_BOT_TOKEN from the environment, else ~/.splitbot/token (outside the repo)."""
    tok = os.environ.get("SPLIT_BOT_TOKEN", "")
    f = home() / "token"
    if not tok and f.exists():
        tok = f.read_text().strip()
    return tok


def owner_id() -> str:
    return os.environ.get("SPLIT_OWNER_ID") or os.environ.get("TELEGRAM_CHAT_ID") or ""


def base() -> str:
    return os.environ.get("SPLITBOT_API", "https://api.telegram.org")  # override for tests


def inbox() -> Path:
    p = home() / "inbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def trip_chats() -> set[str]:
    p = Path(os.environ.get("SPLIT_DATA", ROOT / "trips" / "data")) / "trips.json"
    if not p.exists():
        return set()
    return {t["chat_id"] for t in json.loads(p.read_text())["trips"] if t["status"] == "open"}


def data_dir() -> Path:
    return Path(os.environ.get("SPLIT_DATA", ROOT / "trips" / "data"))


def open_trips() -> list[dict]:
    p = data_dir() / "trips.json"
    return [t for t in json.loads(p.read_text())["trips"] if t["status"] == "open"] if p.exists() else []


def planning_chats() -> set[str]:
    return {t["chat_id"] for t in open_trips() if splitlog.phase(t) == "planning"}


def known_polls() -> dict[str, dict]:
    """Open polls the bot sent, from the plans: poll_id -> {chat_id, n}."""
    out = {}
    for t in open_trips():
        p = data_dir() / "plans" / f"{t['id']}.json"
        if not p.exists():
            continue
        try:
            polls = json.loads(p.read_text()).get("polls") or []
        except json.JSONDecodeError:
            continue
        for q in polls:
            if q.get("poll_id") and q.get("status") == "open":
                out[str(q["poll_id"])] = {"chat_id": t["chat_id"], "n": q["n"]}
    return out


def curl_json(url: str, max_time: int) -> dict:
    out = subprocess.run(["curl", "-sS", "--max-time", str(max_time), url],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def person(u: dict) -> dict:
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username") or "?"
    return {"id": str(u.get("id")), "name": name, "username": u.get("username")}


def relevant(m: dict, text: str, file_id, bot: dict, planning: bool = False) -> bool:
    if file_id or m.get("new_chat_members") or m.get("migrate_to_chat_id"):
        return True
    if m.get("_voice"):
        return True  # every voice note reaches the bot: spoken payments rarely use digits
    if m.get("location"):
        return True  # a shared location: used for "near me" requests
    if not text:
        return False
    reply = m.get("reply_to_message") or {}
    if str((reply.get("from") or {}).get("id")) == str(bot.get("id")):
        return True
    uname = (bot.get("username") or "").lower()
    return (text.startswith("/") or any(c.isdigit() for c in text)
            or (uname and f"@{uname}" in text.lower()) or bool(KEYWORDS.search(text))
            or bool(ASK_KEYWORDS.search(text))
            or (planning and bool(PLAN_KEYWORDS.search(text))))


def poll_event(u: dict, owner: str, polls: dict[str, dict]) -> dict | None:
    """A vote on one of the bot's polls, or None if the poll isn't in a plan."""
    pa = u.get("poll_answer") or {}
    known = polls.get(str(pa.get("poll_id")))
    user = pa.get("user")
    if not known or not user:
        return None
    return {"update_id": u["update_id"], "kind": "poll_answer", "chat_id": known["chat_id"],
            "chat_title": None, "chat_type": None, "message_id": None, "date": None,
            "from": {**person(user), "is_owner": bool(owner) and str(user.get("id")) == str(owner)},
            "text": "", "file_id": None, "reply_to": None, "joined": None, "migrated_to": None,
            "poll_id": str(pa["poll_id"]), "poll_n": known["n"], "option_ids": pa.get("option_ids") or []}


def chatlog_entries(resp: dict, planning: set[str]) -> dict[str, list[dict]]:
    """Text messages from planning-phase groups, for the rolling chat log. Pure."""
    out: dict[str, list[dict]] = {}
    for u in resp.get("result") or []:
        m = u.get("message") or {}
        cid = str((m.get("chat") or {}).get("id"))
        text = (m.get("text") or m.get("caption") or "").strip()
        sender = m.get("from") or {}
        if cid in planning and text and not sender.get("is_bot"):
            out.setdefault(cid, []).append({"name": person(sender)["name"], "text": text[:2000],
                                            "date": m.get("date")})
    return out


def append_chatlog(entries: dict[str, list[dict]]) -> None:
    """Append to ~/.splitbot/chatlog/<chat>.jsonl, keeping the last CHATLOG_LINES lines."""
    d = home() / "chatlog"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    for cid, items in entries.items():
        p = d / f"{cid}.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        lines += [json.dumps(e, ensure_ascii=False) for e in items]
        tmp = p.with_suffix(".tmp")
        tmp.write_text("\n".join(lines[-CHATLOG_LINES:]) + "\n", encoding="utf-8")
        tmp.replace(p)


def parse_updates(resp: dict, owner: str, chats: set[str], bot: dict,
                  polls: dict[str, dict] | None = None,
                  planning: set[str] | frozenset = frozenset()) -> tuple[int | None, list[dict]]:
    """Return (next offset, accepted events). Pure; no I/O."""
    if not resp.get("ok"):
        return None, []
    results = resp.get("result") or []
    if not results:
        return None, []
    last = max(u.get("update_id", 0) for u in results)
    events = []
    for u in results:
        if "poll_answer" in u:
            ev = poll_event(u, owner, polls or {})
            if ev:
                events.append(ev)
            continue
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        sender = m.get("from") or {}
        chat_id = str(chat.get("id"))
        from_owner = bool(owner) and str(sender.get("id")) == str(owner)
        if chat_id not in chats and not from_owner:
            continue  # not a trip group and not the owner: drop, never echo
        if sender.get("is_bot") and not m.get("new_chat_members"):
            continue
        file_id = None
        if m.get("photo"):
            file_id = m["photo"][-1]["file_id"]  # largest size
        elif (m.get("document") or {}).get("mime_type", "").startswith("image/"):
            file_id = m["document"]["file_id"]
        text = (m.get("caption") or m.get("text") or "").strip()
        if not relevant(m, text, file_id, bot, chat_id in planning or (from_owner and chat.get("type") == "private")):
            continue
        reply = m.get("reply_to_message") or {}
        events.append({
            "update_id": u["update_id"],
            "chat_id": chat_id,
            "chat_title": chat.get("title") or "private",
            "chat_type": chat.get("type"),
            "message_id": m.get("message_id"),
            "date": m.get("date"),
            "from": {**person(sender), "is_owner": from_owner},
            "text": text,
            "voice": m.get("_voice"),
            "location": ({"lat": m["location"]["latitude"], "lon": m["location"]["longitude"],
                          "live": bool(m["location"].get("live_period")),
                          "venue": (m.get("venue") or {}).get("title")}
                         if m.get("location") else None),
            "file_id": file_id,
            "reply_to": ({"message_id": reply.get("message_id"),
                          "from_bot": str((reply.get("from") or {}).get("id")) == str(bot.get("id")),
                          "text": (reply.get("text") or reply.get("caption") or "")[:500]}
                         if reply else None),
            "joined": [person(x) for x in m.get("new_chat_members") or []
                       if str(x.get("id")) != str(bot.get("id"))] or None,
            "migrated_to": str(m["migrate_to_chat_id"]) if m.get("migrate_to_chat_id") else None,
        })
    return last + 1, events


VOICE_KEYS = ("voice", "audio", "video_note")


def stt_hint() -> str:
    """Words the speech model should expect: members' names, money words and
    what the trip has been paying for (place names it would otherwise mangle)."""
    names, ccys, descs = [], set(), []
    for t in open_trips():
        names += [m["name"] for m in t.get("members", [])]
        ccys.add(t.get("currency", ""))
        try:
            descs += [r["desc"] for r in splitlog.trip_rows(t)][-25:]
        except (OSError, KeyError):
            pass
    hint = ("Trip expenses. " + ", ".join(dict.fromkeys(names)) + ". "
            + " ".join(sorted(c for c in ccys if c)) + " SGD dollars k Grab dinner paid split. ")
    return (hint + ", ".join(dict.fromkeys(reversed(descs))))[:600]


def transcribe_voices(api: str, resp: dict, owner: str, chats: set[str]) -> None:
    """Turn voice notes from accepted chats into text, in place, before filtering.

    Each note is downloaded, transcribed locally and deleted at once; only the
    text goes on (as the message text, so the usual filter drops chatter).
    The message gets `_voice` = {"duration", "failed"}.
    """
    for u in resp.get("result") or []:
        m = u.get("message") or {}
        key = next((k for k in VOICE_KEYS if m.get(k)), None)
        if not key or m.get("text"):
            continue
        chat_id = str((m.get("chat") or {}).get("id"))
        from_owner = bool(owner) and str((m.get("from") or {}).get("id")) == str(owner)
        if (chat_id not in chats and not from_owner) or (m.get("from") or {}).get("is_bot"):
            continue
        media, text, dest = m[key], None, None
        dur = int(media.get("duration") or 0)
        try:
            if dur <= transcribe.MAX_SECONDS:
                info = curl_json(f"{api}/getFile?file_id={media['file_id']}", 30)
                fpath = info["result"]["file_path"]
                dest = inbox() / f"voice-{u['update_id']}{Path(fpath).suffix or '.ogg'}"
                subprocess.run(["curl", "-sS", "--fail", "--max-time", "60", "-o", str(dest),
                                f"{base()}/file/bot{bot_token()}/{fpath}"], check=True)
                text = transcribe.transcribe(str(dest), stt_hint())
        except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError) as e:
            print(f"listen: voice note not fetched: {e}", file=sys.stderr)
        finally:
            if dest:
                dest.unlink(missing_ok=True)  # never keep the audio
        caption = (m.get("caption") or "").strip()
        m["text"] = " ".join(x for x in (caption, text or "") if x)
        m.pop("caption", None)
        m["_voice"] = {"duration": dur, "failed": not text,
                       **({"too_long": True} if dur > transcribe.MAX_SECONDS else {})}


# ------------------------------------------------------------- trip app changes
def app_config() -> dict | None:
    if os.environ.get("SPLIT_DATA") and not os.environ.get("SPLITBOT_APP_CONFIG"):
        return None  # tests or other data: never the real Worker
    try:
        cfg = webapp_sync.load_config()
    except (OSError, ValueError):
        return None
    return cfg if cfg and cfg.get("url") else None


def all_trips() -> list[dict]:
    p = data_dir() / "trips.json"
    try:
        return json.loads(p.read_text())["trips"] if p.exists() else []
    except (OSError, ValueError, KeyError):
        return []


def good_name(v) -> bool:
    """A name from the app, as the Worker allows it: 1-31 letters or digits, plus
    spaces and . ' - , starting with a letter or digit. Nothing a shell or
    splitlog's name lists would read specially."""
    if not isinstance(v, str) or not 1 <= len(v) <= 31 or v.strip() != v or not v[0].isalnum():
        return False
    return all(c.isalnum() or c in " .'-" or unicodedata.category(c).startswith("M") for c in v)


def app_event(a: dict, trips: list[dict], owner: str) -> dict:
    """One queued app change as an event. Pure. Sets "refused" (a reason) when
    it fails the local checks: the Worker checked the same against its snapshot,
    but the repo is the record."""
    by = a.get("by") if isinstance(a.get("by"), dict) else {}
    by_id = str(by.get("tg_id") or "")
    is_owner = bool(owner) and by_id == str(owner)
    trip = next((t for t in trips if t["id"] == a.get("trip_id")), None)
    me = next((m for m in (trip or {}).get("members", []) if by_id and str(m.get("tg_id") or "") == by_id), None)
    action, name, to = a.get("action"), a.get("name"), a.get("to")
    ev = {"kind": "app_action", "action_id": a["id"], "action": action,
          "trip_id": a.get("trip_id"), "chat_id": trip["chat_id"] if trip else None,
          "chat_title": trip["name"] if trip else None, "trip_status": trip["status"] if trip else None,
          "by": {"id": by_id, "name": me["name"] if me else (by.get("name") if good_name(by.get("name")) else "Someone"),
                 "is_owner": is_owner},
          "name": name, "to": to if action == "rename_member" else None, "at": a.get("at")}
    refused = None
    members = [m["name"] for m in (trip or {}).get("members", [])]
    # The new name must pass the app rule; the old one may also be any name already on the trip.
    if (action not in APP_ACTIONS or not isinstance(name, str)
            or (action == "add_member" and not good_name(name))
            or (action == "rename_member" and not (good_name(to) and (good_name(name) or name in members)))):
        refused = "malformed request"
    elif not trip:
        refused = "no such trip here"
    elif trip["status"] != "open":
        refused = "the trip is closed"
    elif not is_owner and not me:
        refused = "the requester isn't on this trip"
    elif action == "add_member" and not is_owner:
        refused = "only the owner can add people"
    elif action == "rename_member" and not is_owner and me["name"] != name:
        refused = f"{me['name']} can only rename themselves"
    if refused:
        ev["refused"] = refused
    return ev


class AppPoller:
    """Checks the trip app's change queue at most every APP_POLL_EVERY seconds.
    Never raises; reports the first failure only, so a down Worker doesn't
    flood the session with lines."""

    def __init__(self, token: str, owner: str):
        self.token, self.owner, self.last, self.warned = token, owner, 0.0, False

    def poll(self) -> list[dict]:
        now = time.time()
        if now - self.last < APP_POLL_EVERY:
            return []
        self.last = now
        cfg = app_config()
        if not cfg:
            return []
        try:
            actions = webapp_sync.fetch_actions(cfg, self.token, APP_TIMEOUT)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            if not self.warned:
                print(f"listen: trip app queue not reached ({e}); will keep trying quietly", file=sys.stderr)
                self.warned = True
            return []
        done = webapp_sync.acked()
        stale = [a["id"] for a in actions if a["id"] in done and now - done[a["id"]] > APP_REACK_AFTER]
        if stale:  # handled here, but the Worker didn't get (or keep) the ack: send it again
            try:
                webapp_sync.ack(cfg, self.token, stale, APP_TIMEOUT)
            except (OSError, subprocess.SubprocessError):
                pass
        trips = all_trips()
        return [app_event(a, trips, self.owner) for a in actions if a["id"] not in done]


def save_event(api: str, ev: dict) -> dict:
    """Download the photo (if any) and write the event JSON into the inbox."""
    box = inbox()
    ev = dict(ev)
    file_id = ev.pop("file_id")
    ev["photo"] = None
    if file_id:
        info = curl_json(f"{api}/getFile?file_id={file_id}", 30)
        fpath = info["result"]["file_path"]
        dest = box / f"{ev['update_id']}{Path(fpath).suffix or '.jpg'}"
        subprocess.run(["curl", "-sS", "--fail", "--max-time", "60", "-o", str(dest),
                        f"{base()}/file/bot{bot_token()}/{fpath}"], check=True)
        ev["photo"] = str(dest)
    tmp = box / f"{ev['update_id']}.json.tmp"
    tmp.write_text(json.dumps(ev, ensure_ascii=False))
    tmp.rename(box / f"{ev['update_id']}.json")
    return ev


def pending() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(inbox().glob("*.json"),
                                                      key=lambda p: int(p.stem))]


def emit(events: list[dict]) -> None:
    print("=== SPLIT BOT EVENTS ===")
    for ev in events:
        print(json.dumps(ev, ensure_ascii=False))


def whoami(api: str) -> dict:
    """The bot's id, username and whether privacy mode lets it read the group."""
    cache = home() / "me.json"
    try:
        r = curl_json(f"{api}/getMe", 20).get("result") or {}
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return json.loads(cache.read_text()) if cache.exists() else {}
    me = {"id": r.get("id"), "username": r.get("username"),
          "can_read_all_group_messages": r.get("can_read_all_group_messages")}
    if me["id"]:
        home().mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(me))
    return me


GROUP_TTL = 3600


def owner_groups(api: str, owner: str, resp: dict, known: set[str]) -> set[str]:
    """Group chats in this batch that the owner is a member of (cached an hour)."""
    cache_p = home() / "groups.json"
    cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    now = time.time()
    out = set()
    for u in resp.get("result") or []:
        chat = (u.get("message") or {}).get("chat") or {}
        cid = str(chat.get("id"))
        if chat.get("type") not in ("group", "supergroup") or cid in known or cid in out:
            continue
        hit = cache.get(cid)
        if not hit or now - hit["at"] > GROUP_TTL:
            try:
                st = curl_json(f"{api}/getChatMember?chat_id={cid}&user_id={owner}", 20)
                ok = (st.get("result") or {}).get("status") in ("creator", "administrator", "member", "restricted")
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                ok = False
            hit = cache[cid] = {"ok": ok, "at": now}
        if hit["ok"]:
            out.add(cid)
    home().mkdir(parents=True, exist_ok=True)
    cache_p.write_text(json.dumps(cache))
    return out


def code_stamp() -> tuple:
    """Modification times of the bot's code; a change means a pull brought an update."""
    here = Path(__file__).resolve().parent
    return tuple(f.stat().st_mtime_ns for f in sorted(here.glob("*.py")) if not f.name.startswith("test_"))


def single_instance():
    """Hold an exclusive lock for this process's lifetime, or return None if
    another listener has it. Two listeners would both receive the same
    message, and the bot would answer it twice."""
    home().mkdir(parents=True, exist_ok=True)
    fh = open(home() / "listen.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def main() -> int:
    token, owner = bot_token(), owner_id()
    if not token or not owner:
        print("listen: SPLIT_BOT_TOKEN (or ~/.splitbot/token) and SPLIT_OWNER_ID/TELEGRAM_CHAT_ID must be set",
              file=sys.stderr)
        return 2
    lock = single_instance()
    if lock is None:
        print("listen: another listener is already running; not starting a second one", file=sys.stderr)
        return 6
    api = f"{base()}/bot{token}"
    deadline = time.time() + int(sys.argv[1]) if len(sys.argv) > 1 else None
    state = home() / "offset"

    left = pending()
    if left:
        emit(left)
        return 0

    bot = whoami(api)
    offset = int(state.read_text()) if state.exists() else 0
    code = code_stamp()
    app = AppPoller(token, owner)
    while True:
        if code_stamp() != code:
            print("listen: code updated on disk; relaunch", file=sys.stderr)
            return 5
        if deadline and time.time() >= deadline:
            print("listen: deadline reached", file=sys.stderr)
            return 3
        acts = app.poll()  # before the long poll, so a change waits at most one poll
        if acts:
            emit(acts)
            return 0
        try:
            resp = curl_json(f"{api}/getUpdates?timeout={POLL_TIMEOUT}&offset={offset}"
                             "&allowed_updates=%5B%22message%22%2C%22poll_answer%22%5D", POLL_TIMEOUT + 15)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            time.sleep(3)
            continue
        chats = trip_chats()
        chats |= owner_groups(api, owner, resp, chats)
        transcribe_voices(api, resp, owner, chats)
        planning = planning_chats()
        nxt, events = parse_updates(resp, owner, chats, bot, known_polls(), planning)
        if nxt is None:
            continue
        stop = [e for e in events if e["from"]["is_owner"] and not e["file_id"]
                and e["text"].lower().split("@")[0] == "/stopbot"]
        try:
            saved = [save_event(api, e) for e in events if e not in stop]
        except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError):
            time.sleep(3)
            continue  # offset not advanced, so the batch is fetched again
        try:
            append_chatlog(chatlog_entries(resp, planning))
        except OSError as e:  # the log is a convenience; never stall the bot over it
            print(f"listen: chat log not written: {e}", file=sys.stderr)
        home().mkdir(parents=True, exist_ok=True)
        state.write_text(str(nxt))
        offset = nxt
        for chat in {e["chat_id"] for e in saved if e.get("kind") != "poll_answer"}:
            subprocess.run(["curl", "-sS", "--max-time", "10", "-o", "/dev/null",
                            f"{api}/sendChatAction?chat_id={chat}&action=typing"])
        if saved:
            emit(saved)
        if stop:
            print("=== STOP REQUESTED ===")
            return 4
        if saved:
            return 0


if __name__ == "__main__":
    sys.exit(main())
