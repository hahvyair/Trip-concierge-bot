# Feature reference

Add the bot to a trip's Telegram group. Anyone in the group posts payments the way they'd say them, or a receipt photo. The bot logs each one against the trip, splits it, converts it to SGD, and replies with the entry. Ask "balances" or "settle" at any time; at the end, the owner sends /endtrip and gets the settle-up list.

No Anthropic API key is involved. A Claude Code session does the reading, using your subscription. A background long-poll listener (curl, no model) wakes that session when a relevant message arrives.

## In the group

| Send | Get |
|---|---|
| "paid 8400 for the taxi" | Logged in the trip currency, split equally among everyone on the trip |
| "Ben paid 120 SGD dinner, excl. Chloe" | Payer, currency and who's in, as written |
| "hotel 30000, Dan and Eve 2 shares" | Weighted split |
| A voice note, e.g. "Kai paid four hundred k for dinner" | Transcribed on the bot's own machine (no outside service), the audio deleted at once, then handled like the same words typed. Every voice note is read (spoken amounts rarely contain digits); chatter gets no reply. The reply starts with what it heard, so mistakes are easy to spot |
| A receipt photo, optionally "Ben had the steak, rest shared" | The total, split as described; tax and service spread pro rata. The photo is deleted once logged |
| "Ben paid me back 50" | A settle-up transfer |
| `wrapped` / "expense report" / "trip PDF" | The trip PDF: Wrapped-style cards (total spent, top category, top 5 bills, biggest day, an award for each person, how to settle), then the full expense report. Sent automatically at /endtrip |
| Share your location (📎 → Location), then "dessert near me", "where should we eat tonight", "bar around here" | Up to 3 places nearby, nearest first: walking distance, 🚶 walking time, a rough 🛵 Grab time (OpenStreetMap routing, no traffic data, padded), why it fits, price, hours if known and a Google Maps link. They also appear in the trip app's Places tab, where "Distances from me" recalculates from where you are now, on your phone. Locations are kept for an hour on the bot's machine, never in the repo or the group |
| (planning) "draft an itinerary", "send the catalogue" | A day-by-day itinerary drafted from the group's decisions and shortlist, and the trip catalogue PDF: cover, at a glance, day by day with Map links, the shortlist grouped by kind, bookings checklist, decisions and to-dos |
| `balances` / `settle` / `list` | Who owes what / the fewest transfers to settle / the last 10 entries |
| `breakdown` / `excel` / `chart` (any wording: "send the spreadsheet", "can I see a visualisation") | Today's breakdown with chart and Excel log / just the Excel log / just the chart |
| (every night, ~21:45 trip time) | The day's breakdown: total, by category, who paid, each person's share, balances and settle-up, plus a chart and the Excel log. Skipped if nothing was logged |
| `undo`, "delete #7", "fix #7 amount 9000", or a reply to the bot's message | Corrections. Nothing is erased: the old row stays in the file, marked deleted or edited |
| `/join` | Adds you to the trip |
| `/newtrip Japan JPY Ana Ben Chloe Dan` (owner) | Starts a trip in this group. One open trip per group |
| `/endtrip` (owner) | Closes the trip and posts the final settle-up, chart, Excel log and the Wrapped PDF |
| `/stopbot` (owner) | Pauses the bot until you ask Claude to resume it |

Rules:
- **Default split:** everyone on the trip equally, unless the message says otherwise.
- **Currency:** each payment is logged in the currency paid and converted at that day's ECB rate (frankfurter.dev; Yahoo for currencies the ECB doesn't cover). If someone gives the SGD amount their card was charged, that rate is used instead.
- **Settling:** balances and transfers are in SGD, rounded to the cent. Shares always add up exactly to the payment.
- **Categories:** each payment gets one of food, drinks (cocktails, bars, coffee), transport, lodging, activities, shopping or other, for the charts and the workbook.
- **Excel:** the workbook is rebuilt from `data/expenses.csv` each time and sent to the group; it isn't stored in the repo.
- **Talking to it:** mention it, reply to its messages, or just say "bot ...": it answers questions about the trip's spending ("how much have we spent?", "what did Leo pay for?") in the chat. It stays silent on ordinary chatter: the listener only wakes Claude for messages with a number, a photo, a voice note, a /command, a mention or reply to the bot, or a keyword like "balances".
- **Your private chat:** you (only you) can also message the bot directly for the chart, the Excel file, balances or a breakdown; replies come to you, not the group.
- **Groups:** it works in any group you're a member of. Before a trip is started, it tells people that you can start one. Groups you're not in are ignored.
- **Privacy mode:** if BotFather's privacy mode is still on, the bot only sees /commands, and it messages you how to fix it.

## Planning a trip

The same bot helps the group plan a trip before it starts, in the trip's own group chat. Its first job is group decisions: turning debates into polls, recording what was decided, and keeping one plan everyone can check. Its second is suggestions, when asked or when the group is stuck.

| Send | Get |
|---|---|
| `/plan Japan JPY Dec Ana Ben Chloe` (owner) | Starts the trip in planning: dates (or a loose window like "Dec 2026"), members, currency |
| "where are we", "what did we decide" | The plan: dates, budget, decisions, open polls with tallies and who hasn't voted, to-dos by person, bookings |
| A debate ("Namba or Umeda?") | The bot offers a poll. Polls are non-anonymous; date polls allow several answers. When everyone has voted, it posts the result and records it. A tie goes back to the group |
| "suggest a ryokan", "any ideas for day 3?" | Up to 3 options, one line each, with why it fits, a rough price and a link, then an offer to poll them. Check prices and hours before booking |
| "I'll check visas by Friday", "done #2" | To-dos with an owner and a due date |
| "Ben's booking the flights", "flights booked" | The bookings checklist: who's booking what, held or booked, estimated cost, deadline |
| "catch us up" | A short summary of the recent chat and the plan |
| (daily, after 10:00 trip time) | A reminder for to-dos and bookings due within 2 days or overdue, once per item per day |

Rules:
- **Phase:** a trip is in planning until its start date (or while it has no dates). From the start date it runs as a normal bill-split trip. A trip started with /newtrip is live from the start, as before.
- **Payments still work while planning,** so deposits for flights and hotels are split as usual. There is no nightly breakdown until the trip starts.
- **The bot never books or pays.** It keeps checklists and links. It never stores booking references, passport or ID numbers, card details, or addresses beyond a venue name.
- **Chat log:** while a trip is in planning, the listener keeps the group's last 400 messages (name, text, time) in `~/.splitbot/chatlog/` so "catch us up" works. It is outside the repo, never committed, and lost when the bot's run restarts (about every 12 hours). The listener does not wake Claude for this chatter.

## Telegram app

A Mini App for each trip, opened from a button the bot posts in the group ("app", "open the app", "dashboard"). Each person sees only the trips they're on, and the owner sees all of them. It is view-only except for names (see People below).

| Tab | Shows |
|---|---|
| Balances | Total spent, per day, per person; who is owed and who owes (bars); the settle-up transfers; People (rename, add) |
| Payments | Every payment, newest first, with search and filters (person, category, day): description, payer, local amount, SGD, split, category |
| Charts | Spending per day by category (tap a bar for that day), category shares, paid vs share per person |
| Plan | Trips with a plan: dates, budget, decisions, open polls with tallies and who hasn't voted, to-dos, bookings, itinerary |
| Wrapped | The Wrapped cards as swipeable cards, once something has been spent |

It follows the Telegram theme (light or dark). Payments and corrections are still made in the chat, and the app shows the time of its latest data.

**People.** The Balances tab lists everyone on the trip. The owner can rename anyone (the pencil) and add people ("Add person"); everyone else can rename only themselves (their row says "you"). A name is 1–31 letters or digits; spaces and `. ' -` are fine. Nothing changes in the app itself: the request waits in the Worker, the bot picks it up within about a minute, applies it to the ledger (a rename changes the name on every entry and in the plan, so balances don't move), and posts one line in the group, e.g. "Ana renamed Leo → Leon in the trip app". If it can't be applied (say the name was taken in the meantime), it says so in the group instead. Tap Refresh to see the result. Closed trips can't be changed. Someone added from the app is linked to their Telegram account the first time they post in the group.

**How it works.** There is no always-on server. A Cloudflare Worker (free tier, `webapp/`) serves the page and a small API. After each push of `trips/data`, the bot session runs `splitbot/webapp_sync.py`, which sends one snapshot per trip to the Worker, signed with a key derived from the bot token. When someone opens the app, Telegram signs who they are (initData). The Worker checks that signature against the bot token and returns only the trips whose members include that Telegram id. A rename or add from the app is checked the same way (who is asking comes from initData, what they may do from the trip's stored members, never from the page), then queued in KV (at most 10 per trip). The bot's listener collects the queue with a signed request, the session applies each change with `splitlog.py` and acknowledges it (`webapp_sync.py --ack`), and the queue entry is deleted.

**Setup:** see [SETUP.md](../SETUP.md), part 2.

**Privacy.** A snapshot of each trip's data is stored in your Cloudflare account's KV store: first names, Telegram ids, payments, balances and the plan. The chat log, receipts, usernames and who logged what are never sent. Telegram ids are used only to decide who may see or change a trip and are not sent to the page. A queued name change also records who asked (Telegram id and name) until the bot has handled it. A closed trip stays in the app for 7 days after /endtrip, then is removed. `webapp_sync.py --delete <trip id>` removes a trip from the app. The Worker keeps no request logs.

## Files

| File | Role |
|---|---|
| `splitbot/listen.py` | Long-polls the bot, keeps only trip groups (and the owner), drops chatter, downloads receipts to the inbox, collects name changes from the trip app, prints events, exits |
| `splitbot/splitlog.py` | Trips, members, add, transfer, edit, undo, balances, settle. All the arithmetic, in SGD cents. Trip dates and the planning phase (`trip new --planning`, `trip dates`, `phase()`) |
| `splitbot/plan.py` | The plan for a trip being planned: basics, decisions, polls and votes, ideas, to-dos, bookings checklist, itinerary, reminders (`due`), chat log for "catch us up" |
| `splitbot/catalogue.py` | The trip catalogue PDF (`report.py catalogue`), from the plan file |
| `splitbot/places.py` | Shared locations (kept an hour, outside the repo), area names and venue distances via OpenStreetMap (no key) |
| `splitbot/transcribe.py` | Voice notes to text with a local Whisper model (faster-whisper on CPU; no API key). Used by the listener |
| `splitbot/send.py` | Replies (text, chart images, files, polls), only to a trip group, a group you're in, or you |
| `splitbot/report.py` | Nightly breakdown, chart (PNG via the preinstalled Chromium, `render_png.js`) and the Excel workbook (Transactions, Daily, Balances, By category; with Excel charts) |
| `splitbot/wrapped.py` | The trip PDF (`report.py wrapped`): the Wrapped cards and the expense report, printed by `render_pdf.js` |
| `splitbot/webapp_sync.py` | Builds each trip's snapshot for the Telegram app and pushes it to the Worker (signed); `--link` gives the app link for a group; `--ack` marks app name changes as handled |
| `webapp/` | The Telegram app: `worker.js` (Cloudflare Worker: page, initData check, sync, the queue of name changes), `public/` (the page), `wrangler.toml`, `deploy.sh`, `config.json` (URL, bot username, app short name), `test/` (`node --test trips/webapp/test/*.mjs`) |
| `splitbot/test_splitbot.py`, `splitbot/test_plan.py`, `splitbot/test_webapp.py` | Tests, including end-to-end runs against a fake Telegram server |
| `data/trips.json` | Trips, their group chat and members |
| `data/expenses.csv` | Every payment and settle-up, with the rate and its source |
| `data/fx_cache.json` | Rates used, so the numbers don't move on a re-run |
| `data/plans/<trip id>.json` | Each planned trip's plan (no chat text) |
| `../.claude/skills/bill-split/SKILL.md` | The operating procedure the session follows |
| `../.claude/skills/trip-concierge/SKILL.md` | The procedure for trips in planning |

## Setup

See [SETUP.md](../SETUP.md).

## Privacy and limits

- **Who can use it:** only groups you're a member of (or that have a trip you started) are read. Messages from any other chat are dropped unread, except yours.
- **What's stored:** friends' first names, Telegram ids and what they paid are stored in your copy of this repo (keep it private), as are plans (decisions, votes by name, ideas, to-dos, bookings checklist). Receipts, voice notes and chat messages are not stored in the repo (voice notes are transcribed on the bot's machine and the audio is deleted at once). If the Telegram app is set up, a snapshot of each trip (not the chat) is also kept in your Cloudflare KV store; see Telegram app above.
- **Latency:** typically 20–60 seconds. Between two runs (a few minutes, twice a day) messages wait; Telegram keeps them for 24 hours.
- **Not a payment app:** the bot records and calculates. People still pay each other themselves.
