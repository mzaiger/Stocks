# Stock Board

A static stock board in the same style as Movie Board. It ranks stocks by three scores, like Movie Board ranks by Rotten Tomatoes, IMDb and Box Office:

| Tab | Like (Movies) | Score (0 to 100) | Source |
|---|---|---|---|
| **Gainers** | Rotten Tomatoes | Price gain since the previous close, for stocks on FMP's top-gainers list | FMP |
| **Most Active** | IMDb | Dollars traded (price x volume), for stocks on FMP's most-active list | FMP |
| **Buzz** | Box Office | Reddit mentions in the last 24 hours | ApeWisdom |
| **All** | All | The three scores averaged evenly, **only for stocks on at least 2 of the 3 rankings** | |

A Google Apps Script timer (the same pattern as SlickDealsTracker) triggers a GitHub Actions workflow every 15 minutes while the market session is open; the workflow takes a snapshot and commits it as JSON. `index.html` (GitHub Pages) reads `data/board.json`.

## API key (GitHub secret)

| Secret name | Service | Used for |
|---|---|---|
| `FMP_Key` | [Financial Modeling Prep](https://site.financialmodelingprep.com/developer) (free plan, 250 requests/day) | Biggest gainers, most actives, quotes for Reddit-only tickers, company websites (profile) |

Add it under **Settings > Secrets and variables > Actions**. ApeWisdom needs no key. Locally, set an environment variable named `FMP_Key`.

## Files

| File | What it is |
|---|---|
| `index.html` | The page: tabs, time frames, favorites, stock tiles |
| `stocks.py` | Does all the work. `run` (one snapshot), `rebuild` (board from saved snapshots, no key), `check` (test the APIs), `demo` (fake data for previewing) |
| `.github/workflows/stocks.yml` | Runs on a `repository_dispatch` event (`trigger-stocks-pull`) or manually; no cron schedule. Exits by itself when the market session is closed |
| `apps-script/Code.gs` | The Google Apps Script timer that sends the event. Paste it into script.google.com |
| `data/board.json` | What the page reads: every stock's scores for every time frame, rebuilt on each run |
| `state/snaps/*.json` | One small file per snapshot, kept for the longest time frame plus 2 hours, then deleted |
| `state/usage.json` | FMP requests spent today |
| `state/meta.json` | Cached price/exchange/market cap for Reddit-only tickers |
| `state/profiles.json` | Company websites from FMP's company profile, remembered for 90 days (14 days if a company had none) |
| `requirements.txt` | `requests`, `tzdata` |

## First run

1. Add the `FMP_Key` secret.
2. **Actions > Update stocks > Run workflow**, tick **Run even when the market is closed**. This makes one snapshot outside market hours so you can see it work.
3. Optional: `FMP_Key=... python stocks.py check` prints what each API returned (HTTP status, row count, field names). It uses 5 of the day's 250 requests (gainers, actives, batch quote, quote, profile). Run it if a tab looks empty.
4. Turn on GitHub Pages for the repo. To preview with fake data first: `python stocks.py demo`, then `python -m http.server` and open `http://localhost:8000/index.html?data=demo_data`.
5. Set up the Apps Script timer (next section).

## Timing (Google Apps Script)

GitHub's cron schedule is not used. `apps-script/Code.gs` sends a `repository_dispatch` event, the same way SlickDealsTracker works:

| Function | What it does |
|---|---|
| `triggerStocksWorkflow` | Sends `trigger-stocks-pull` to the repo, but only Monday to Friday, 4:00 AM to 8:00 PM Eastern (checked in Eastern time, so daylight saving is handled) |
| `installStocksTrigger` | Run once: creates the time-driven trigger (every 15 minutes) and removes any older stock trigger |
| `testStocksDispatch` | Sends the event even when the market is closed (the workflow gets `force = true`), to test the whole chain |

Setup:

1. At [script.google.com](https://script.google.com/home/) open the project you use for SlickDealsTracker (or make a new one) and paste in `apps-script/Code.gs`. Check the `STOCKS_REPO` line at the top (`mzaiger/Stocks`) matches the repo's real name.
2. **Project Settings > Script Properties**: the `GITHUB_TOKEN` property. If it is the token you already use for SlickDealsTracker and it has the `repo` scope, it works for this repo too and you add nothing. A new fine-grained token needs access to this repo with **Contents: Read and write**.
3. Run `testStocksDispatch` once from the editor and approve the permissions. In the execution log you want "Stocks workflow dispatched." (HTTP 204), and a run should appear under the repo's Actions tab. HTTP 401 or 403 means the token; 404 means the repo name or the token's access to it.
4. Run `installStocksTrigger` once. Check **Triggers** (clock icon): one time-driven trigger, every 15 minutes.

Notes:
* Apps Script fires "every 15 minutes" at slightly irregular moments, and the snapshot windows allow for that. `stocks.py` also skips any run that comes less than 40% of an interval after the previous one, so a double trigger or an extra manual run cannot burn the FMP limit.
* Market holidays are checked by `stocks.py`, not the Apps Script (about 9 wasted starts a year that exit immediately).
* Triggers are not exact, but runs are not skipped the way GitHub cron runs can be. Apps Script also keeps its own daily quotas (a consumer account has about 90 minutes of trigger runtime a day; this uses about a second per run).
* `repository_dispatch` workflows only run from the repo's default branch.

## When it runs

* **Business days only.** The script checks the real Eastern time and the NYSE holiday calendar (New Year's, MLK, Presidents, Good Friday, Memorial, Juneteenth, July 4, Labor, Thanksgiving, Christmas, with the weekend rules). Early-close days are not special-cased.
* **4:00 AM to 8:00 PM Eastern**: pre-market (4:00 to 9:30), regular (9:30 to 4:00), after-hours (4:00 to 8:00). Each snapshot is labelled with its session. Change the window with `SESSION_START` / `SESSION_END` if you want regular hours only.
* **Daylight saving:** both the Apps Script and `stocks.py` check the real Eastern time, so EST and EDT both work; a run outside the session exits immediately without calling any API.
* If a trigger is ever late or missed, a window just holds one fewer snapshot.
* **Extended hours caveat:** FMP's documentation describes the gainers and most-active lists as updating during regular trading hours. If the lists do not change before 9:30 or after 4:00, the script sees identical prices and buzz and saves nothing, so you do not get duplicate snapshots (it still spends its requests). If that happens every day, set `SESSION_START=09:30` and `SESSION_END=16:00` in the workflow `env` to stop spending requests on it.

## Rate limits (FMP free: 250 requests per day)

Each run uses: gainers (1), most actives (1), and one batch quote for Reddit-only tickers (1) = 3 requests.

| Interval | Runs per day (16 hours) | Requests per day | Fits in 250? |
|---|---|---|---|
| 15 min | 64 | 192 (128 without the quote call) | Yes, about 40 spare |
| 30 min | 32 | 96 | Yes |
| 1 hour | 16 | 48 | Yes |
| 10 min | 96 | 288 (192 without the quote call) | Only without the quote call |

15 minutes is the fastest interval that fits with everything on. The script also protects the limit itself: it counts requests per Eastern day in `state/usage.json` and keeps back 3 requests for every run still to come today (gainers, actives, quote) plus a margin of 20. **Only what is left over is spent on the optional calls**: company profile lookups (up to 4 per run, most important stocks first: on the most rankings, then best rank) and single-quote fallbacks. At 15 minutes that leaves roughly 35 profile lookups a day, and each website is remembered for 90 days, so the cache fills within a few days. If the plan does not include the batch quote or profile endpoints, it notices (HTTP 402/403), stops trying for the day and carries on without them. ApeWisdom is a free public service with no stated limit; one request per run is light.

### Changing the interval

Change `INTERVAL_MIN` in the workflow **and** `STOCKS_INTERVAL_MINUTES` in `Code.gs` to the same value, then run `installStocksTrigger` again. The page builds its time frame buttons from the interval:

| Interval | Time frames | Apps Script trigger |
|---|---|---|
| 15 | 15 min, 30 min, 1 h, 4 h, 12 h, 24 h | `everyMinutes(15)` |
| 30 | 30 min, 1 h, 4 h, 12 h, 24 h | `everyMinutes(30)` |
| 60 | 1 h, 2 h, 4 h, 12 h, 24 h | `everyHours(1)` (replace `.everyMinutes(...)` in `installStocksTrigger`) |

## Scores

Each snapshot scores every stock on the three axes, then the scores are **averaged over all snapshots in the time frame**. A stock only scores on a ranking if it is on that list, and a snapshot where it is on none of the lists counts as 0, so a stock that stays on the lists beats one that spiked once.

* **Gainers:** `100 x ln(1 + change%) / ln(51)` for stocks on the top-gainers list, so +5% is about 45, +10% about 61, +20% about 77, +50% or more is 100.
* **Most Active:** dollars traded on a log scale, $1M = 0 and $10B = 100, for stocks on the most-active list. If volume is missing from the list, its rank on the list is used instead.
* **Buzz:** `100 x ln(1 + mentions) / ln(1 + mentions of the most-mentioned stock)` from ApeWisdom's 24-hour mention count. The Buzz chip also shows the change from the 24 hours before.
* **All:** the average of the three scores, **only if the stock is on at least 2 of the 3 rankings** (a score above 0 on at least two tabs) in that time frame. A stock on 3 of 3 beats one on 2 of 3 with similar scores because the missing one counts as 0.

**Time frames** are measured back from the latest snapshot (not from the clock), so on a weekend the 24-hour tab still shows the last trading day. After a weekend or overnight, the longer frames hold fewer snapshots; the tile says how many. The 12 and 24 hour frames are wall-clock hours, and the market session is only 16 of them.

## Filters (so small stocks do not ruin the board)

Applied when each snapshot is taken. All can be overridden with environment variables in the workflow.

| Filter | Default | Variable |
|---|---|---|
| Share price at least | $5 | `MIN_PRICE` |
| Exchange | NASDAQ, NYSE, AMEX, BATS, CBOE (no OTC) | `EXCHANGES` |
| Market cap at least (when known) | $300M | `MIN_MARKET_CAP` |
| Dollars traded at least (when known) | $5M | `MIN_DOLLAR_VOLUME` |
| Ticker must look like a real symbol | 1 to 6 letters | |

Reddit tickers that are not on the FMP lists get a price check (batch quote) so the same filters apply. If a Reddit ticker cannot be priced, it is left out, because the filters cannot run on it. Other settings: `BUZZ_FILTER` (default `all-stocks`; also `wallstreetbets`, `stocks`, ...), `BUZZ_PAGES` (100 tickers each), `BUZZ_LOOKUP` (how many top Reddit tickers get a price check, default 40).

## The page

Each tile, top to bottom:

1. **Rank and favorite:** the rank in the current tab and time frame (top right) and the favorite star (top left).
2. **Company icon:** the favicon of the company's website when the FMP profile has one (requested at 128 px and shown large), otherwise FMP's logo for the ticker, otherwise the ticker letters. Favicons are often small, so some look soft.
3. **Stock info:** the ticker (a link to Yahoo Finance), price, company name and exchange, change since the previous close, dollars traded, Reddit mentions with the change from the day before, the stock's rank on each of the three tabs ("Rankings (2 of 3)"), its score in this tab and the three component scores, and how many snapshots it was on the lists.
4. **Last in the data:** the date and time of the newest snapshot the stock appeared in (Eastern time) and how many minutes ago that was. The "minutes ago" updates every 30 seconds without reloading.
5. **Links at the bottom:** Yahoo Finance, the company website (only if known), TradingView, StockTwits and Reddit ($TICKER search).

Also: the four tabs, one row of time frame buttons built from the interval in `board.json`, and **★ Favorites** (starred stocks are saved in your browser; ones not on the lists right now show greyed out). No sorting buttons; the order is the rank. The page reloads the data every 5 minutes, and `?data=demo_data` loads the demo folder instead of `data/`.

## Notes

* Git history grows by one small snapshot file and a rewritten `board.json` per run (about 64 commits per day). The files are small and delta-compress well, but if the repo size bothers you, squash old history now and then.
* Data can be delayed and is not financial advice. FMP's free plan is for personal use; check its terms before putting the site to commercial use.
* The FMP logo (`images.financialmodelingprep.com/symbol/TICKER.png`) is only the second choice for the tile icon, and is also used when no website is known. Favicons come from Google's favicon service.
