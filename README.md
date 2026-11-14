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
| `FMP_Key` | [Financial Modeling Prep](https://site.financialmodelingprep.com/developer) (free plan, 250 requests/day) | Biggest gainers, most actives, company websites (profile); the screener and batch quote only if your plan allows them |
| `GEMINI_KEY` | Google Gemini (the same secret name TrendingTopics and SportsDashboard use) | The "why is it trending" line on each tile. Optional: without it the tiles simply have no line |

Add them under **Settings > Secrets and variables > Actions**. ApeWisdom, Yahoo Finance (quotes and headlines) and Google News need no key. Locally, set environment variables with the same names.

## Files

| File | What it is |
|---|---|
| `index.html` | The page: tabs, Newest / Ranking sort, favorites, stock tiles |
| `stocks.py` | Does the work. `run` (one snapshot), `rebuild` (board from saved snapshots, no key), `check` (test the APIs), `demo` (fake data for previewing) |
| `why.py` | Fetches recent headlines and makes the one Gemini call per run that writes the "why is it trending" lines |
| `.github/workflows/stocks.yml` | Runs on a `repository_dispatch` event (`trigger-stocks-pull`) or manually; no cron schedule. Exits by itself when the market session is closed |
| `apps-script/Code.gs` | The Google Apps Script timer that sends the event. Paste it into script.google.com |
| `data/board.json` | What the page reads: every stock on the latest snapshot with its numbers and scores, rebuilt on each run |
| `state/snaps/*.json` | One small file per snapshot, kept for about a day (for "trending since"), then deleted |
| `state/usage.json` | FMP requests spent today |
| `state/meta.json` | Cached quote info, and which optional FMP endpoints the plan does not allow today |
| `state/profiles.json` | Company website and market cap from FMP's company profile, remembered for 30 days (14 days if a company had no website) |
| `state/why.json` | The "why trending" line for each stock and when it was written |
| `state/trending.json` | When each stock first got on the board (without going off it), for the "35m ago" on the tile |
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
| `triggerStocks` | The function your **15-minute trigger runs**. It sends `trigger-stocks-pull` to the repo, but only Monday to Friday, 4:00 AM to 8:00 PM Eastern (checked in Eastern time, so daylight saving is handled) |
| `testStocks` | Run by hand once: sends the event even when the market is closed (the workflow gets `force = true`) |
| `sendStocksDispatch_` | Helper that calls the GitHub API. Never run it directly |

Setup:

1. At [script.google.com](https://script.google.com/home/) open the project you use for the timers and paste in `apps-script/Code.gs`, **at the top level of the file** (not inside another function). Check the `STOCKS_REPO` line (`mzaiger/Stocks`) matches the repo's real name, and save.
2. **Project Settings > Script Properties**: the `GITHUB_TOKEN` property. If it is the token you already use for SlickDealsTracker and it has the `repo` scope, it works for this repo too and you add nothing. A new fine-grained token needs access to this repo with **Contents: Read and write**.
3. Choose `testStocks` in the function dropdown and click Run; approve the permissions. In **Executions** you want "Stocks workflow dispatched." (HTTP 204), and an "Update stocks" run should appear under the repo's Actions tab. HTTP 401 or 403 means the token; 404 means the repo name or the token's access to it.
4. On the **Triggers** page (clock icon) make sure there is **one** time-driven trigger, every 15 minutes, that runs `triggerStocks`. If you already have a 15-minute trigger, change its function to `triggerStocks` instead of adding a second one.

Notes:
* Apps Script fires "every 15 minutes" at slightly irregular moments, and the snapshot windows allow for that. `stocks.py` also skips any run that comes less than 40% of an interval after the previous one, so a double trigger or an extra manual run cannot burn the FMP limit.
* Market holidays are checked by `stocks.py`, not the Apps Script (about 9 wasted starts a year that exit immediately).
* The Calendar permission some people see in the prompt does not come from this file; the permission prompt covers everything in the project. This file only uses an external request (to GitHub) and the script property.
* `repository_dispatch` workflows only run from the repo's default branch.

## All the limits per run (15-minute interval)

| Service | Requests per run | Per day (64 runs) | Limit |
|---|---|---|---|
| FMP | 2 (gainers, actives) plus up to 4 profile lookups and a few single quotes if the budget allows | 128 plus the optional ones, capped by the script | 250 per day |
| ApeWisdom | 1 | 64 | none stated |
| Yahoo quotes | 3 (cookie, crumb, batch quote) plus up to 60 one-by-one lookups only for stocks the batch missed | about 200 to 5,000 | not published; about 2,000 per hour per IP is reported |
| Yahoo / Google News headlines | 1 or 2 per stock that needs a "why trending" line (at most 25) | a few hundred | none stated |
| **Gemini** | **1 call per run, rewriting the line for every stock on the board** | **64** | free tier is reported at about 500 per day and 10 per minute for the flash-lite models; Google shows the real figures in AI Studio |
| GitHub Actions | 1 run | 64 | unlimited for public repos |
| Google Apps Script | 1 call | 64 | well inside its quotas |

Gemini only goes over the limit if a model is unavailable: the chain `3.5 -> 3.1 -> 2.5 flash-lite` then makes up to 3 attempts for that run. A call carries at most `WHY_MAX_PER_RUN` (100) stocks, roughly 20,000 to 30,000 input tokens. To cut calls, set `WHY_TTL_HOURS=4` (only stocks with no line or a 4-hour-old line are sent); to cut the size of a call, lower `WHY_MAX_PER_RUN`. The headline feeds are fetched 6 at a time (`NEWS_WORKERS`), about 100 to 200 small requests per run.

## When it runs

* **Business days only.** The script checks the real Eastern time and the NYSE holiday calendar (New Year's, MLK, Presidents, Good Friday, Memorial, Juneteenth, July 4, Labor, Thanksgiving, Christmas, with the weekend rules). Early-close days are not special-cased.
* **4:00 AM to 8:00 PM Eastern**: pre-market (4:00 to 9:30), regular (9:30 to 4:00), after-hours (4:00 to 8:00). Each snapshot is labelled with its session. Change the window with `SESSION_START` / `SESSION_END` if you want regular hours only.
* **Daylight saving:** both the Apps Script and `stocks.py` check the real Eastern time, so EST and EDT both work; a run outside the session exits immediately without calling any API.
* If a trigger is ever late or missed, a window just holds one fewer snapshot.
* **Extended hours:** FMP's gainers and most-active lists are described as updating during regular hours, so before 9:30 and after 4:00 they may not move. The prices and changes on a tile come from Yahoo's quote, which has pre-market and after-hours fields. The price block shows the **regular-session price** with its **point change and percent change** and an arrow, as on a quote page (for example `$162.85 -3.34 -2.01% ▼`); in pre-market and after-hours a **second line** shows the **Pre** or **AH** price with its own point and percent change (`AH $161.04 -1.81 -1.11% ▼`). Before the open the first line is the last close and the previous day's change. As on Yahoo, the pre-market change is against the previous close and the after-hours change against today's close. On nights, weekends and holidays the **last after-hours trade stays shown** (Yahoo reports the market as closed but still has it) until pre-market trading starts; a stale after-hours price from before the last close is ignored. **The Chg box shows only the change the ranking uses**: the regular-session percent normally, and the **pre-market or after-hours percent (labelled "Pre chg" / "AH chg") while there is one**. So in the evening Gainers ranks stocks by their after-hours move, not the day's. This needs the Yahoo quote to work; the chart-feed and FMP fallbacks only have regular-session numbers. **Volume stays the regular-session volume** (Yahoo's free feed has no reliable pre-market volume), so in the morning it is the previous day's total. To stop spending requests outside regular hours, set `SESSION_START=09:30` and `SESSION_END=16:00` in the workflow `env`.

## Rate limits (FMP free: 250 requests per day)

Each run uses 3 requests: gainers (1), most actives (1) and one **screener** call (1) that returns volume and market cap for every stock that passes the filters. Gemini and the headline feeds do not use FMP requests.

| Interval | Runs per day (16 hours) | Requests per day | Fits in 250? |
|---|---|---|---|
| 15 min | 64 | 192 | Yes, about 40 spare |
| 30 min | 32 | 96 | Yes |
| 1 hour | 16 | 48 | Yes |
| 10 min | 96 | 288 | No |

15 minutes is the fastest interval that fits. The script protects the limit itself: it counts requests per Eastern day in `state/usage.json` and keeps back 3 requests for every run still to come today plus a margin of 20. **Only what is left over is spent on the optional calls**: company profile lookups (up to 4 per run, most important stocks first: on the most rankings, then best rank) and single quotes. At 15 minutes that leaves roughly 35 optional calls a day; websites and market caps are remembered for 30 days, so the cache fills within a few days.

**Where the numbers on a tile come from.** Every stock on the board, including Reddit-only ones, needs price, change, volume and market cap. FMP's free plan blocks the screener and the batch quote (HTTP 402) and its most-active list has no volume, so each run tries these in order and fills in whatever is still missing:

1. FMP screener (1 request, only if your plan has it).
2. **Yahoo Finance batch quote**, no key and no FMP request: all gainers, most-active and Reddit tickers in one call (plus a cookie and a crumb request). It uses `curl_cffi` to look like Chrome, because Yahoo answers 429 to plain Python requests.
3. **Yahoo chart feed**, one symbol at a time (up to `YAHOO_CHARTS_PER_RUN` = 60), for any stock the batch call missed or if the batch call was refused. It gives price, change and volume.
4. **Last known values:** a missing price, change or volume is kept from the previous snapshot if that is less than 3 intervals old; a missing market cap comes from the previous snapshot or the profile cache.
5. **FMP quotes** (a few single calls per run, within the request budget) for whatever is still missing, most important stocks first. A symbol FMP does not know is not asked about again for a day.

Yahoo is an unofficial feed that can change or block cloud IPs without notice; after a refusal the script waits 60 minutes (`YAHOO_RETRY_MINUTES`). A Reddit-only ticker that no source can price is left out, because the filters cannot run on it. Run `python stocks.py check` to see which sources your setup can reach.

### Changing the interval

Change `INTERVAL_MIN` in the workflow **and** the trigger's interval on the Apps Script Triggers page to the same value (5, 10, 15 or 30 minutes, or hourly).

The page shows the latest snapshot only, so there are no time frame buttons by default. To bring them back, set `BOARD_WINDOWS` in the workflow `env`, for example `BOARD_WINDOWS: "60,240,1440"`: the board then also averages the scores over the last 1, 4 and 24 hours (measured back from the latest snapshot), and the page shows one button per time frame.

## Scores

Each snapshot scores every stock on the three axes. A stock only scores on a ranking if it is on that list. (If you turn on extra time frames with `BOARD_WINDOWS`, the scores are averaged over all snapshots in the frame, and a snapshot where the stock is on none of the lists counts as 0, so a stock that stays on the lists beats one that spiked once.)

* **Gainers:** `100 x ln(1 + change%) / ln(51)` for stocks on the top-gainers list, so +5% is about 45, +10% about 61, +20% about 77, +50% or more is 100.
* **Most Active:** dollars traded on a log scale, $1M = 0 and $10B = 100, for stocks on the most-active list. If volume is missing from the list, its rank on the list is used instead.
* **Buzz:** `100 x ln(1 + mentions) / ln(1 + mentions of the most-mentioned stock)` from ApeWisdom's 24-hour mention count. The Buzz chip also shows the change from the 24 hours before.
* **All:** the average of the three scores, **only if the stock is on at least 2 of the 3 rankings** (a score above 0 on at least two tabs). A stock on 3 of 3 beats one on 2 of 3 with similar scores because the missing one counts as 0.

**Time frames** exist only if you set `BOARD_WINDOWS` (see above); they are measured back from the latest snapshot (not from the clock), so on a weekend the 24-hour frame still shows the last trading day.

## Filters (so small stocks do not ruin the board)

Applied when each snapshot is taken. All can be overridden with environment variables in the workflow.

| Filter | Default | Variable |
|---|---|---|
| Share price at least | $5 | `MIN_PRICE` |
| Exchange | NASDAQ, NYSE, AMEX, BATS, CBOE (no OTC) | `EXCHANGES` |
| Market cap at least (when known) | $300M | `MIN_MARKET_CAP` |
| Dollars traded at least (when known) | $5M | `MIN_DOLLAR_VOLUME` |
| Shares traded today at least (screener) | 500,000 | `MIN_SHARE_VOLUME` |
| Ticker must look like a real symbol | 1 to 6 letters | |

**How the filters use the screener (if your plan has it):** each run asks FMP's screener for every stock with a market cap, price and share volume above the limits (`isActivelyTrading`, no ETFs or funds, up to `UNIVERSE_LIMIT` = 1000 rows). That one call supplies the volume and market cap shown on the tiles. When the answer is complete (more than 200 rows and not cut off by the limit), any stock on the gainers, most-active or Reddit list that is **not** in it is dropped: it is below a limit, or it is an ETF or fund. If the answer is missing or looks cut off (always the case on the free plan), nothing is dropped this way: the price, exchange, market cap and dollar-volume limits are applied to each stock using the Yahoo or FMP quote values, and a limit is skipped when its value is unknown.

Reddit tickers get their price, change, volume and market cap from the screener or the Yahoo batch quote (all 100 in one request); if neither works, up to `BUZZ_LOOKUP` (40) of them get an FMP quote instead, and a ticker that cannot be priced is left out. Other settings: `BUZZ_FILTER` (default `all-stocks`; also `wallstreetbets`, `stocks`, ...) and `BUZZ_PAGES` (100 tickers each).

## The page

Each tile, top to bottom:

1. **Rank and favorite:** the rank in the current tab (top right) and the favorite star (top left).
2. **Company icon:** the favicon of the company's website when the FMP profile has one (requested at 128 px and shown large), otherwise FMP's logo for the ticker, otherwise the ticker letters. Favicons are often small, so some look soft.
3. **Ticker, then the price block:** the ticker links to Yahoo Finance. Under it the regular-session price with its point change, percent change and an arrow ▲/▼; in pre-market and after-hours a second line with the **Pre** or **AH** price and its own change.
4. **Company name and market cap**, for example "Robinhood Markets · $210B".
5. **Why it is trending:** the Gemini line (see below), if there is one yet.
6. **Three boxes:** **Chg** (only the change the ranking uses: the regular-session percent, or the pre-market / after-hours percent while there is one, labelled "Pre chg" / "AH chg"), **Volume** (shares traded today, for example 52.3M) and **Buzz** (Reddit mentions in 24 hours with the change from the day before side by side, for example 21K ▲11%). Under each, on a line level across the three boxes, is the stock's rank on that tab (large and dark), or "—" if it is not on that ranking.
7. **How long it has been trending:** for example "35m ago", "10h 5m ago" (same wording as the other boards). It counts from **the first time the stock got on the board without having gone off it**. A stock that is missing from more than 2 snapshots in a row (`OFF_BOARD_TOLERANCE`, 30 minutes at the 15-minute interval) has gone off the board and starts a new count when it returns; one or two missed snapshots do not reset it. Snapshots only exist while the market session is open, so nights, weekends and holidays do not break the count. The start times are kept in `state/trending.json` (rebuilt from the saved snapshots the first time). Hover for the date and time. It updates every 30 seconds without reloading.
8. **Links at the bottom:** the company website (only if known), Yahoo Finance, TradingView, StockTwits and Reddit ($TICKER search).

The scores still decide the order of the tabs, but they are .

Also: the four tabs; **Sort: Newest / Ranking** (Newest puts the stocks that started trending most recently first, the same "35m ago" shown on each tile; Ranking orders by the tab's score; the choice is remembered); and **★ Favorites** (starred stocks are saved in your browser; ones not on the lists right now show greyed out). The page reloads the data every 5 minutes, and `?data=demo_data` loads the demo folder instead of `data/`.

## Why is it trending (Gemini)

Copied from the TrendingTopics approach. `why.py`:

1. After each snapshot, takes **every stock in it** (`WHY_TTL_HOURS` is 0 by default: lines are rewritten on every run). Set `WHY_TTL_HOURS=4` to ask only for stocks with no line yet or a 4-hour-old line.
2. Fetches up to 5 recent headlines per such stock from Yahoo Finance's ticker feed (Google News as the fallback; no key needed).
3. Sends all of them, with the stock's numbers (change, volume, market cap, which rankings, Reddit mentions), in **one Gemini call per run** (at most `WHY_MAX_PER_RUN` = 100 stocks, most important first) and asks for one or two short sentences. The model chain is `gemini-3.5-flash-lite`, then `gemini-3.1-flash-lite`, then `gemini-2.5-flash-lite`, tried in that order when a model is unavailable.
4. The prompt tells the model to use only the material it is given, to say so plainly when the headlines do not explain the move, and not to predict or advise. Nothing is written back if Gemini fails; the old line stays and the stock is asked about again on the next run.

Lines are stored in `state/why.json` for 7 days and copied into `data/board.json`. Without `GEMINI_KEY` the tiles just have no line. A line is only as good as the headlines found, so some will honestly say that no clear news was found.

## Notes

* Git history grows by one small snapshot file and a rewritten `board.json` per run (about 64 commits per day). The files are small and delta-compress well, but if the repo size bothers you, squash old history now and then.
* Data can be delayed and is not financial advice. FMP's free plan is for personal use; check its terms before putting the site to commercial use.
* The FMP logo (`images.financialmodelingprep.com/symbol/TICKER.png`) is only the second choice for the tile icon, and is also used when no website is known. Favicons come from Google's favicon service.
