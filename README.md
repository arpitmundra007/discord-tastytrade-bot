# Discord → Tastytrade Signal Bot

Reads options signals posted in a Discord channel, applies risk filters, and
places bracketed (OTOCO) orders on Tastytrade — controlled through a live
web dashboard.

## ⚠️ Before you touch a live account

- Leave `dry_run` on (the default) for your first sessions. This parses
  signals, runs risk checks, and logs the order payload it *would* send —
  no order is placed.
- Test against Tastytrade's **sandbox/cert** environment (the Setup tab's
  default) with a demo account before switching to Live.
- Even once live, keep the hard contract cap low (e.g. 1–2) regardless of
  what a signal's size tag says — it's a second line of defense if the
  parser misreads something.
- This is a **starting scaffold**, not a finished trading system. Test every
  code path (parser edge cases, partial fills, rejected orders, disconnects,
  a restart mid-session) before trusting it with real money. Nothing here is
  financial advice.

## Setup guide

**Windows: the easy way.** Double-click **`setup.bat`**. It installs Python
automatically if you don't have it (via `winget`, Windows' built-in package
manager), creates the virtual environment, installs every dependency,
verifies everything actually works, and offers to launch the bot
immediately — no terminal typing required at all. If it needs to install
Python for you, it'll ask you to close the window and run it again once
(Windows needs a fresh window to recognize a newly-installed program) —
that's the only manual step in the whole process.

Everything below is what `setup.bat` does automatically, kept here for
Mac/Linux users, anyone who prefers the terminal, or if something in the
automated version needs troubleshooting.

You only need the terminal once, to start the process. Everything else —
including your very first configuration — happens in the browser.

**1. Install dependencies**

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

**2. Verify your environment**

```bash
python check_setup.py
```

This checks every dependency actually works before you try running the
real app - if something's wrong (a package conflict, a version mismatch),
you'll get one clear line telling you what and how to fix it, instead of a
five-file traceback. Re-run this after any reinstall or troubleshooting
step. Safe to run repeatedly - it doesn't touch Discord or Tastytrade.

**3. Start the app**

```bash
python run.py
```

This works even with no `.env` file at all — it'll boot with everything
blank and let you configure from the browser.

**4. Open the dashboard**

Go to **http://localhost:8000**. With nothing configured yet, you land
automatically on the **Setup** tab.

**5. Fill in Setup and save**

- **Discord mode** — `Bot` (needs a server admin to invite it, but is
  ToS-compliant) or `Self-bot` (works on any channel you're a member of, no
  admin needed, but is against Discord's ToS regardless of read-only use —
  see the section below before choosing this).
- **Token** — bot token or user token, matching whichever mode you picked.
- **Signal channel IDs** — comma-separated. Enable Developer Mode in Discord
  (User Settings → Advanced) to right-click a channel and copy its ID.
- **Tastytrade environment, client secret, refresh token, account number** —
  see "Getting Tastytrade credentials" below.

Click **Save & restart**. The whole process restarts itself and the page
reloads automatically once it's back — this takes a few seconds.

**6. Watch it work**

You're now on the **Live** tab. The Quote Stream card should flip to
Connected within a few seconds. Post a real signal in your configured
channel and watch it appear in the Recent Activity feed with its
approve/reject decision.

**7. Tune risk filters live**

The **Risk filters** tab (slippage tolerance, position sizing by tag,
TP/SL, hard contract cap) applies to the running process immediately on
save — no restart needed. Only Setup-tab changes (credentials, mode,
channels) need the restart, since those connections are established once
at startup.

**8. Go live when ready**

Flip `dry_run` off from the Risk tab once you've watched enough signals
flow through correctly in dry run. Keep the dashboard's pause button
within reach for your first live session — it's a real kill switch that
takes effect on the very next signal.

## Day-to-day: starting the app after initial setup

`setup.bat` (or steps 1-2 above, if done manually) is a **one-time** step -
it doesn't need repeating unless you delete the `venv` folder or change
`requirements.txt`. Every time after that, just double-click **`start.bat`**
(or run `start.bat` from cmd). It activates the venv, runs the environment
checker, and starts the app in one step - if the checker finds a problem, it
tells you and stops before launching anything broken; otherwise it goes
straight to `python run.py` and the dashboard.

## Getting Tastytrade credentials

You need a `client_secret` (register an OAuth app at
developer.tastytrade.com) and a `refresh_token` (obtained via the OAuth
authorization flow — Tastytrade's docs walk through this:
https://developer.tastytrade.com/oauth/).

## Testing with real signals, without real money

**Sandbox** (which account orders go to) and **Dry Run** (whether an order
gets submitted at all) are two completely independent switches — the
dashboard's Live tab shows a banner combining both so it's always clear
which of the four combinations you're in.

To test with real Discord signals flowing in real-time, but zero real
money at risk:

1. Go to https://developer.tastytrade.com/login/ and create a **sandbox
   account** — a separate login from your regular tastytrade.com
   credentials, not a toggle on your existing account.
2. Once logged in, create a test customer, then a test account under it
   with whatever starting cash balance you want to practice with — that's
   your test funds.
3. While logged into that same sandbox user, register a new OAuth
   application (same process as your live one) and create a grant to get a
   sandbox client secret + refresh token.
4. Dashboard Setup tab: select **Sandbox / cert**, enter the sandbox
   credentials and test account number, Save & restart.
5. Risk filters tab: turn **Dry Run OFF**. This is the step that's easy to
   miss — Sandbox alone doesn't submit anything; Dry Run is separate and
   also has to be off for real order flow (against fake money) to happen.

Two quirks of the sandbox environment worth knowing: it resets every 24
hours (positions and trade history clear, your login/account structure
doesn't), and quotes there are always 15 minutes delayed — so don't read
too much into slippage-check behavior while testing here, since a stale
sandbox quote can trigger a rejection that a real live quote wouldn't.

## Discord: self-bot mode

This project automates your own personal Discord account instead of using
an official Bot application — it works on any channel you're already a
member of, with no server admin needed. Discord's own policy prohibits
this unconditionally, regardless of read-only use, and violating it risks
termination of your entire Discord account, not just its use here — see
https://support.discord.com/hc/en-us/articles/115002192352. This exists
because it was explicitly requested with that tradeoff understood; it
isn't something to use without weighing that risk yourself first.

Get your account's user token **manually via your own browser's developer
tools only** (DevTools → Network tab → filter Fetch/XHR → reload → click
any request to `discord.com/api` → copy the `authorization` header). Do
**not** use a browser extension or "token grabber" tool for this — that's
a common vector for genuine account-stealing malware, independent of
anything in this project.

## What's included

- `app/signal_parser.py` — parses the Discord card format into a structured signal
- `app/risk_engine.py` — slippage check, position sizing by risk tag, TP/SL calc
- `app/tastytrade_client.py` — auth, persistent live quote stream, OTOCO order builder/submitter
- `app/discord_selfbot.py` — the Discord listener (self-bot mode)
- `app/runtime_state.py` — the live pause/kill-switch flag
- `app/db.py` — SQLite log of every signal + decision + order response (audit trail)
- `app/config.py` — all tunables; live-editable via the dashboard, persisted to `.env`
- `app/main.py` — FastAPI app: serves the dashboard and all its API endpoints
- `app/static/dashboard.html` — the dashboard itself

## Signal format this parser expects

```
Buy To Open
LOTTO SIZE / SMALL
SPY 731P  0DTE $1.7
```

Line 1: action (`Buy To Open`, `Sell To Close`, `Sell To Open`, `Buy To Close`)
Line 2: free-text size/risk tag — last word after `/` is used for sizing (e.g. `SMALL`)
Line 3: `TICKER STRIKE[C|P]  NDTE $PRICE`

If your channel posts other formats (verticals, multi-leg, explicit dates
instead of `NDTE`), extend `signal_parser.py` — it's intentionally isolated
so you can add patterns without touching the rest of the pipeline.
