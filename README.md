# Trip Concierge Bot

A Telegram bot for group trips. Add it to your trip's group chat and it:

- **Splits the bills.** People post payments the way they'd say them ("paid 8400 for the taxi", "Ben paid 120 SGD dinner, excl. Chloe"), send a receipt photo or a voice note. The bot logs each one, splits it, converts it to SGD and keeps running balances. `settle` gives the fewest transfers to square up.
- **Reports.** A breakdown with a chart and an Excel file every night, and at the end a "Wrapped"-style trip PDF (who spent most on food, the biggest night, an award for each person).
- **Helps plan.** Before the trip it turns debates into polls, records decisions, keeps to-dos and a bookings checklist, drafts an itinerary and sends a trip catalogue PDF.
- **Helps on the trip.** Share your location and ask "dessert near me": up to 3 places with walking distance and time.
- **Has an app (optional).** A Telegram Mini App with balances, payments, charts, the plan and nearby places, free on Cloudflare.

It never books or pays for anything. People still pay each other themselves.

## How it works, in one paragraph

There is no server to rent. The bot's "brain" is Claude, running in a Claude Code cloud session on your own Claude subscription. A small script in that session waits for Telegram messages and wakes Claude only when one matters (a payment, a question for the bot). Claude reads it, uses the scripts in this repo to do the arithmetic, replies in the group, and saves the ledger back to your GitHub repo. A scheduled "routine" starts a fresh session twice a day, so it runs on its own.

The method (a listener that exits to wake Claude, and why it stays cheap) is explained in [HOW-IT-WORKS.md](HOW-IT-WORKS.md). It works for other chat bots too.

## What you need

| Thing | Cost | Why |
|---|---|---|
| Telegram on your phone | Free | The bot lives there |
| A GitHub account | Free | Holds your copy of this code and the trip ledger |
| A Claude **Pro** or **Max** plan | Paid | Runs the bot. It uses your plan's usage while running (see SETUP.md, "Costs") |
| A Cloudflare account | Free | Only for the optional app |

## Get started

**Follow [SETUP.md](SETUP.md).** It assumes no technical background and takes about 30–45 minutes, mostly clicking through websites. A computer is easier than a phone for setup; after that, everything happens in Telegram.

The full list of what the bot understands is in [trips/README.md](trips/README.md).

## Limits

- **Balances are in Singapore dollars (SGD).** Payments can be in any currency; they're converted to SGD at the day's rate. Changing the home currency means changing the code.
- **One owner per bot.** The person who sets it up is the owner: they start and end trips. Everyone else in the group can log and check payments.
- **Replies take 20–60 seconds,** because Claude reads each message.

## Privacy

Your trip data (first names, Telegram ids, payments, plans) is saved in **your** copy of this repo, so keep that copy **private** (SETUP.md shows how). Receipt photos and voice notes are deleted from the bot's machine once read and never saved to the repo. The optional app keeps a copy of each trip's numbers in your own Cloudflare account. Nothing goes to the author of this project.

## License

MIT, see [LICENSE](LICENSE). The Barlow fonts in `trips/webapp/public/fonts/` are under the SIL Open Font License (`OFL.txt` there).
