# Nivesh Council

A local, one-click dashboard where a panel of named agents (Scout, Technician,
Fundamentalist, Newsdesk, Bull, Bear, Judge, Messenger) scan Indian stocks (NSE),
debate each shortlisted pick, and send BUY signals straight to your Telegram.
Everything runs on your own machine — no cloud backend.

Analysis only. No trades are ever placed. Not investment advice.

## How to run

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. **LLM debate engine (optional but recommended).** The app auto-detects a
   provider in this order:

   - **`claude_code`** — if you have [Claude Code](https://claude.com/claude-code)
     installed and are logged in (`claude` on your PATH, run `claude` once and
     `/login` with your Claude Pro/Max plan), the app shells out to it. This uses
     your existing subscription — no API key, no per-call billing.
   - **`anthropic`** — set `ANTHROPIC_API_KEY` in `.env`.
   - **`openai`** — set `OPENAI_API_KEY` in `.env`.
   - **deterministic fallback** — if none of the above are available (no CLI
     login, no key, no network), the app always falls back to a transparent
     rule-based scoring engine. The dashboard never crashes for lack of an LLM.

   You can force a specific engine with `LLM_PROVIDER=claude_code|anthropic|openai|deterministic`
   in `.env`.

3. **Telegram.** Get a bot token from [@BotFather](https://t.me/BotFather) and
   your numeric chat id from [@userinfobot](https://t.me/userinfobot). Copy
   `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`. Without these, the app still runs and shows verdicts —
   it just skips the Telegram send and shows a small notice in the UI.

4. Run the app:

   ```bash
   python app.py
   ```

   Open **http://127.0.0.1:5000** and click **Start agents**.

   - Use **Demo** mode first — it loads pre-built evidence bundles from
     `demo_data/*.json` and runs fully offline.
   - Use **Live** mode during NSE market hours (Mon–Fri, 09:15–15:30 IST) to
     pull real data via `yfinance` for whichever sector set you pick from the
     `universes/` dropdown (Automobile, IT, FMCG, ...).
   - Use **Auto** mode to skip picking a sector yourself — it finds today's
     biggest-moving NSE sectoral index (via `sector_auto.py`) and scans that
     sector's set automatically, then runs exactly like Live mode from there.
     See "Auto mode" below for how the sector gets picked.

## File layout

| File | Purpose |
|---|---|
| `app.py` | Flask server, background pipeline/state machine, SQLite audit, Telegram delivery |
| `scoring.py` | Deterministic rule-based agent scores + Judge (always-works fallback) |
| `llm.py` | LLM debate engine — provider auto-detection, prompt, grounding verifier |
| `data_sources.py` | Demo JSON loader + yfinance adapter + evidence bundle builder + screening |
| `sector_auto.py` | Auto mode — finds today's biggest-moving NSE sector and picks its `universes/*.json` set |
| `dashboard.html` | Single self-contained UI (inline CSS/JS, no build step, no external libraries) |
| `universes/*.json` | Editable NSE tickers per sector set, auto-discovered for the Live-mode dropdown and Auto mode |
| `config.py` | Tiny built-in `.env` loader — no secrets hardcoded anywhere |
| `demo_data/*.json` | A handful of realistic evidence bundles for offline demo mode |
| `agent_dashboard.db` | SQLite audit trail of runs + verdicts (created on first run) |
| `buy_logs/*.txt` | One full audit dump per fired BUY signal — technicals, fundamentals, news headlines, every seat's reasoning, and the verdict (created on first BUY) |

## Configuration (`.env`)

See `.env.example` for the full list: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` (optional), `LLM_PROVIDER`, `BRAND`,
`CONFIDENCE_THRESHOLD` (default 7), `AGENT_DELAY` (visual pacing, default 0.35s),
`SHORTLIST_PERCENT` (default 40), `PORT` (default 5000), `SL_PERCENT` (default 3),
`RISK_REWARD_RATIO` (default 2.5), `BUY_LOG_RETENTION_DAYS` (default 30),
`SCHEDULE_ENABLED` / `SCHEDULE_TIME` / `SCHEDULE_MODE` (see "Scheduled run" below).

`SHORTLIST_PERCENT` controls how many stocks Scout sends to the debate: it takes
this % of each cap-segment bucket (large/mid/small), rounded, ranked by today's
price change — e.g. 40% of a 10-stock bucket = 4, 40% of 12 = 5. A bucket with any
stocks at all still gets at least 1 shortlisted, so a small bucket can't round down
to zero and silently drop out of every run.

`SL_PERCENT` and `RISK_REWARD_RATIO` drive the Entry/Stop-loss/Take-profit shown on
BUY signals only: `stop_loss = entry - (entry * SL_PERCENT/100)` and
`take_profit = entry + (entry - stop_loss) * RISK_REWARD_RATIO`, where `entry` is
`evidence.price.live`. Both engines (`scoring.py`, `llm.py`) compute this from the
same formula in `scoring.compute_trade_levels()` — the LLM engine is only allowed to
echo the pre-computed numbers back, never generate its own.

The bot token is never printed to logs or the UI.

## Auto mode

`universes/*.json` holds one file per stock set — Live mode's dropdown lets you
pick one manually. Auto mode picks one *for* you: it finds whichever NSE
sectoral index moved the most today and scans that sector's set.

`sector_auto.py` does this in two steps:

1. **`fetch_sector_momentum()`** — calls NSE's live sectoral-indices API (the
   same data behind [NSE's heatmap](https://www.nseindia.com/market-data/live-market-indices/heatmap))
   and returns all 23 sectors ranked by today's % change, biggest gainer
   first. NSE blocks requests without a real session, so this first visits
   the homepage to collect cookies before calling the API — the same
   approach as the original standalone script this was built from
   (`~/Desktop/Algo/Indian Stocks/nse_sectoral_indices.py`), now ported into
   the app itself rather than shelling out to a file outside the project.
2. **`pick_auto_universe_set()`** — walks that ranked list from the top and
   picks the first sector that has a matching `universes/*.json` file.
   Matching isn't naive string equality: NSE's own naming doesn't always
   match a sensible filename (e.g. the live API returns "NIFTY OIL & GAS"
   while a natural filename is `NIFTY OIL AND GAS.json`), so it normalizes
   "&"/"AND" and tries a substring match before giving up on a sector and
   moving to the next-biggest mover. This was found and fixed by testing
   against the real API response, not assumed.

If today's single biggest mover doesn't have a matching file (e.g. NSE's
generic "NIFTY BANK" index moved most, but you only have `NIFTY PSU
BANK.json` / `NIFTY PVT BANK.json`), it doesn't just fail — it keeps walking
down the ranked list until it finds a sector you actually have a set for.
It only gives up, with a clear message surfaced via the run's `notice`
field, if NSE couldn't be reached at all, or literally none of the 23
sectors match anything in `universes/`. Auto mode never silently falls back
to an arbitrary set — same "don't guess" rule as everywhere else in this app.

Once a sector is picked, Auto mode runs exactly like Live mode from there —
same `data_sources.build_universe_evidence()` call, same scoring, same
Telegram delivery. The picked sector and its % move are recorded in
`STATE["auto_pick"]` for the dashboard footer, and flow through to the
`Sector:` line in Telegram/`buy_logs` the same way a manually-picked Live
set does.

## Scheduled run

Set `SCHEDULE_ENABLED=true` in `.env` (only takes effect when running via
`python app.py`, not needed for local testing) to have the app fire one run
automatically every weekday at `SCHEDULE_TIME` (24h, IST — default `09:20`),
using whichever mode `SCHEDULE_MODE` names (`demo` | `live` | `auto`; default
`auto`, so by default it's "fire Auto mode at 9:20am every trading day").

This is a small background thread (`_scheduler_loop()` in `app.py`), not a
new dependency. It polls every 30 seconds using real wall-clock time
(`datetime.now()`) rather than one long `time.sleep()` spanning the whole
wait — a long sleep isn't reliable across a real system sleep/wake cycle
(e.g. the laptop was actually asleep, not just screen-off, for part of the
wait), so a short poll loop that re-checks the actual clock every 30s
self-corrects regardless. It fires if the current time is at or up to
`SCHEDULE_GRACE_MINUTES` (10) past `SCHEDULE_TIME` — covering the time a
scheduled OS wake takes to actually resume everything — and only once per
calendar day, tracked so it can't double-fire while polling through that
window. It skips weekends, and never fires on top of an already-running
cycle (checks `STATE["running"]` first). One iteration's error can't take
down future scheduled runs; it's caught and the loop just waits and tries
again.

Since this runs unattended, a fired BUY signal sends a real Telegram message
the same as a manual run would — that's the point, but worth knowing before
turning it on.

### Running fully unattended overnight (macOS)

`SCHEDULE_ENABLED` only fires *inside* the app — it does nothing if the app
isn't running, and does nothing if the Mac is asleep. To get a genuinely
hands-off setup (Mac asleep overnight, everything happens on its own), you
need two more pieces, both outside this repo since they're OS-level, not
part of the app:

1. **Wake the Mac itself**, since a sleeping Mac runs no code at all:
   ```bash
   sudo pmset repeat wakeorpoweron MTWRF 07:50:00
   ```
   (time is in your Mac's own local timezone, not IST — see below)

2. **Auto-start the app**, since waking the Mac doesn't launch anything by
   itself — a macOS LaunchAgent handles this. `start_app.sh` (in this repo)
   is what it runs; it skips launching if the app's already running, so it
   can never cause a "port already in use" conflict with a copy you started
   yourself. The LaunchAgent definition itself lives outside the repo, at
   `~/Library/LaunchAgents/com.niveshcouncil.autostart.plist` (it's a
   per-machine system file, similar in spirit to a cron job), with a
   `StartCalendarInterval` entry for each weekday.

3. **Auto-stop the app** (optional), once the day's check is done —
   `stop_app.sh` (in this repo) finds whatever's listening on port 5000 and
   kills it; safe to run even if nothing's there. Wired up the same way, as
   `~/Library/LaunchAgents/com.niveshcouncil.autostop.plist`.

**Four different times are involved, and they use two different time
standards — worth being deliberate about:**

| # | What | Where | Time standard |
|---|---|---|---|
| 1 | Mac wakes up | `pmset` (macOS, not a project file) | Mac's own local timezone |
| 2 | App auto-starts | `com.niveshcouncil.autostart.plist` (macOS, not a project file) | Mac's own local timezone |
| 3 | App checks the market | `SCHEDULE_TIME` in `.env` | **Always IST** — hardcoded via `IST = ZoneInfo("Asia/Kolkata")` at the top of `app.py`, regardless of the Mac's own timezone |
| 4 | App auto-stops | `com.niveshcouncil.autostop.plist` (macOS, not a project file) | Mac's own local timezone |

#1, #2, and #4 are plain macOS tools with no concept of India — they only
know the Mac's own clock. #3 is this app's own code, and it's deliberately
independent of the Mac's timezone since the point is checking an Indian
market. This means #1, #2, and #4 need to be *manually* converted to match
#3 and kept in sync by hand — changing `SCHEDULE_TIME` doesn't update
`pmset` or either LaunchAgent automatically, since none of those know this
app exists.

**#4 needs one more thing kept in sync too: `SCHEDULE_GRACE_MINUTES`**
(10, set in `app.py`) — the app's own scheduler can fire anywhere up to that
many minutes after `SCHEDULE_TIME`, to cover a late wake-from-sleep. So #4
should always be set later than `SCHEDULE_TIME + SCHEDULE_GRACE_MINUTES`,
with enough extra buffer for the run itself to actually finish — a run that's
still going gets killed mid-analysis if #4 fires first, silently losing that
day's signals rather than erroring loudly.

## Swapping the data source

`data_sources.py` is the only file that talks to yfinance. It builds one
normalized "evidence bundle" per stock (price, 52-week range, technicals,
analyst consensus, fundamentals, news sentiment, and a `data_gaps` list for
anything it couldn't compute). If you have a richer feed — a broker API, paid
data, or an MCP connector — swap the fetch logic in that file and keep the
evidence-bundle shape identical; `scoring.py` and `llm.py` don't need to
change.

### Fundamentals (`evidence.fundamentals`)

Pulled from `yfinance`'s `info` dict: P/E (TTM & forward), P/B, ROE, gross/
operating/net margin, revenue & earnings growth YoY, debt/equity, current
ratio, dividend yield, promoter/insider & institutional holding %, EPS, book
value/share, free cash flow, market cap, and beta. `score_fundamentalist()`
in `scoring.py` uses ROE, growth, leverage, and promoter holding to adjust
its score; the LLM engine sees the whole section automatically since it just
serializes the evidence bundle into the prompt — no prompt change was needed.

A couple of yfinance's fields mix units in a way that's easy to get wrong, so
the conversions were checked against a live API call rather than assumed:
`profitMargins` / `operatingMargins` / `grossMargins` / `revenueGrowth` /
`earningsGrowth` / `returnOnEquity` / `heldPercentInsiders` /
`heldPercentInstitutions` are decimal fractions (×100 to get a percent), but
`debtToEquity` and `dividendYield` come back already percent-scaled on the
installed yfinance version — multiplying those by 100 again would be a 100x
bug. See the comment in `fetch_live_bundle()` for the full breakdown.

### Technicals (`evidence.technicals`)

Price history now pulls a full year (`period="1y"`), not just a month — needed
for a real 200-day average and to give RSI/MACD/Bollinger enough history to
be meaningful. On top of the original fields (RVOL, trend, day-range
position...), it now includes: 20/50/200-day SMAs and their bullish/bearish
alignment (50 vs 200), RSI(14) with an overbought (≥70) / oversold (≤30) /
neutral reading, MACD (line, signal, histogram, bullish/bearish), and
Bollinger Bands (20-period, 2 std dev) with where price sits between them.
`score_technician()` in `scoring.py` weighs the MA alignment, RSI extremes,
and MACD direction into its score.

These are hand-written formulas (`_compute_rsi()`, `_compute_macd()`,
`_compute_bollinger()` in `data_sources.py`), not a third-party TA library —
`pandas_ta`, the obvious off-the-shelf choice, currently requires Python
3.12+ and this project runs 3.11, so it wasn't usable. Each formula was
independently cross-checked against a from-scratch, non-vectorized
implementation on real data before being trusted.

**Two existing fields kept their original meaning on purpose.** Pulling a
full year of history could have silently changed what `rvol` and
`window_return_pct` mean — `rvol` (today's volume vs a recent baseline) still
uses only the last ~20 trading days for that baseline, not the whole year,
since it directly gates whether a BUY signal can fire at all
(`check_technical_confirmation()` uses `rvol >= 3`) and quietly redefining it
would have changed real trading decisions without anyone noticing.
`window_return_pct` likewise still means "return over the trailing ~1 month,"
not the full year now pulled for the other indicators.

Any field yfinance doesn't have for a given stock is left `null` and shows up
in `data_gaps` / as "n/a" in the `buy_logs` audit dump, same as every other
section — nothing here is invented when the source doesn't have it.

### Multi-year trend & quality (`evidence.fundamentals_trend`)

Everything in `fundamentals` above is a single snapshot — today's P/E, today's
ROE. This section instead pulls yfinance's actual annual financial statements
(`income_stmt`, `balance_sheet`, `cashflow`, typically ~4 clean years) and
looks at the *trend*, computed in `compute_fundamentals_trend()`:

- **`net_margin_trend`** — improving/declining/flat, comparing the latest
  year's net margin to the oldest available (±1 percentage point = "flat").
- **`revenue_growth_streak`** — how many of the recent years actually grew
  revenue vs. how many didn't (a single YoY % can hide an inconsistent track
  record; this doesn't).
- **`cfo_to_net_income_ratio`** / **`earnings_quality`** — does reported
  profit actually show up as cash? Compares operating cash flow to net
  income; a ratio persistently below 1 means the company is reporting profit
  its cash flow doesn't back up — a real red flag serious investors watch
  for, not something a P/E or ROE snapshot would ever catch.
- **`piotroski_f_score`** / **`piotroski_max`** / **`piotroski_breakdown`** —
  the standard Piotroski F-Score (Piotroski, 2000): 9 yes/no year-over-year
  checks across profitability, leverage, and efficiency, 1 point each. 8-9 is
  the textbook "strong" band, 0-2 "weak" — established methodology, not a
  scoring scheme invented for this app. `piotroski_max` is the number of
  criteria that were actually computable (can be less than 9 if a statement
  row is missing) — a stock is never silently penalized for missing data by
  padding the denominator up to 9. Every one of the 9 checks was manually
  verified against real numbers before being trusted; the full pass/fail
  breakdown (not just the final score) is written to `buy_logs` for that
  same reason — a bare "6/9" is much less checkable than seeing exactly
  which of the 9 criteria passed.

`score_fundamentalist()` weighs the F-Score, earnings quality, and margin
trend into its score. Needs at least 2 years of clean statement data to
compute anything — below that, every field here stays `null` rather than
guessing from a single year.

### News (`evidence.news`)

Two sources feed `evidence.news.recent`, each headline tagged with where it
came from:

- **Yahoo Finance** (`ticker.news` via yfinance) — same as before, often thin
  for Indian mid/small-caps.
- **RSS feeds** (Economic Times, LiveMint, Business Standard) — fetched once
  per Live run via `fetch_rss_headlines()`, not once per stock, then matched
  to each company by name (`match_rss_headlines_for_company()`). Matching
  uses the full company name with the corporate suffix stripped ("Cochin
  Shipyard Ltd." → search for "Cochin Shipyard"), not a single word — a
  single word like "Tata" or "Adani" would wrongly pull in headlines about a
  different company in the same business group. This trades some recall for
  precision: RSS supplements Yahoo's news, it doesn't replace it, so a
  missed match there just means one less headline, not wrong data.

Every headline still gets a quick word-list tone tag (`_classify_headline()`)
for the deterministic engine and for display, but the LLM engine is
explicitly told not to just trust that tag — it's instructed to read each
headline itself and form its own judgment, since the word-list approach
misreads mixed-signal headlines (e.g. "Profit rises but company warns of
headwinds" isn't simply positive). The `buy_logs` audit dump shows both: the
word-count tags under "NEWS HEADLINES", and the AI's own take under
"NEWSDESK", so you can see if they disagree.

Each headline also carries a `published` timestamp, normalized to IST no
matter which source it came from (RSS uses RFC 2822 dates, yfinance uses ISO
8601 or a Unix timestamp depending on version — both get parsed and converted
the same way). This is genuinely useful, not just cosmetic: yfinance's own
news for a stock is often weeks or months old, and the date makes that
obvious in the audit log instead of a stale headline silently reading like
fresh news.

## Grounding

Every number an agent cites must come from the evidence bundle — agents are
instructed to say "data unavailable" rather than invent a figure. `llm.py`
includes a small verifier that flags any number in an LLM's output that isn't
traceable back to the evidence; if verification fails to find a working
provider, `evaluate()` transparently falls back to the deterministic engine.

The same rule applies to the Entry/Stop-loss/Take-profit shown on BUY signals:
they're always the output of `scoring.compute_trade_levels()`, never something
either engine invents. The LLM is given those numbers as fixed inputs and told
to echo them back verbatim; the verifier separately checks that its echoed
`entry` / `stop_loss` / `take_profit` / `risk_reward_pct` match the formula
output exactly, and the final displayed values always come from the formula,
not from what the LLM returned.

## BUY signal audit logs

Every time a signal actually fires as a BUY (verdict is BUY *and* confidence
clears `CONFIDENCE_THRESHOLD` — the same gate that triggers the Telegram
message), `app.py` writes a full plain-text audit dump to `buy_logs/`, named
`<SYMBOL>_<YYYYMMDD_HHMMSS>.txt`. It captures everything that went into the
call: price/52-week range/technicals, analyst consensus and target, every news
headline with its tone, each seat's score and reasoning (Bull, Bear,
Technician, Fundamentalist, Newsdesk), the Judge's verdict and rationale, the
entry/stop-loss/take-profit levels, and the grounding verifier's result
(`verified` + any unverified claims). A logging failure never breaks the
pipeline — `write_buy_log()` catches its own exceptions.

Files older than `BUY_LOG_RETENTION_DAYS` (default 30) are auto-deleted by
`cleanup_old_buy_logs()`, which runs on app startup and again at the start of
every run cycle — so `buy_logs/` never grows unbounded even if the server
stays up for weeks. A cleanup failure is caught the same way and never breaks
a run.
