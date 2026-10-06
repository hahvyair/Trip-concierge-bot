#!/usr/bin/env python3
"""Send a plain-text reply from the split bot. Text comes on stdin.

Usage: printf '%s' "$reply" | python3 trips/splitbot/send.py --chat ID [--reply-to MESSAGE_ID]
       python3 trips/splitbot/send.py --chat ID --member-count     (people in the group, incl. the bot)
       python3 trips/splitbot/send.py --status                     (bot username, privacy mode)
       python3 trips/splitbot/send.py --chat ID --photo chart.png [--caption TEXT]
       python3 trips/splitbot/send.py --chat ID --document trip.xlsx [--caption TEXT]
       python3 trips/splitbot/send.py --chat ID --poll "Where to stay?" --option Namba --option Umeda [--multi]
           (a non-anonymous poll; prints "send: ok (message N) poll_id=ID")
       python3 trips/splitbot/send.py --chat ID --stop-poll MESSAGE_ID
       printf '%s' "$text" | python3 trips/splitbot/send.py --chat ID --button-text "Open the trip app" \
           --button-url "https://t.me/<bot>/<app>?startapp=<trip id>"   (text with one inline link button;
           t.me links only, e.g. the trip app's link from webapp_sync.py --link)

Only sends to a group that has (or had) a trip, a group the owner is in, or
the owner's own chat, so a message can never be steered to a stranger. Never prints the token.
Exit non-zero if Telegram rejects the message.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def allowed_chats() -> set[str]:
    p = Path(os.environ.get("SPLIT_DATA", ROOT / "trips" / "data")) / "trips.json"
    chats = {t["chat_id"] for t in json.loads(p.read_text())["trips"]} if p.exists() else set()
    owner = os.environ.get("SPLIT_OWNER_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    return chats | ({owner} if owner else set())


def token() -> str:
    """SPLIT_BOT_TOKEN from the environment, else ~/.splitbot/token (outside the repo)."""
    tok = os.environ.get("SPLIT_BOT_TOKEN", "")
    f = Path(os.environ.get("SPLITBOT_HOME", Path.home() / ".splitbot")) / "token"
    if not tok and f.exists():
        tok = f.read_text().strip()
    return tok


def call(method: str, fields: dict) -> dict:
    args = ["curl", "-sS", "--max-time", "30"]
    for k, v in fields.items():
        args += ["--data-urlencode", f"{k}={v}"]
    base = os.environ.get("SPLITBOT_API", "https://api.telegram.org")
    out = subprocess.run(args + [f"{base}/bot{token()}/{method}"], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "description": "no reply from Telegram"}


def upload(method: str, field: str, path: str, fields: dict) -> dict:
    args = ["curl", "-sS", "--max-time", "60", "-F", f"{field}=@{path}"]
    for k, v in fields.items():
        args += ["--form-string", f"{k}={v}"]  # taken literally: no @file or ;type= parsing
    base = os.environ.get("SPLITBOT_API", "https://api.telegram.org")
    out = subprocess.run(args + [f"{base}/bot{token()}/{method}"], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "description": "no reply from Telegram"}


def owner_in_chat(chat: str) -> bool:
    owner = os.environ.get("SPLIT_OWNER_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not owner:
        return False
    st = (call("getChatMember", {"chat_id": chat, "user_id": owner}).get("result") or {}).get("status")
    return st in ("creator", "administrator", "member", "restricted")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reply-to", type=int)
    ap.add_argument("--member-count", action="store_true")
    ap.add_argument("--photo", help="send this image (PNG/JPG) instead of text")
    ap.add_argument("--document", help="send this file (e.g. the Excel log) instead of text")
    ap.add_argument("--caption", default="")
    ap.add_argument("--poll", metavar="QUESTION", help="send a non-anonymous poll")
    ap.add_argument("--option", action="append", default=[], help="a poll option (2 to 10)")
    ap.add_argument("--multi", action="store_true", help="poll: allow several answers (e.g. dates)")
    ap.add_argument("--stop-poll", type=int, metavar="MESSAGE_ID", help="close a poll the bot sent")
    ap.add_argument("--button-text", help="text message: add one inline link button with this label")
    ap.add_argument("--button-url", help="the button's link; https://t.me/ links only (e.g. the trip app)")
    a = ap.parse_args(argv)
    if bool(a.button_text) != bool(a.button_url):
        ap.error("--button-text and --button-url go together")
    if a.button_url and not a.button_url.startswith("https://t.me/"):
        ap.error("--button-url must be a https://t.me/ link")
    if not token():
        print("send: SPLIT_BOT_TOKEN (or ~/.splitbot/token) not set", file=sys.stderr)
        return 2
    if a.status:
        r = call("getMe", {}).get("result") or {}
        privacy = "off (reads all group messages)" if r.get("can_read_all_group_messages") \
            else "ON: the bot only sees /commands, mentions and replies in groups"
        print(f"bot @{r.get('username')}; privacy mode {privacy}")
        return 0 if r else 1
    if not a.chat:
        ap.error("--chat is required")
    if a.member_count:  # read-only; works before the group has a trip
        d = call("getChatMemberCount", {"chat_id": a.chat})
        print(d["result"] if d.get("ok") else f"send: telegram error: {d.get('description')}")
        return 0 if d.get("ok") else 1
    if str(a.chat) not in allowed_chats() and not owner_in_chat(a.chat):
        print("send: refusing, chat is not a trip group or the owner", file=sys.stderr)
        return 2
    if a.poll is not None:
        opts = [o.strip() for o in a.option if o.strip()]
        if not a.poll.strip() or not 2 <= len(opts) <= 10:
            print("send: a poll needs a question and 2 to 10 options", file=sys.stderr)
            return 2
        fields = {"chat_id": a.chat, "question": a.poll.strip()[:300], "is_anonymous": "false",
                  "options": json.dumps([{"text": o[:100]} for o in opts], ensure_ascii=False)}
        if a.multi:
            fields["allows_multiple_answers"] = "true"
        if a.reply_to:
            fields.update(reply_to_message_id=a.reply_to, allow_sending_without_reply="true")
        d = call("sendPoll", fields)
        if not d.get("ok"):
            print(f"send: telegram error: {d.get('description')}", file=sys.stderr)
            return 1
        print(f"send: ok (message {d['result']['message_id']}) poll_id={d['result']['poll']['id']}")
        return 0
    if a.stop_poll:
        d = call("stopPoll", {"chat_id": a.chat, "message_id": a.stop_poll})
        if not d.get("ok"):
            print(f"send: telegram error: {d.get('description')}", file=sys.stderr)
            return 1
        print("send: ok (poll closed)")
        return 0
    if a.photo or a.document:
        method, field, path = (("sendPhoto", "photo", a.photo) if a.photo
                               else ("sendDocument", "document", a.document))
        if not Path(path).is_file():
            print(f"send: no such file {path}", file=sys.stderr)
            return 2
        fields = {"chat_id": a.chat}
        if a.caption:
            fields["caption"] = a.caption[:1000]
        if a.reply_to:
            fields.update(reply_to_message_id=a.reply_to, allow_sending_without_reply="true")
        d = upload(method, field, path, fields)
        if not d.get("ok"):
            print(f"send: telegram error: {d.get('description')}", file=sys.stderr)
            return 1
        print(f"send: ok (message {d['result']['message_id']})")
        return 0
    text = sys.stdin.read().strip()
    if not text:
        print("send: empty message", file=sys.stderr)
        return 2
    fields = {"chat_id": a.chat, "text": text[:4000], "disable_web_page_preview": "true"}
    if a.button_url:
        fields["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": a.button_text.strip()[:64], "url": a.button_url}]]}, ensure_ascii=False)
    if a.reply_to:
        fields.update(reply_to_message_id=a.reply_to, allow_sending_without_reply="true")
    d = call("sendMessage", fields)
    if not d.get("ok"):
        print(f"send: telegram error: {d.get('description')}", file=sys.stderr)
        return 1
    print(f"send: ok (message {d['result']['message_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
