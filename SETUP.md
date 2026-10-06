# Setup guide

This guide assumes you've never done anything like this before. Follow the steps in order. Each one says what to click and what you should see. Total time: about 30–45 minutes. Use a computer if you can.

**Words you'll meet**

| Word | Meaning |
|---|---|
| **Bot** | A Telegram account run by a program instead of a person |
| **Token** | A long password for your bot, like `7412345678:AAH3k...`. Anyone who has it can control your bot, so never post it anywhere or send it to anyone |
| **Repository (repo)** | A folder of code stored on GitHub. You'll make your own private copy of this one |
| **Claude Code** | Claude working on code in a cloud computer. It runs your bot |
| **Session** | One conversation with Claude Code, with its own cloud computer |
| **Environment** | The settings for those cloud computers: which websites they may reach, and private values like your token |
| **Routine** | A schedule that starts a Claude Code session by itself |

**Overview**

| Part | Steps | You end up with |
|---|---|---|
| 1. The bot | 1–8 | A bot that splits bills in your group chat |
| 2. The app (optional) | 9–11 | A Telegram app with balances, charts and the plan |

---

## Part 1: the bot

### Step 1. Accounts you need

1. **Telegram** on your phone (you probably have it).
2. **GitHub:** go to [github.com/signup](https://github.com/signup) and create a free account. Remember your username.
3. **Claude Pro or Max:** go to [claude.ai](https://claude.ai), sign up or log in, and subscribe to Pro or Max (Settings → Billing). The free plan can't run Claude Code in the cloud.

### Step 2. Make your own private copy of this code

Your copy will hold your trip data (names and payments), so it must be **private**.

1. While logged in to GitHub, open this project's page (the page you're reading this on).
2. Click the green **Use this template** button near the top right, then **Create a new repository**.
   - Don't click **Fork**: a fork of a public project is always public.
3. On the form:
   - **Owner:** you.
   - **Repository name:** `trip-bot` (or anything you like).
   - Choose **Private**.
4. Click **Create repository**. You now have `github.com/<your username>/trip-bot`.

### Step 3. Create your Telegram bot

You'll do this in Telegram, by chatting with Telegram's own bot-making bot, @BotFather.

1. In Telegram, search for **@BotFather** (it has a blue tick) and open it. Tap **Start**.
2. Send `/newbot`.
3. It asks for a **name**. This is what people see, e.g. `Trip Bot`.
4. It asks for a **username**. It must end in `bot` and nobody else can have it, e.g. `ana_trip_bot`. Try another if it's taken.
5. BotFather replies with a message containing **the token**, a long line like `7412345678:AAH3kQ...`. Copy it into a private note for the next steps. Don't share it.
6. **Let the bot read the group.** Still in BotFather, send `/setprivacy`, tap your bot, then tap **Disable**. It should reply "Privacy mode is disabled". Without this, the bot only sees messages that start with `/`.
7. **Say hello to your bot.** Search for your bot's username in Telegram, open it and tap **Start**. Bots can't message you until you've done this, and yours needs to message you if something's wrong.

If your token ever leaks, send `/revoke` to BotFather to get a new one, and update it in step 5.

### Step 4. Find your Telegram user ID

The bot needs to know you're its owner. Your Telegram user ID is a number, like `123456789` (not your username or phone number).

1. In Telegram, search for **@userinfobot**, open it and tap **Start**.
2. It replies with your details. Copy the number next to **Id**.

### Step 5. Connect Claude to GitHub and add your settings

1. Go to [claude.ai/code](https://claude.ai/code). If it asks you to connect GitHub, do so. Otherwise open [claude.ai/connect-github](https://claude.ai/connect-github).
2. GitHub asks where to install the **Claude** app. Choose your account, pick **Only select repositories**, select `trip-bot`, and click **Install**. (If you're never asked, open [github.com/apps/claude](https://github.com/apps/claude), click **Configure** and add `trip-bot` there.)
3. Back on [claude.ai/code](https://claude.ai/code), find the small **cloud button** in the row just above the message box. It shows an environment name, probably **Default**. Click it.
4. Choose **Cloud**, then **Add cloud environment**. A form opens.
5. Fill it in:
   - **Name:** `Trip bot`
   - **Network access:** **Full**. The bot reaches Telegram, maps, exchange rates and a speech model download, which the default setting blocks.
   - **Environment variables:** paste these two lines, with your own values after the `=` and no spaces:
     ```
     SPLIT_BOT_TOKEN=paste-your-bot-token-from-step-3
     SPLIT_OWNER_ID=paste-your-id-from-step-4
     ```
   - Leave **Setup script** empty. If there's an **API credentials** section, leave that empty too.
6. Click **Create environment**.

These values are visible to anyone who uses this environment. That's only you, unless you share your Claude account.

### Step 6. Check everything works

1. On [claude.ai/code](https://claude.ai/code), start a new session:
   - **Repository:** your `trip-bot`.
   - **Environment:** **Trip bot** (the cloud button above the message box).
2. Type: **`Set up my trip bot`** and send.
3. Claude checks your token and ID, checks privacy mode, runs the tests and sets up the bot's command menu. Then your bot sends you a message in Telegram saying setup worked.

If Claude reports a problem, it says what to fix. The usual one is a typo in the variable names in step 5: they must be exactly `SPLIT_BOT_TOKEN` and `SPLIT_OWNER_ID`.

### Step 7. Make it run by itself (the routine)

A Claude session doesn't run forever, so a routine starts a fresh one twice a day. Each run lasts about 12 hours.

1. Go to [claude.ai/code/routines](https://claude.ai/code/routines) and click **New routine**.
2. **Name:** `Trip bot run`.
3. **Prompt.** Paste this exactly:

   ```
   Run the Telegram trip bot for the next 11 hours 50 minutes. Work on the main branch: `git checkout main && git pull --rebase origin main`. Read .claude/skills/bill-split/SKILL.md once (and .claude/skills/trip-concierge/SKILL.md the first time a trip in planning sends an event), then do "Start or restart": run `python3 trips/splitbot/transcribe.py --warm` in the foreground first (Bash timeout 600000), then `python3 trips/splitbot/commands.py sync`, then launch the listener. Handle every listener exit as the skill says, and relaunch it each time with `python3 trips/splitbot/listen.py <N>`, where N = min(3300, seconds left in this run), Bash timeout 3600000, run_in_background true. After each exit also run `python3 trips/splitbot/report.py nightly` and `python3 trips/splitbot/plan.py due --send`, and commit and push trips/data to main whenever it changed. Keep context small. Never print SPLIT_BOT_TOKEN or SPLIT_OWNER_ID. Stop when the time is up.
   ```

4. **Model** (the selector next to the prompt): choose **Sonnet**. It handles the bot well and uses less of your plan than Opus.
5. **Repositories:** add your `trip-bot`.
6. **Environment:** **Trip bot**.
7. **Trigger:** **Schedule** → **Daily**, at a time a few minutes past the hour, e.g. **8:07 AM**.
8. **Second run of the day:** click **Add another trigger** → **Schedule** → **Daily** at **8:07 PM**, twelve hours after the first. If the form won't take a second schedule, save this routine, then make a second identical one called `Trip bot run (evening)` at 8:07 PM.
9. **Connectors** (bottom of the form): remove them all. The bot doesn't need Gmail, Drive or anything else.
10. Click **Create**.
11. **Start it now:** open the routine and click **Run now**. Otherwise it starts at the next scheduled time.

After about 2 minutes, a new session appears in your Claude Code sessions list. Your bot is live.

### Step 8. Use it on a trip

1. In Telegram, create a group with your travel buddies (or use your existing one).
2. Add your bot to the group: group name → **Add members** → search your bot's username.
3. In the group, send: `/newtrip Japan JPY Ana Ben Chloe`, with your trip name, the local currency code (JPY, THB, VND, USD, EUR...) and everyone's first names, yours first. Only you, the owner, can do this.
4. Everyone just talks to it:

| Send | Get |
|---|---|
| `paid 8400 for the taxi` | Logged, split equally among everyone |
| `Ben paid 120 SGD dinner, excl. Chloe` | Payer, currency and who's in, as written |
| A receipt photo | The total, split |
| A voice note: "Chloe paid four thousand yen for drinks" | The same, from speech |
| `balances` / `settle` | Who owes what / the fewest transfers to square up |
| `undo`, `fix #7 amount 9000` | Corrections |
| Share location, then `dessert near me` | 3 places nearby with walking time |
| `/endtrip` (you) | Closes the trip: final settle-up, chart, Excel file and the Wrapped PDF |

The full list is in [trips/README.md](trips/README.md). To plan a trip before it starts, use `/plan Japan JPY Dec Ana Ben Chloe` instead of `/newtrip`.

Replies take 20–60 seconds. That's Claude reading the message.

---

## Part 2: the trip app (optional)

A Telegram app with tabs for balances, payments, charts, the plan, places and the Wrapped. It runs free on Cloudflare. Skip this part if the chat is enough.

### Step 9. Cloudflare account and key

1. Sign up at [dash.cloudflare.com/sign-up](https://dash.cloudflare.com/sign-up) (free). Verify your email.
2. In the left menu, open **Workers & Pages** (under **Compute**) once. If it asks you to choose a `workers.dev` subdomain, pick any name.
3. **Account ID:** on that Workers & Pages page, find **Account ID** on the right side and copy it. (It's also in the address bar: the long string of letters and numbers after `dash.cloudflare.com/`.)
4. **API token:** click the person icon at the top right → **Profile** → **API Tokens** (left menu) → **Create Token**. Find **Edit Cloudflare Workers** in the list and click **Use template**. Under **Account Resources**, choose your account. Under **Zone Resources**, choose **All zones**. Click **Continue to summary**, then **Create Token**, and copy the token (it's shown once).
5. Add both to your environment: on [claude.ai/code](https://claude.ai/code), click the cloud button → **Cloud** → hover over **Trip bot** → click the settings icon on the right. Add two lines under **Environment variables**:
   ```
   CLOUDFLARE_API_TOKEN=paste-the-token
   CLOUDFLARE_ACCOUNT_ID=paste-the-account-id
   ```
   Click **Save changes**.

### Step 10. Deploy the app

1. Start a new session on [claude.ai/code](https://claude.ai/code) with your `trip-bot` repository and the **Trip bot** environment.
2. Type: **`Deploy the trip app`**. Claude publishes it to your Cloudflare account and replies with the app's address, like `https://trip-app.<your-subdomain>.workers.dev`. Copy it.

### Step 11. Register the app with Telegram

1. In Telegram, open **@BotFather** and send `/newapp`. Choose your bot.
2. Answer its questions:
   - **Title:** `Trip`
   - **Description:** `Balances, payments and charts for the trip`
   - **Photo:** any image, 640×360 pixels. A screenshot cropped to that size works.
   - **GIF:** send `/empty`
   - **Web App URL:** the address from step 10
   - **Short name:** e.g. `trip` (letters, digits and underscores)
3. Back in your Claude session, type: **`The app short name is trip`** (with your short name).

Now send `/app` in your trip group: the bot posts a button that opens the app. Everyone sees only the trips they're on.

---

## Costs

- **Claude:** the bot uses your plan's usage while a run is going, even when the group is quiet (a little each hour), and more on busy days with lots of receipts and questions. See [claude.ai/settings/usage](https://claude.ai/settings/usage). **Between trips, switch the routine off** (the on/off switch on the routine's page) and turn it on again a day before you need it.
- **Telegram, GitHub, Cloudflare:** free for this.

## When something's wrong

| Problem | Try |
|---|---|
| The bot doesn't reply at all | Open [claude.ai/code](https://claude.ai/code) and look for the latest `Trip bot run` session. No session: open the routine and click **Run now**. A session that stopped with an error: read its last message, it usually says what's missing |
| It only answers `/commands` | Privacy mode is still on. Step 3.6, then remove the bot from the group and add it again |
| "SPLIT_BOT_TOKEN missing" or similar | Check the variable names in your environment (step 5). They're case-sensitive |
| The bot never messages you privately | Open your bot in Telegram and tap **Start** (step 3.7) |
| The first voice note takes long | The speech model downloads at the start of each run (1–2 minutes). Later ones are quick |
| "Network" or "403" errors in the session | Set the environment's **Network access** to **Full** (step 5) |
| You've run out of Claude usage | The bot stops until your usage resets. Messages wait in Telegram for up to 24 hours |

Still stuck? Start a session with your repo and the **Trip bot** environment, describe the problem, and ask Claude to check the bot. It can read the code and the logs.

## Getting updates

When this project improves, start a session with your repo and type: **`Update the bot from github.com/rtsw96/trip-concierge-bot`**. Claude copies in the new code, keeps your trip data and app settings, runs the tests, and saves it. The next routine run uses it.

## Pausing and resuming

- **Pause:** send `/stopbot` in any trip group (owner only). The bot stops until you resume it.
- **Resume:** start a session and type **`Resume the trip bot`**, then click **Run now** on the routine.
