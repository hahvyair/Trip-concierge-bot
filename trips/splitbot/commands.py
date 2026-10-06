#!/usr/bin/env python3
"""The bot's command menu in Telegram.

Everyone sees the public commands. The owner also sees the owner-only ones
(/newtrip, /plan, /endtrip, /stopbot) in every trip group and in their
private chat with the bot. Idempotent: run at each bot start and after
/newtrip or /plan so a new group gets the owner's menu too.

  commands.py sync [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import send  # noqa: E402
import splitlog as sl  # noqa: E402

PUBLIC = [
    ("app", "Open the trip app: balances, payments, charts, places"),
    ("balances", "Who owes what"),
    ("settle", "Fewest transfers to settle up"),
    ("list", "The last 10 entries"),
    ("breakdown", "Today's spending with chart and Excel"),
    ("chart", "Spending chart"),
    ("excel", "Excel log of every payment"),
    ("wrapped", "Trip Wrapped PDF"),
    ("undo", "Undo the last entry"),
    ("join", "Add yourself to this trip"),
    ("status", "Where the trip or plan stands"),
    ("catalogue", "The trip plan as a PDF (planning)"),
    ("help", "What the bot can do"),
]
OWNER = [
    ("newtrip", "Start a trip: /newtrip Name CUR names"),
    ("plan", "Start planning a trip"),
    ("dates", "Set the trip dates: /dates 3 Oct - 7 Oct"),
    ("endtrip", "Close the trip: settle-up and Wrapped"),
    ("stopbot", "Pause the bot"),
]
# Narrower menus Telegram would show instead of the default one; cleared so
# nothing hides the public list (an /app-only list once did).
STALE_SCOPES = [{"type": "all_group_chats"}, {"type": "all_private_chats"}, {"type": "all_chat_administrators"}]


def as_json(cmds: list[tuple[str, str]]) -> str:
    return json.dumps([{"command": c, "description": d} for c, d in cmds])


def plan_calls() -> list[tuple[str, dict]]:
    owner = os.environ.get("SPLIT_OWNER_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    calls = [("deleteMyCommands", {"scope": json.dumps(s)}) for s in STALE_SCOPES]
    calls.append(("setMyCommands", {"commands": as_json(PUBLIC), "scope": json.dumps({"type": "default"})}))
    if owner:
        full = as_json(PUBLIC + OWNER)
        calls.append(("setMyCommands", {"commands": full, "scope": json.dumps({"type": "chat", "chat_id": int(owner)})}))
        for t in sl.load_trips():
            if t["status"] == "open":
                calls.append(("setMyCommands", {"commands": full, "scope": json.dumps(
                    {"type": "chat_member", "chat_id": int(t["chat_id"]), "user_id": int(owner)})}))
    return calls


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["sync"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    calls = plan_calls()
    if a.dry_run:
        owner = os.environ.get("SPLIT_OWNER_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "-"
        for m, f in calls:
            print(m, (f.get("scope") or "").replace(owner, "<owner>"))  # never print the owner's id
        return 0
    if not send.token():
        print("commands: no bot token; nothing done")
        return 0
    failed = [(m, f.get("scope"), r.get("description")) for m, f in calls
              for r in [send.call(m, f)] if not r.get("ok")]
    owner = os.environ.get("SPLIT_OWNER_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "-"
    for m, scope, why in failed:
        print(f"commands: {m} {(scope or '').replace(owner, '<owner>')} failed: {why}")
    print(f"commands: menu synced ({len(calls) - len(failed)}/{len(calls)} calls ok)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
