# Myntra BLINKDEAL Telegram Bot

Monitors a single Myntra product page every 5 minutes via GitHub Actions and
sends a Telegram alert the moment the **BLINKDEAL** coupon becomes available
on it. No server, no Oracle Cloud, no credit card — GitHub Actions' free tier
is the only infrastructure this needs.

Target product (override-able, see Configuration below):
`https://www.myntra.com/mailers/jewellery/kalyan-jewellers/kalyan-jewellers-24k-(999)-purity-shreeram-gold-coin-1-gms/31416910/buy`

## Read this first: known limitation

While building this, I tried to fetch the actual product page (and another
page on the same Myntra template) to inspect how the coupon data is
structured — HTML, embedded JSON, or a separate API call, as the brief asked.
**Both attempts were served a generic "Site Maintenance / Something went
wrong" block page instead of the real product page.** That's a strong signal
Myntra's anti-bot system is rejecting the request outright, most likely based
on the requester's IP range or lack of a real browser fingerprint — not
something fixable with better headers.

This matters because **GitHub Actions runners use well-known datacenter IP
ranges** that many e-commerce anti-bot systems pre-block regardless of how
"browser-like" the request looks. There's a real chance this bot gets blocked
the same way once it's running on a schedule.

What this means in practice:

- The parser (`coupon_is_active` in `check.py`) is written defensively — it
  tries several known patterns for where Myntra-style React pages embed page
  data as JSON (`window.__myx`, `window.__INITIAL_STATE__`,
  `__NEXT_DATA__`, generic `<script type="application/json">` blocks), plus a
  lower-confidence HTML fallback. But **none of these patterns were verified
  against a real, unblocked response** — they're informed guesses based on
  common Myntra/React conventions, documented as such in the code.
- The bot **does not try to defeat Myntra's bot detection** — no proxy
  rotation, no browser-fingerprint spoofing, no CAPTCHA solving. That crosses
  from "checking a public page" into deliberately evading a site's anti-abuse
  controls, which is out of scope for what I'll build.
- Instead, the bot **detects when it's being blocked** rather than failing
  silently: if it gets a non-200 response, a network error, or a page that
  looks like a maintenance/block/CAPTCHA page for `BLOCK_ALERT_THRESHOLD`
  consecutive runs (default 12 ≈ 1 hour), it sends you **one** Telegram
  heads-up saying so, instead of either silently never alerting again or
  spamming you every 5 minutes.

**Before relying on this for real**, run it manually once (Actions tab → "Run
workflow") and check the logs. If it reports "could not get a usable page" or
falls back to the lower-confidence HTML method, see *Verifying the parser
yourself* below.

## Project structure

```
myntra-blinkdeal-bot/
├── check.py                       # entry point: fetch, parse, alert, persist state
├── config.py                      # all settings, read from environment variables
├── notifier.py                    # Telegram sending, with retries/timeouts
├── requirements.txt                # requests, beautifulsoup4
├── state.json                     # persisted state (committed back by the workflow)
├── README.md
└── .github/workflows/monitor.yml  # the 5-minute schedule
```

## How it detects the coupon

Per the brief, this does **not** just grep the raw HTML for the string
"BLINKDEAL" — modern Myntra pages are React-rendered and typically ship their
real data as a JSON blob embedded in a `<script>` tag, not as plain text in
the markup. `check.py` does the following, in order:

1. Looks for known embedding patterns (`window.__myx = {...}`,
   `window.__INITIAL_STATE__ = {...}`, a `__NEXT_DATA__` script tag, and any
   `<script type="application/json">` blocks) and parses each as JSON.
2. Walks the parsed JSON recursively, but **only treats a match as real** if
   the string `BLINKDEAL` appears nested under a key that itself looks
   coupon/offer/deal/promo-related (e.g. `couponList`, `offerDetails`). This
   avoids false positives from the code appearing somewhere unrelated, like
   an analytics payload or a "coupons you don't qualify for" list.
3. If no embedded JSON is found at all (which would itself be a sign
   something's off, since Myntra's real pages should have some), it falls
   back to scanning HTML elements whose `class` attribute looks
   offer/coupon-related, as a lower-confidence last resort, and logs that
   it's doing so.

This logic is covered by unit tests run against synthetic fixtures
(true positive, true negative, and a deliberate false-positive trap) since I
couldn't test it against the real page — see *Verifying the parser yourself*.

## Verifying the parser yourself

Since I couldn't get past Myntra's block to confirm the real page structure,
please do this once before trusting the bot:

1. Open the product URL in a normal browser.
2. Right-click → **View Page Source**, and Ctrl+F for `BLINKDEAL` (search with
   the actual coupon visible on the page, e.g. during a known-active period,
   or any coupon code currently shown). Note what `<script>` tag or variable
   it's inside.
3. If it matches one of the patterns above, you're good. If not — e.g. if
   Myntra loads offers via a separate `fetch`/`XHR` call rather than embedding
   them in the initial HTML — open DevTools → **Network → Fetch/XHR**, reload,
   and look for a request whose response contains coupon data. That would mean
   switching `check.py` to call that API endpoint directly instead of parsing
   the HTML, which the brief anticipated as a possibility ("inspect whether
   coupon information comes from page HTML, embedded JSON, or API calls").
4. Either way, update `JSON_BLOB_PATTERNS` / `INTERESTING_KEY_HINT` in
   `check.py` (or add an API-based fetch function) to match what you find.

I'm glad to make that adjustment for you directly if you paste in what the
real embedded JSON or API response looks like.

## Setup

### 1. Get the code into your repo

Upload this project's files to `iiiiirahuliii/myntra-blinkdeal-bot`
(your existing private repo), preserving the folder structure above —
easiest via Codespaces (already set up per your notes) or `git push` from
your machine.

### 2. Confirm your GitHub Secrets

You mentioned these are already set on the repo:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Double check under **Settings → Secrets and variables → Actions** — the
workflow reads them from there, never from a committed file.

### 3. Enable Actions and do a manual test run

- Go to the **Actions** tab → enable workflows if prompted.
- Select **Monitor BLINKDEAL** → **Run workflow** to trigger it manually
  once, instead of waiting up to 5 minutes for the schedule.
- Check the run's logs for either a clean "BLINKDEAL not active" / "BLINKDEAL
  found" line, or the block/fallback warnings described above.

Once a manual run looks healthy, the `cron: "*/5 * * * *"` schedule in
`.github/workflows/monitor.yml` takes over automatically — no further action
needed.

### 4. (Optional) Adjust which product/coupon is tracked

Without editing code, you can override these via repo **Settings → Secrets
and variables → Actions → Variables** (or by adding `env:` entries in the
workflow step) — see the Configuration table below.

## How state persists across runs

Each GitHub Actions run starts in a brand-new, throwaway container, so
`state.json` is the only thing that remembers "was BLINKDEAL already active
last time I checked" between runs (otherwise every run would treat the deal
as brand-new and you'd get a Telegram message every 5 minutes for as long as
it stays live). The workflow's last step commits the updated `state.json`
back to the repo after every run — that's why the workflow needs `contents:
write` permission. The commit message includes `[skip ci]` so it doesn't
accidentally trigger anything else, and the job only commits when the file
actually changed.

## Staying within the free tier

Checking every 5 minutes adds up to roughly 8,600 GitHub Actions minutes a
month (GitHub bills each run as a minimum of 1 minute, even though the
actual work takes a few seconds). Private repos on the GitHub Free plan
only get 2,000 free minutes a month — at this pace, that runs out in under
a week, and (assuming no payment method is on file) GitHub simply stops
running the workflow until the quota resets next month, rather than
charging you. Public repos get unlimited free Actions minutes with no
catch.

Two ways to keep it running continuously and genuinely free:

- **Make the repo public** (recommended). Nothing in this code is
  sensitive — `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` stay protected as
  GitHub Secrets regardless of repo visibility. Settings → scroll to
  "Danger Zone" → Change visibility → Public.
- **Keep it private, check less often.** In
  `.github/workflows/monitor.yml`, change the `cron` line from
  `*/5 * * * *` to `*/30 * * * *` (every 30 minutes) to comfortably fit
  inside the 2,000-minute budget.

(GitHub Codespaces has a separate free allowance — 60 hours/month on the
default machine — but that's only spent while you have a Codespace open for
development. It's unrelated to the scheduled bot, which runs on GitHub
Actions, not Codespaces.)

## Configuration

All of these are environment variables with sensible defaults, read in
`config.py`. Set them as repository **Variables** (not Secrets, unless
sensitive) under **Settings → Secrets and variables → Actions**, and pass
them into the workflow's `env:` block if you want to override a default.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Yes | — | Telegram chat to notify |
| `PRODUCT_URL` | No | the Kalyan Jewellers coin URL from the brief | which product page to monitor |
| `COUPON_CODE` | No | `BLINKDEAL` | which coupon to watch for |
| `REQUEST_TIMEOUT_SECONDS` | No | `15` | HTTP timeout per attempt |
| `MAX_RETRIES` | No | `3` | retry attempts for both fetching the page and sending Telegram messages |
| `RETRY_BACKOFF_SECONDS` | No | `2` | base delay between retries (doubles each attempt) |
| `BLOCK_ALERT_THRESHOLD` | No | `12` | consecutive unreadable checks (~1 hour at 5-min cadence) before sending one "I might be blocked" heads-up |
| `STATE_FILE_PATH` | No | `state.json` | where state is persisted |
| `LOG_LEVEL` | No | `INFO` | Python logging level |

## Testing locally

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=your_token
export TELEGRAM_CHAT_ID=your_chat_id
python check.py
```

Check `state.json` afterward to see what was recorded, and the console
output for `[INFO]`/`[WARNING]` lines describing what happened.

## Future enhancements (not built yet — out of scope for this handoff)

Per the brief, these are intentionally deferred rather than half-built:

- `/add`, `/remove`, `/list`, `/status` Telegram bot commands
- Monitoring multiple products at once
- Price-drop alerts, stock alerts
- (`state.json`'s duplicate-suppression design already anticipates extending
  to a dict keyed by product ID, so adding multi-product support later
  shouldn't require a rewrite of the state model — just widening
  `DEFAULT_STATE` from one set of fields to one set per tracked product.)
