---
name: bot-setup
description: One-off and maintenance tasks for the owner of this trip bot, who may not be technical. Use when they say "set up my trip bot", "check the bot", "deploy the trip app", "the app short name is X", "update the bot from github.com/rtsw96/trip-concierge-bot", or "resume the trip bot". Not for running the bot (that is the bill-split skill).
---

# Bot setup and maintenance

The owner followed SETUP.md and may know nothing about code. Reply in plain words: what you checked, what worked, and the one thing they need to do next, by its SETUP.md step number. No jargon, no stack traces.

**Never print** `SPLIT_BOT_TOKEN`, `SPLIT_OWNER_ID`, `CLOUDFLARE_API_TOKEN` or `CLOUDFLARE_ACCOUNT_ID`, not even partly. Check them with `[ -n "$VAR" ] && echo set || echo missing`. Never ask the owner to paste a token into the chat: tokens go in the environment's variables (SETUP.md step 5).

**Branch.** This repo keeps the bot's data and settings on `main`; the owner's standing instruction is to commit these tasks straight to `main` (see CLAUDE.md). `git checkout main && git pull --rebase origin main` first, `git push origin main` at the end.

## "Set up my trip bot" (SETUP.md step 6)

Run each check; stop at the first failure and say how to fix it.

1. **Variables.** `SPLIT_BOT_TOKEN` and `SPLIT_OWNER_ID` set. `SPLIT_OWNER_ID` must be digits only: `[[ "$SPLIT_OWNER_ID" =~ ^[0-9]+$ ]]`. Missing: they add it in the environment (step 5) and start a new session, since a running session doesn't pick up changes.
2. **Network.** `curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 https://nominatim.openstreetmap.org/status` should print 200. A 403 means the environment's Network access isn't **Full** (step 5).
3. **Token.** `python3 trips/splitbot/send.py --status` prints the bot's username and privacy mode. No username: the token is wrong (copy it again from @BotFather, step 3.5). Privacy mode ON: step 3.6.
4. **Webhook.** If the bot was ever used elsewhere, a webhook blocks it. Check without showing the token:
   `curl -sS "https://api.telegram.org/bot$SPLIT_BOT_TOKEN/getWebhookInfo" | python3 -c 'import json,sys; print("webhook set" if json.load(sys.stdin)["result"].get("url") else "no webhook")'`
   If set: `curl -sS -o /dev/null "https://api.telegram.org/bot$SPLIT_BOT_TOKEN/deleteWebhook"`.
5. **Tests.** `python3 -m pytest -q trips/splitbot` (or `python3 -m unittest discover -s trips/splitbot -p 'test_*.py'` if pytest is missing) and `node --test trips/webapp/test/*.mjs`. Report pass or fail in one line.
6. **Menu.** `python3 trips/splitbot/commands.py sync`.
7. **Hello.** `printf 'Your trip bot is set up. Next: the routine (SETUP.md step 7), then add me to your trip group.' | python3 trips/splitbot/send.py --chat "$SPLIT_OWNER_ID"`. "chat not found" or "bot can't initiate conversation": the owner hasn't opened the bot and tapped Start (step 3.7); ask them to, then retry.
8. Tell them it worked and that the next step is the routine (SETUP.md step 7). Don't start the listener here: the routine runs the bot.

## "Check the bot"

Do checks 1–4 above, then look at `git log --oneline -5 -- trips/data` (when the bot last saved anything) and whether `trips/data/PAUSED` exists. Explain what you found. If the routine seems not to run, ask them to open claude.ai/code/routines and check that it's switched on, uses the **Trip bot** environment and their repo.

## "Deploy the trip app" (SETUP.md step 10)

1. `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` set (else step 9.5, then a new session), plus `SPLIT_BOT_TOKEN` and `SPLIT_OWNER_ID`.
2. `bash trips/webapp/deploy.sh` (Bash timeout 600000). It creates the storage, publishes the app, stores the bot token and owner id as Cloudflare secrets, and writes `trips/webapp/config.json`. It prints nothing secret. If it says there's no `workers.dev` subdomain: step 9.2, then run it again. If an authentication error: the token needs the **Edit Cloudflare Workers** template with the account selected (step 9.4).
3. `git add trips/webapp/config.json trips/webapp/wrangler.toml && git commit -m "Trip app deployed"`, push to `main`.
4. `python3 trips/splitbot/webapp_sync.py` (pushes the trips to the app).
5. Give them the URL from the script's last line and point them to step 11, including 11.4 (the profile's Open App button needs the same URL set separately, or it opens blank).

## "The app short name is X" (SETUP.md step 11.3)

`python3 trips/splitbot/webapp_sync.py --set-short-name X`, commit `trips/webapp/config.json` ("Trip app short name"), push to `main`. Tell them `/app` in a trip group now posts the app button.

## "Update the bot from github.com/rtsw96/trip-concierge-bot"

Copies in the new code and keeps their data and app settings.

1. `git remote get-url upstream || git remote add upstream https://github.com/rtsw96/trip-concierge-bot.git`, then `git fetch upstream main`.
2. `git checkout upstream/main -- . ':(exclude)trips/data' ':(exclude)trips/webapp/wrangler.toml'` (`trips/webapp/config.json` isn't in upstream, so it's kept).
3. `git status --short` and `git diff --cached --stat`. If they had changed code themselves, those files are now overwritten: list them and ask before going on.
4. Run the tests (as in setup, step 5). If they fail, `git checkout HEAD -- .` to undo, and tell them the update was not applied.
5. Commit ("Update bot code from upstream") and push to `main`. If the trip app is deployed (`trips/webapp/config.json` has a `url`) and anything under `trips/webapp/` changed, run `bash trips/webapp/deploy.sh` too.
6. Tell them what changed in a few words (`git log --oneline` of the upstream commits they got), and that the next routine run uses it, or **Run now** on the routine to use it straight away.

## "Resume the trip bot"

If `trips/data/PAUSED` exists: `git rm trips/data/PAUSED`, commit ("Resume the bot"), push to `main`. Then ask them to click **Run now** on the routine (claude.ai/code/routines) and check that the routine is switched on.
