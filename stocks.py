#!/usr/bin/env python3
"""Stock Board data builder.

    python stocks.py run       take one snapshot (only inside the market session) and rebuild data/board.json
    python stocks.py rebuild   rebuild data/board.json from the saved snapshots (no API calls, no key needed)
    python stocks.py check     call each API once and print what came back (uses 6 FMP requests)
    python stocks.py demo      write fake snapshots + board into demo_data/ (open index.html?data=demo_data)

FMP (secret FMP_Key): biggest gainers, most actives, company websites (profile), and when the plan allows it a screener / batch quote.
Yahoo Finance (no key, unofficial): one batch quote per run for volume, market cap and change.
Gemini (secret GEMINI_KEY, see why.py): a short 'why is it trending' line per stock, from recent headlines.
ApeWisdom (no key): Reddit mention counts.
Each run saves one small snapshot in state/snaps/. The board is rebuilt from those snapshots, so every
time frame (15 min ... 24 hours) is just a different slice of the same files.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import why

try:
    from curl_cffi import requests as creq            # impersonates Chrome's TLS fingerprint; Yahoo answers 429 to plain python requests
except ImportError:                                       # pragma: no cover
    creq = None

# ---------- settings (all can be overridden with environment variables) ----------
FMP_KEY = os.environ.get("FMP_Key", "").strip()                         # GitHub secret FMP_Key
INTERVAL = int(os.environ.get("INTERVAL_MIN", "15"))                    # minutes between runs (must match the cron)
FMP_LIMIT = int(os.environ.get("FMP_DAILY_LIMIT", "250"))               # FMP free plan: 250 requests per day
FMP_MARGIN = int(os.environ.get("FMP_MARGIN", "20"))                    # requests never spent
SESSION_START = os.environ.get("SESSION_START", "04:00")                # ET, pre-market opens
SESSION_END = os.environ.get("SESSION_END", "20:00")                    # ET, after-hours closes
MIN_PRICE = float(os.environ.get("MIN_PRICE", "5"))                     # no penny stocks
MIN_MCAP = float(os.environ.get("MIN_MARKET_CAP", "300000000"))         # applied when the market cap is known
MIN_DVOL = float(os.environ.get("MIN_DOLLAR_VOLUME", "5000000"))        # price x volume, applied when volume is known
EXCH_TOKENS = [t.strip().upper() for t in os.environ.get("EXCHANGES", "NASDAQ,NYSE,AMEX,BATS,CBOE").split(",") if t.strip()]
BUZZ_FILTER = os.environ.get("BUZZ_FILTER", "all-stocks")               # ApeWisdom community filter
BUZZ_PAGES = int(os.environ.get("BUZZ_PAGES", "1"))                     # 100 tickers per page
BUZZ_LOOKUP = int(os.environ.get("BUZZ_LOOKUP", "40"))                  # top Reddit tickers we price-check
MAX_QUOTES = int(os.environ.get("MAX_QUOTES_PER_RUN", "40"))            # symbols per batch quote call
SINGLE_QUOTES = int(os.environ.get("SINGLE_QUOTES_PER_RUN", "6"))       # one-by-one fallback calls per run
META_TTL_H = int(os.environ.get("META_TTL_HOURS", "24"))
PROFILES_PER_RUN = int(os.environ.get("PROFILES_PER_RUN", "4"))         # company profile lookups (website) per run, if the budget allows
PROFILE_TTL_DAYS = int(os.environ.get("PROFILE_TTL_DAYS", "30"))        # how long a website / market cap is remembered
UNIVERSE_LIMIT = int(os.environ.get("UNIVERSE_LIMIT", "1000"))          # rows asked from the FMP screener (volume + market cap for all stocks)
MIN_SHARE_VOL = int(os.environ.get("MIN_SHARE_VOLUME", "500000"))       # screener: at least this many shares traded today
PROFILE_RETRY_DAYS = int(os.environ.get("PROFILE_RETRY_DAYS", "14"))    # retry a company that had no website after this
DATA = Path(os.environ.get("DATA_DIR", "data"))
STATE = Path(os.environ.get("STATE_DIR", "state"))

ET = ZoneInfo("America/New_York")
UA = {"User-Agent": "stock-board/1.0"}
FMP_BASE = "https://financialmodelingprep.com/stable"
APE_URL = "https://apewisdom.io/api/v1.0/filter/{flt}/page/{page}"
SYM_RE = re.compile(r"^[A-Z]{1,6}(-[A-Z]{1,2})?$")
USAGE = {"date": "", "fmp": 0}
RESERVE_AFTER = 0          # FMP requests kept back for the rest of today's runs (3 each: gainers, actives, quote)
QUOTE_DONE = False         # this run's own quote call has been made (or is not needed)


# ---------- small helpers ----------
def read(path, default):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return default


def write(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), "utf-8")


def num(v):
    if v is None or v == "":
        return None
    try:
        x = float(str(v).replace(",", "").replace("%", "").replace("$", ""))
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def norm_sym(s):
    s = str(s or "").upper().strip().replace(".", "-")
    return s if SYM_RE.match(s) else None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------- market calendar (NYSE) ----------
def nth_weekday(y, m, wd, n):
    d = date(y, m, 1)
    d += timedelta(days=(wd - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def last_weekday(y, m, wd):
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def easter(y):
    a = y % 19
    b, c = divmod(y, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(y, month, (h + l - 7 * m + 114) % 31 + 1)


def observed(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def holidays(y):
    out = {
        nth_weekday(y, 1, 0, 3),                 # Martin Luther King Jr. Day
        nth_weekday(y, 2, 0, 3),                 # Presidents Day
        easter(y) - timedelta(days=2),           # Good Friday
        last_weekday(y, 5, 0),                   # Memorial Day
        observed(date(y, 6, 19)),                # Juneteenth
        observed(date(y, 7, 4)),                 # Independence Day
        nth_weekday(y, 9, 0, 1),                 # Labor Day
        nth_weekday(y, 11, 3, 4),                # Thanksgiving
        observed(date(y, 12, 25)),               # Christmas
    }
    if date(y, 1, 1).weekday() != 5:             # New Year's on a Saturday: the market stays open that Friday
        out.add(observed(date(y, 1, 1)))
    return out


def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def session_of(et):
    """'pre', 'reg', 'post' or None for a datetime in Eastern time."""
    if et.weekday() >= 5 or et.date() in holidays(et.year):
        return None
    t = et.hour * 60 + et.minute
    if hm(SESSION_START) <= t < 570:
        return "pre"
    if 570 <= t < 960:
        return "reg"
    if 960 <= t < hm(SESSION_END):
        return "post"
    return None


def runs_after(et):
    left = hm(SESSION_END) - (et.hour * 60 + et.minute)
    return max(0, left // INTERVAL)


def windows_for(i):
    """Time frames (minutes) the board is built for. Default: only the latest snapshot ("right now").
    BOARD_WINDOWS=60,240,1440 adds longer frames; the page then shows time frame buttons again."""
    extra = {int(x) for x in os.environ.get("BOARD_WINDOWS", "").replace(" ", "").split(",") if x.isdigit() and int(x) >= i}
    return sorted({i} | extra)


# ---------- HTTP and the FMP request budget ----------
def load_usage(et):
    global USAGE
    u = read(STATE / "usage.json", {})
    today = et.date().isoformat()
    USAGE = {"date": today, "fmp": int(u.get("fmp", 0)) if u.get("date") == today else 0, "last": u.get("last", "")}


def save_usage():
    write(STATE / "usage.json", USAGE)


def spare():
    """Requests free for optional calls (profiles, single quotes): keeps the margin, this run's quote call and every later run."""
    return FMP_LIMIT - FMP_MARGIN - USAGE["fmp"] - RESERVE_AFTER - (0 if QUOTE_DONE else 1)


def room():
    """True if a call that was already budgeted for (this run's batch quote) can still be made."""
    return FMP_LIMIT - USAGE["fmp"] - RESERVE_AFTER >= 1


def can_call():
    """May another FMP call be made? The first call of the quote slot uses the run's own reservation; later ones need spare budget."""
    return spare() >= 1 if QUOTE_DONE else room()


def get_json(url, params=None, tries=3, fmp=False):
    status = 0
    for i in range(tries):
        if fmp:
            USAGE["fmp"] += 1
        try:
            r = requests.get(url, params=params, timeout=30, headers=UA)
            status = r.status_code
            if status == 200:
                return r.json(), 200
            if status == 429 or status >= 500:
                time.sleep(2 * (i + 1))
                continue
            return None, status
        except requests.RequestException as e:        # never print the exception: its text contains the URL and the API key
            status = 0
            print(f"  network error ({type(e).__name__})")
            time.sleep(2 * (i + 1))
        except ValueError:
            return None, status
    return None, status


def fmp(path, **params):
    params["apikey"] = FMP_KEY
    return get_json(f"{FMP_BASE}/{path}", params, tries=2, fmp=True)


def fmp_row(r):
    sym = norm_sym(r.get("symbol"))
    if not sym:
        return None
    c = r.get("changesPercentage")
    if c is None:
        c = r.get("changePercentage")
    mc = num(r.get("marketCap"))
    return {"sym": sym, "n": (str(r.get("name") or r.get("companyName") or "").strip()[:60] or None),
            "x": (str(r.get("exchangeShortName") or r.get("exchange") or "").strip() or None),
            "p": num(r.get("price")), "c": num(c), "v": num(r.get("volume")), "mc": mc if mc and mc > 0 else None}


def fmp_list(path):
    data, st = fmp(path)
    if not isinstance(data, list) or not data:
        print(f"  FMP {path}: no usable data (HTTP {st})" + (f" {str(data)[:120]}" if isinstance(data, dict) else ""))
        return None
    rows = [x for x in (fmp_row(r) for r in data if isinstance(r, dict)) if x]
    return rows or None


def fetch_buzz():
    out = []
    for page in range(1, BUZZ_PAGES + 1):
        data, st = get_json(APE_URL.format(flt=BUZZ_FILTER, page=page), tries=2)
        res = data.get("results") if isinstance(data, dict) else None
        if not isinstance(res, list):
            print(f"  ApeWisdom page {page}: no usable data (HTTP {st})")
            break
        for r in res:
            sym = norm_sym(r.get("ticker"))
            m = num(r.get("mentions"))
            if sym and m is not None:
                out.append({"sym": sym, "n": (str(r.get("name") or "").strip()[:60] or None), "m": int(m),
                            "m24": int(num(r.get("mentions_24h_ago")) or 0)})
    out.sort(key=lambda b: -b["m"])
    return out or None


# ---------- filters ----------
def passes(r, price=None):
    p = r.get("p") if price is None else price
    if p is None or p < MIN_PRICE:
        return False
    x = (r.get("x") or "").upper()
    if x and not any(t in x for t in EXCH_TOKENS):
        return False
    mc = r.get("mc")
    if mc is not None and mc < MIN_MCAP:
        return False
    v = r.get("v")
    if v is not None and p * v < MIN_DVOL:
        return False
    return True


# ---------- quotes for Reddit-only tickers (price, exchange, market cap so the filters can run) ----------
def cache_entry(row, now):
    return {k: v for k, v in {"n": row.get("n"), "x": row.get("x"), "p": row.get("p"), "mc": row.get("mc"), "t": iso(now)}.items() if v is not None}


def fetch_universe(meta, et):
    """One screener call: volume and market cap for every stock that passes the filters. Returns (rows by symbol, complete)."""
    global QUOTE_DONE
    today = et.date().isoformat()
    sm = meta.setdefault("screener", {})
    if (sm.get("ok") is False and sm.get("day") == today) or not can_call():
        return {}, False
    data, st = fmp("company-screener", marketCapMoreThan=int(MIN_MCAP), priceMoreThan=MIN_PRICE, volumeMoreThan=MIN_SHARE_VOL,
                   isActivelyTrading="true", isEtf="false", isFund="false", limit=UNIVERSE_LIMIT)
    QUOTE_DONE = True
    rows = [x for x in (fmp_row(r) for r in data if isinstance(r, dict)) if x] if isinstance(data, list) else []
    if not rows:
        sm.update(ok=False, day=today)
        print(f"  screener not available (HTTP {st}); falling back to quotes")
        return {}, False
    sm.update(ok=True, day=today)
    return {r["sym"]: r for r in rows}, 200 <= len(rows) < UNIVERSE_LIMIT     # complete = the screener was not cut off by the limit


YAHOO_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9"}
YAHOO_RETRY_MIN = int(os.environ.get("YAHOO_RETRY_MINUTES", "60"))       # after a failure, wait this long before asking Yahoo again
YAHOO_CHARTS = int(os.environ.get("YAHOO_CHARTS_PER_RUN", "60"))         # one-by-one Yahoo lookups per run for stocks the batch missed


def yahoo_session():
    sess = creq.Session(impersonate="chrome") if creq else requests.Session()
    if not creq:
        sess.headers.update(YAHOO_UA)
    sess.get("https://fc.yahoo.com", timeout=15)                           # sets the cookie (the page itself is a 404)
    return sess


def fetch_yahoo(symbols, meta, now):
    """Price, change, volume and market cap for many symbols in ONE keyless request (Yahoo Finance's unofficial quote feed).
    Costs no FMP requests. Returns {sym: row}; {} if Yahoo refuses (then the FMP fallbacks run)."""
    if not symbols:
        return {}
    ym = meta.setdefault("yahoo", {})
    if ym.get("fail_t") and (now - parse_iso(ym["fail_t"])).total_seconds() < YAHOO_RETRY_MIN * 60:
        return {}
    out = {}
    try:
        sess = yahoo_session()
        cr = sess.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15)
        crumb = cr.text.strip()
        if cr.status_code != 200 or not crumb or len(crumb) > 40 or " " in crumb or "<" in crumb:
            raise ValueError(f"no crumb (HTTP {cr.status_code})")
        for i in range(0, len(symbols), 100):
            r = sess.get("https://query1.finance.yahoo.com/v7/finance/quote", params={"symbols": ",".join(symbols[i:i + 100]), "crumb": crumb}, timeout=20)
            if r.status_code != 200:
                raise ValueError(f"quote HTTP {r.status_code}")
            for q in (r.json().get("quoteResponse") or {}).get("result") or []:
                sym = norm_sym(q.get("symbol"))
                if not sym:
                    continue
                mc = num(q.get("marketCap"))
                price, chg, ext = num(q.get("regularMarketPrice")), num(q.get("regularMarketChangePercent")), None
                rp = price                                                      # regular-session price (shown next to the pre-market / after-hours price)
                rc, ec = chg, None                                              # rc = regular-session change, ec = pre-market / after-hours change (as Yahoo shows them)
                state = str(q.get("marketState") or "").upper()
                pre, post = num(q.get("preMarketPrice")), num(q.get("postMarketPrice"))
                if state == "PRE" and pre:                                    # pre-market: change is against the previous close
                    price, chg, ext = pre, num(q.get("preMarketChangePercent")), "pre"
                    ec = chg
                elif state in ("POST", "POSTPOST") and post:                  # after-hours: change against the close BEFORE today, so it stays "since the previous close"
                    prev = num(q.get("regularMarketPreviousClose"))
                    price, ext = post, "post"
                    ec = num(q.get("postMarketChangePercent"))
                    chg = (post / prev - 1) * 100 if prev else (chg or 0) + (ec or 0)
                out[sym] = {"sym": sym, "n": (str(q.get("longName") or q.get("shortName") or "").strip()[:60] or None),
                            "x": (str(q.get("fullExchangeName") or q.get("exchange") or "").strip() or None),
                            "p": price, "c": chg, "v": num(q.get("regularMarketVolume")), "mc": mc if mc and mc > 0 else None, "ext": ext, "rc": rc, "ec": ec, "rp": rp if ext else None}
    except Exception as e:                                                    # network, TLS, JSON or shape problems: all mean "not available"
        ym["fail_t"] = iso(now)
        print(f"  Yahoo batch quote not available ({type(e).__name__}{': ' + str(e)[:60] if isinstance(e, ValueError) else ''})")
        return {}
    if not out:
        ym["fail_t"] = iso(now)
        print("  Yahoo batch quote returned nothing")
        return {}
    ym.pop("fail_t", None)
    return out


def fetch_yahoo_charts(symbols, meta, now):
    """Price, change and volume one symbol at a time from Yahoo's chart feed (no crumb needed). Used for stocks the batch call missed,
    mostly Reddit-only tickers. Returns {sym: row}; stops at the first refusal."""
    if not symbols:
        return {}
    cm = meta.setdefault("yahoo_chart", {})
    if cm.get("fail_t") and (now - parse_iso(cm["fail_t"])).total_seconds() < YAHOO_RETRY_MIN * 60:
        return {}
    out = {}
    try:
        sess = yahoo_session()
        for sym in symbols[:YAHOO_CHARTS]:
            r = sess.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": "1d", "interval": "1d"}, timeout=20)
            if r.status_code == 404:
                continue                                                  # Yahoo does not know this symbol
            if r.status_code != 200:
                raise ValueError(f"chart HTTP {r.status_code}")
            res = ((r.json().get("chart") or {}).get("result") or [None])[0]
            m = (res or {}).get("meta") or {}
            price, prev = num(m.get("regularMarketPrice")), num(m.get("previousClose") if m.get("previousClose") is not None else m.get("chartPreviousClose"))
            if price is None:
                continue
            out[sym] = {"sym": sym, "n": (str(m.get("longName") or m.get("shortName") or "").strip()[:60] or None),
                        "x": (str(m.get("fullExchangeName") or m.get("exchangeName") or "").strip() or None), "p": price,
                        "c": (price / prev - 1) * 100 if prev else None, "v": num(m.get("regularMarketVolume")), "mc": None}
            time.sleep(0.35)
    except Exception as e:
        cm["fail_t"] = iso(now)
        print(f"  Yahoo chart lookups stopped after {len(out)} ({type(e).__name__}{': ' + str(e)[:60] if isinstance(e, ValueError) else ''})")
    else:
        cm.pop("fail_t", None)
    return out


def fetch_quotes(symbols, meta, now, et):
    """Fresh quotes (price, change, volume, market cap): a batch call if the plan allows it, a few single calls otherwise."""
    global QUOTE_DONE
    out = {}
    if not symbols:
        return out
    q = meta.setdefault("q", {})
    bm = meta.setdefault("batch", {})
    today = et.date().isoformat()
    if not (bm.get("ok") is False and bm.get("day") == today) and can_call():
        data, st = fmp("batch-quote", symbols=",".join(symbols[:MAX_QUOTES]))
        rows = [x for x in (fmp_row(r) for r in data if isinstance(r, dict)) if x] if isinstance(data, list) else []
        QUOTE_DONE = True
        if rows:
            bm.update(ok=True, day=today)
            for r in rows:
                out[r["sym"]] = r
                q[r["sym"]] = cache_entry(r, now)
            return out
        bm.update(ok=False, day=today)
        print(f"  batch quote not available (HTTP {st}); using single quotes")
    n = 0
    for s in symbols:
        if n >= SINGLE_QUOTES or not can_call():
            break
        n += 1
        QUOTE_DONE = True
        data, st = fmp("quote", symbol=s)
        row = fmp_row(data[0]) if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if row:
            out[s] = row
            q[s] = cache_entry(row, now)
        elif isinstance(data, list):                          # FMP does not know this symbol: do not ask again for a day
            meta.setdefault("miss", {})[s] = iso(now)
    return out


# ---------- company websites (FMP company profile), cached for a long time ----------
def norm_site(w):
    w = str(w or "").strip()
    if not w:
        return None
    if not re.match(r"^https?://", w, re.I):
        w = "https://" + w
    m = re.match(r"^(https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,})(/[^\s]*)?$", w[:200], re.I)
    return ("https://" + m.group(1).split("://", 1)[1].lower() + (m.group(2) or "")) if m else None


def needs_profile(e, now, want_mc=False):
    if not e:
        return True
    age = (now - parse_iso(e["t"])).days
    if want_mc and not e.get("mc") and age >= 1:
        return True
    return age >= (PROFILE_TTL_DAYS if e.get("w") else PROFILE_RETRY_DAYS)


def importance_key(rows):
    """Sort key: stocks on the most rankings first, then the best rank."""
    def key(sym):
        r = rows[sym]
        return (-sum(k in r for k in ("gr", "ar", "m")), min(r.get("gr", 999), r.get("ar", 999), r.get("br", 999)))
    return key


def fetch_profiles(rows, meta, now, et):
    """Look up website and market cap of the most important tickers that are not cached yet, as far as the request budget allows."""
    today = et.date().isoformat()
    if meta.get("profile_dead") == today:
        return
    prof = read(STATE / "profiles.json", {})
    prof = {s: e for s, e in prof.items() if (now - parse_iso(e["t"])).days < 365}
    todo = [s for s in sorted(rows, key=importance_key(rows)) if needs_profile(prof.get(s), now, rows[s].get("mc") is None)][:PROFILES_PER_RUN]
    for sym in todo:
        if spare() < 1:
            break
        data, st = fmp("profile", symbol=sym)
        if st in (401, 402, 403):
            meta["profile_dead"] = today
            print(f"  company profile not available on this plan (HTTP {st}); no websites today")
            break
        if isinstance(data, list):
            row = data[0] if data and isinstance(data[0], dict) else {}
            mc = num(row.get("marketCap"))
            prof[sym] = {k: v for k, v in {"w": norm_site(row.get("website")), "mc": int(mc) if mc and mc > 0 else None, "t": iso(now)}.items() if v is not None or k == "w"}
    write(STATE / "profiles.json", prof)


# ---------- one snapshot ----------
def build_snapshot(now, ses, g_rows, a_rows, buzz, meta, et):
    rows = {}

    def merge(r, **extra):
        d = rows.setdefault(r["sym"], {})
        for k in ("n", "x", "p", "c", "v", "mc", "rc", "ec", "rp"):
            if r.get(k) is not None:
                d[k] = r[k]
        if r.get("ext"):
            d["ext"] = r["ext"]
        d.update(extra)

    for i, r in enumerate(g_rows or [], 1):
        merge(r, gr=i)
    for i, r in enumerate(a_rows or [], 1):
        merge(r, ar=i)

    # ---- find the numbers (price, change, volume, market cap) for EVERY stock on the board, Reddit-only ones included ----
    uni, complete = fetch_universe(meta, et)                      # 1) FMP screener, if the plan allows it
    all_syms = list(rows) + [b["sym"] for b in (buzz or []) if b["sym"] not in rows]
    stats = {s: dict(uni[s]) for s in all_syms if s in uni}

    def fill(sym, src, override=()):
        d = stats.setdefault(sym, {"sym": sym})
        for k in ("n", "x", "p", "c", "v", "mc", "ext", "rc", "ec", "rp"):
            if src.get(k) is not None and (d.get(k) is None or k in override):
                d[k] = src[k]

    if not complete:                                              # 2) Yahoo batch quote: every ticker in one request
        for sym, y in fetch_yahoo(all_syms, meta, now).items():
            fill(sym, y, override=("p", "c", "v", "mc", "ext", "rc", "ec", "rp"))

    def has(sym, k):
        return (rows.get(sym) or {}).get(k) is not None or (stats.get(sym) or {}).get(k) is not None

    lack = [s for s in all_syms if not (has(s, "p") and has(s, "c") and has(s, "v")) and not (complete and uni and s not in uni)]
    for sym, y in fetch_yahoo_charts(lack, meta, now).items():    # 3) one by one from Yahoo's chart feed for whatever is still missing
        fill(sym, y)

    for sym, d in rows.items():
        u = stats.get(sym)
        if u:
            if u.get("ext") and u.get("p") is not None:               # pre-market / after-hours price replaces the (regular-hours) list price
                d["p"], d["ext"] = u["p"], u["ext"]
                for k in ("c", "rc", "ec", "rp"):
                    if u.get(k) is not None:
                        d[k] = u[k]
            for k in ("v", "mc"):
                if u.get(k) is not None:
                    d[k] = u[k]
            for k in ("n", "x", "p", "c"):
                if d.get(k) is None and u.get(k) is not None:
                    d[k] = u[k]
    if complete and any(sym in uni for sym in rows):              # a complete screener answer defines which stocks pass the size limits
        for sym in [sym for sym in rows if sym not in uni]:
            del rows[sym]

    # last known values come from the previous snapshot and the profile cache
    files = snap_files()
    prev = read(files[-1], None) if files else None
    prow = prev["rows"] if prev else {}
    recent = bool(prev) and (now - parse_iso(prev["t"])).total_seconds() <= 3 * INTERVAL * 60
    prof = read(STATE / "profiles.json", {})

    # Reddit-only tickers: they join the board once their numbers are known
    miss = meta.setdefault("miss", {})
    pending = []
    for b in buzz or []:
        sym = b["sym"]
        if sym in rows:
            continue
        if (stats.get(sym) or {}).get("p") is not None:
            merge(stats[sym])
        elif recent and (prow.get(sym) or {}).get("p") is not None:     # no fresh numbers this time: keep the last known ones
            merge({"sym": sym, **{k: prow[sym].get(k) for k in ("n", "x", "p", "c", "v", "mc", "ext", "rc", "ec", "rp")}})
        elif not (uni and complete) and not (miss.get(sym) and (now - parse_iso(miss[sym])).total_seconds() < 86400):
            pending.append(sym)
    pending = pending[:BUZZ_LOOKUP]

    # 4) last known values: a missing number is carried over from the previous snapshot (price, change and volume only if it is
    #    recent) or from the profile cache (market cap changes slowly, so an older value will do)
    for sym, d in rows.items():
        old = prow.get(sym) or {}
        if recent:
            if d.get("p") is None and old.get("p") is not None and old.get("ext"):
                d["ext"] = old["ext"]
                for k in ("rc", "ec", "rp"):
                    if d.get(k) is None and old.get(k) is not None:
                        d[k] = old[k]
            for k in ("p", "c", "v"):
                if d.get(k) is None and old.get(k) is not None:
                    d[k] = old[k]
        if d.get("mc") is None:
            mc = old.get("mc") or (prof.get(sym) or {}).get("mc")
            if mc:
                d["mc"] = mc

    # 5) FMP quotes (a few single calls, within the request budget) for what is still missing
    need = [sym for sym in sorted(rows, key=importance_key(rows)) if rows[sym].get("v") is None or rows[sym].get("mc") is None]
    fresh = fetch_quotes(need + [sym for sym in pending if sym not in need], meta, now, et) if (need or pending) else {}
    for sym, qr in fresh.items():
        if sym in rows:
            for k in ("p", "c", "v", "mc"):
                if rows[sym].get(k) is None and qr.get(k) is not None:
                    rows[sym][k] = qr[k]
    cache = meta.get("q", {})
    cached_price = {}
    for sym in pending:
        if sym in rows:
            continue
        if sym in fresh:
            merge(fresh[sym])
        elif sym in cache and (now - parse_iso(cache[sym]["t"])).total_seconds() < META_TTL_H * 3600:
            merge({"sym": sym, "n": cache[sym].get("n"), "x": cache[sym].get("x"), "mc": cache[sym].get("mc")})
            cached_price[sym] = cache[sym].get("p")
    for i, b in enumerate(buzz or [], 1):
        d = rows.get(b["sym"])
        if d is not None:
            d["m"], d["m24"], d["br"] = b["m"], b["m24"], i
            if not d.get("n") and b.get("n"):
                d["n"] = b["n"][:60]

    kept = {s: d for s, d in rows.items() if passes(d, d.get("p") if d.get("p") is not None else cached_price.get(s))}
    for key in ("gr", "ar", "br"):                           # ranks among the stocks that passed the filters
        order = sorted((s for s, d in kept.items() if key in d), key=lambda s: kept[s][key])
        for i, s in enumerate(order, 1):
            kept[s][key] = i
    for d in kept.values():
        if d.get("rc") is None and d.get("c") is not None and not d.get("ext"):
            d["rc"] = d["c"]                                         # in the regular session the regular change IS the change
        for k in ("p", "c", "rc", "ec", "rp"):
            if d.get(k) is not None:
                d[k] = round(d[k], 4)
        for k in ("v", "mc"):
            if k in d:
                d[k] = int(d[k])
    fp = hashlib.md5(json.dumps(sorted((s, d.get("p"), d.get("c"), d.get("v")) for s, d in kept.items() if "gr" in d or "ar" in d)).encode()).hexdigest()[:12]
    bp = hashlib.md5(json.dumps(sorted((s, d.get("m")) for s, d in kept.items() if "m" in d)).encode()).hexdigest()[:12]
    return {"t": iso(now), "ses": ses or "closed", "ok": {"g": int(bool(g_rows)), "a": int(bool(a_rows)), "b": int(bool(buzz))},
            "fp": fp, "bp": bp, "rows": kept}


def snap_files():
    return sorted((STATE / "snaps").glob("*.json"))


def prune(latest_t):
    keep_from = latest_t - timedelta(minutes=max(max(windows_for(INTERVAL)), 1440) + 120)     # a day of history for "trending since"
    for f in snap_files():
        s = read(f, None)
        if not s or parse_iso(s["t"]) < keep_from:
            f.unlink()


# ---------- scores (0 to 100) ----------
def gain_score(c):
    if c is None:
        return None
    return 0.0 if c <= 0 else min(100.0, 100 * math.log1p(c) / math.log1p(50))      # +50% or more = 100


def vol_score(dollars):
    return max(0.0, min(1.0, (math.log10(max(dollars, 1)) - 6) / 4)) * 100          # $1M = 0, $10B = 100


def rank_score(rank, n):
    return max(0.0, 100 * (1 - (rank - 1) / n))


def snapshot_scores(s):
    rows, ok = s["rows"], s["ok"]
    with_rank = [r for r in rows.values() if "ar" in r]
    use_vol = bool(with_rank) and sum(1 for r in with_rank if r.get("v") and r.get("p")) >= 0.8 * len(with_rank)
    n_ar = max(len(with_rank), 25)
    max_m = max([r.get("m", 0) for r in rows.values()] or [0])
    out = {}
    for sym, r in rows.items():
        if not ok["g"]:
            g = None
        else:
            g = gain_score(r.get("c")) if "gr" in r else 0.0                 # only stocks on the gainers list score here
        if not ok["a"]:
            a = None
        elif "ar" not in r:
            a = 0.0
        elif use_vol and r.get("v") and r.get("p"):
            a = vol_score(r["p"] * r["v"])
        else:
            a = rank_score(r["ar"], n_ar)
        if not ok["b"]:
            b = None
        elif "m" not in r or max_m <= 0:
            b = 0.0
        else:
            b = 100 * math.log1p(r["m"]) / math.log1p(max_m)
        out[sym] = (g, a, b)
    return out


def mean(vals):
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def build_board():
    snaps = [x for x in (read(f, None) for f in snap_files()) if x]
    snaps.sort(key=lambda s: s["t"])
    wins = windows_for(INTERVAL)
    board = {"updated": iso(datetime.now(timezone.utc)), "interval": INTERVAL, "windows": wins, "asof": None, "session": None,
             "snaps": {}, "filters": {"min_price": MIN_PRICE, "min_market_cap": MIN_MCAP, "min_dollar_volume": MIN_DVOL, "exchanges": EXCH_TOKENS},
             "sources": {}, "stocks": {}}
    if not snaps:
        write(DATA / "board.json", board)
        return board
    latest = snaps[-1]
    latest_t = parse_iso(latest["t"])
    board["asof"], board["session"] = latest["t"], latest["ses"]
    board["sources"] = {"g": bool(latest["ok"]["g"]), "a": bool(latest["ok"]["a"]), "b": bool(latest["ok"]["b"])}
    profiles = read(STATE / "profiles.json", {})
    scored = [(parse_iso(s["t"]), s, snapshot_scores(s)) for s in snaps]

    last_seen = {}                                            # newest snapshot each ticker appeared in
    appear = {}                                               # every snapshot time each ticker appeared in
    for t, s, _ in scored:
        for sym, r in s["rows"].items():
            last_seen[sym] = (t, r)
            appear.setdefault(sym, []).append(t)
    why_cache = read(STATE / "why.json", {})
    gap = timedelta(minutes=INTERVAL * 2.5)

    def trending_since(times):
        """When the current run of appearances began (a gap of more than 2.5 intervals starts a new run)."""
        start = prev = times[-1]
        for t in reversed(times[:-1]):
            if prev - t > gap:
                break
            start = prev = t
        return start

    table = {}
    for w in wins:
        cutoff = latest_t - timedelta(minutes=w) + timedelta(minutes=INTERVAL / 2)
        part = [x for x in scored if x[0] >= cutoff]
        board["snaps"][str(w)] = len(part)
        syms = {sym for _, s, _ in part for sym in s["rows"]}
        for sym in syms:
            gs, as_, bs, seen = [], [], [], 0
            for _, s, sc in part:
                if sym in s["rows"]:
                    g, a, b = sc[sym]
                    seen += 1
                else:                                         # not on any list in this snapshot = not a mover
                    ok = s["ok"]
                    g, a, b = (0.0 if ok["g"] else None), (0.0 if ok["a"] else None), (0.0 if ok["b"] else None)
                gs.append(g); as_.append(a); bs.append(b)
            g, a, b = mean(gs), mean(as_), mean(bs)
            have = [x for x in (g, a, b) if x is not None]
            on = sum(1 for x in (g, a, b) if x)                      # rankings (tabs) the stock is on in this time frame
            allv = sum(have) / len(have) if len(have) >= 2 and on >= 2 else None     # All needs 2 of the 3 rankings
            table.setdefault(sym, {})[str(w)] = [None if x is None else round(x, 1) for x in (g, a, b, allv)] + [seen]

    for sym, per in table.items():
        if not any((v or 0) > 0 for arr in per.values() for v in arr[:4]):
            continue
        t, r = last_seen[sym]
        pf = profiles.get(sym) or {}
        wy = why_cache.get(sym) or {}
        st = {"n": r.get("n"), "x": r.get("x"), "p": r.get("p"), "c": r.get("c"), "rc": r.get("rc"), "ec": r.get("ec"), "rp": r.get("rp"), "ext": r.get("ext"), "v": r.get("v"), "mc": r.get("mc") or pf.get("mc"),
              "m": r.get("m"), "m24": r.get("m24"), "since": iso(trending_since(appear[sym])), "web": pf.get("w"),
              "why": wy.get("d"), "why_t": wy.get("t") if wy.get("d") else None, "s": per}
        board["stocks"][sym] = {k: v for k, v in st.items() if v is not None}
    write(DATA / "board.json", board)
    return board


# ---------- commands ----------
def run():
    global RESERVE_AFTER, QUOTE_DONE
    now = datetime.now(timezone.utc)
    et = now.astimezone(ET)
    ses = session_of(et)
    force = os.environ.get("FORCE") == "1"
    if not ses and not force:
        print(f"Market session closed ({et:%a %Y-%m-%d %H:%M} ET): nothing to do.")
        return 0
    if not FMP_KEY:
        print("FMP_Key is not set. Add it as a GitHub secret named FMP_Key.")
        return 1
    load_usage(et)
    if USAGE["fmp"] + 2 > FMP_LIMIT:
        print(f"FMP budget used up for today ({USAGE['fmp']}/{FMP_LIMIT}).")
        return 0
    if not force and USAGE["last"] and (now - parse_iso(USAGE["last"])).total_seconds() < INTERVAL * 0.4 * 60:
        print("The last run was less than 40% of an interval ago (a duplicate trigger?): skipping to protect the FMP limit.")
        return 0
    USAGE["last"] = iso(now)
    RESERVE_AFTER = 3 * runs_after(et)
    QUOTE_DONE = False
    meta = read(STATE / "meta.json", {})
    try:
        g_rows = fmp_list("biggest-gainers")
        a_rows = fmp_list("most-actives")
        if not g_rows and not a_rows:
            print("Both FMP lists failed; no snapshot saved.")
            return 1
        buzz = fetch_buzz()
        snap = build_snapshot(now, ses, g_rows, a_rows, buzz, meta, et)
        QUOTE_DONE = True
        fetch_profiles(snap["rows"], meta, now, et)
        try:
            why.update(snap["rows"], now, STATE, importance_key(snap["rows"]))
        except Exception as e:                      # a failed "why trending" step must never lose the snapshot
            print(f"  why-trending step failed ({type(e).__name__}); continuing")
        files = snap_files()
        prev = read(files[-1], None) if files else None
        if prev and prev.get("fp") == snap["fp"] and prev.get("bp") == snap["bp"]:
            print("Prices and buzz are unchanged since the last snapshot (market data not updating); nothing saved.")
        else:
            write(STATE / "snaps" / (now.strftime("%Y%m%dT%H%M%SZ") + ".json"), snap)
            n = len(snap["rows"])
            print(f"Snapshot {snap['t']} [{snap['ses']}]: {n} stocks "
                  f"(gainers {sum('gr' in r for r in snap['rows'].values())}, active {sum('ar' in r for r in snap['rows'].values())}, "
                  f"buzz {sum('m' in r for r in snap['rows'].values())})")
        prune(now)
        build_board()
        cutoff = now - timedelta(days=7)
        q = meta.get("q", {})
        for s in [s for s, e in q.items() if parse_iso(e["t"]) < cutoff]:
            del q[s]
        miss = meta.get("miss", {})
        for s in [s for s, t in miss.items() if parse_iso(t) < now - timedelta(days=2)]:
            del miss[s]
        write(STATE / "meta.json", meta)
    finally:
        save_usage()
        print(f"FMP requests today: {USAGE['fmp']}/{FMP_LIMIT}")
    return 0


def rebuild():
    b = build_board()
    print(f"Board rebuilt: {len(b['stocks'])} stocks, windows {b['windows']}, snapshots per window {b['snaps']}")
    return 0


def check():
    if not FMP_KEY:
        print("Set the FMP_Key environment variable first.")
        return 1
    print("Checking the APIs (6 FMP requests)...")
    for name, path, params in [("biggest-gainers", "biggest-gainers", {}), ("most-actives", "most-actives", {}),
                               ("batch-quote", "batch-quote", {"symbols": "AAPL,MSFT"}), ("quote", "quote", {"symbol": "AAPL"}), ("profile", "profile", {"symbol": "AAPL"}),
                               ("company-screener", "company-screener", {"marketCapMoreThan": int(MIN_MCAP), "priceMoreThan": MIN_PRICE, "volumeMoreThan": MIN_SHARE_VOL, "isActivelyTrading": "true", "isEtf": "false", "isFund": "false", "limit": 5})]:
        data, st = fmp(path, **params)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            print(f"  {name}: HTTP {st}, {len(data)} rows, fields: {', '.join(sorted(data[0]))}")
        else:
            print(f"  {name}: HTTP {st}, {str(data)[:150] if data is not None else 'no data'}")
    y = fetch_yahoo(["AAPL", "MSFT"], {}, datetime.now(timezone.utc))
    print("  Yahoo batch quote: " + (f"{len(y)} rows, AAPL price {y.get('AAPL', {}).get('p')} ({ {'pre': 'pre-market', 'post': 'after-hours'}.get(y.get('AAPL', {}).get('ext'), 'regular session') }), volume {y.get('AAPL', {}).get('v')}, market cap {y.get('AAPL', {}).get('mc')}" if y else "not available from here"))
    yc = fetch_yahoo_charts(["AAPL"], {}, datetime.now(timezone.utc))
    print(f"  Yahoo chart feed: " + (f"AAPL price {yc['AAPL']['p']}, change {yc['AAPL']['c']:.2f}%, volume {yc['AAPL']['v']}" if yc else "not available from here")
          + f"  (Chrome impersonation via curl_cffi: {'on' if creq else 'OFF - pip install curl_cffi'})")
    items = why.headlines("AAPL", "Apple", datetime.now(timezone.utc))
    print(f"  headlines (Yahoo/Google News RSS): {len(items)} found" + (f", newest: {items[0][0][:70]}" if items else ""))
    print("  GEMINI_KEY: " + ("set" if os.environ.get("GEMINI_KEY", "").strip() else "NOT set (no 'why trending' lines)"))
    data, st = get_json(APE_URL.format(flt=BUZZ_FILTER, page=1), tries=1)
    res = data.get("results") if isinstance(data, dict) else None
    if res:
        print(f"  ApeWisdom: HTTP {st}, {len(res)} rows, fields: {', '.join(sorted(res[0]))}")
    else:
        print(f"  ApeWisdom: HTTP {st}, no usable data")
    return 0


def demo():
    global DATA, STATE
    DATA, STATE = Path("demo_data"), Path("demo_state")
    for p in (DATA, STATE):
        shutil.rmtree(p, ignore_errors=True)
    rnd = random.Random(7)
    names = {"NVDA": "NVIDIA", "TSLA": "Tesla", "AAPL": "Apple", "AMD": "Advanced Micro Devices", "PLTR": "Palantir Technologies",
             "SOFI": "SoFi Technologies", "RIVN": "Rivian Automotive", "AMZN": "Amazon.com", "META": "Meta Platforms", "MSFT": "Microsoft",
             "GME": "GameStop", "AMC": "AMC Entertainment", "HOOD": "Robinhood Markets", "COIN": "Coinbase Global", "MSTR": "Strategy",
             "INTC": "Intel", "SMCI": "Super Micro Computer", "UBER": "Uber Technologies", "SNAP": "Snap", "NIO": "NIO Inc.",
             "F": "Ford Motor", "BAC": "Bank of America", "T": "AT&T", "ONDS": "Ondas Holdings", "RKLB": "Rocket Lab",
             "IONQ": "IonQ", "SOUN": "SoundHound AI", "LCID": "Lucid Group", "CCL": "Carnival", "PFE": "Pfizer",
             "NFLX": "Netflix", "DIS": "Walt Disney", "BABA": "Alibaba Group", "TQQQ": "ProShares UltraPro QQQ", "SPY": "SPDR S&P 500 ETF"}
    st = {s: {"p": rnd.uniform(6, 600), "w": rnd.uniform(0.6, 2.2), "drift": rnd.uniform(-0.2, 0.25), "heat": rnd.random() ** 2 * 900,
              "x": rnd.choice(["NASDAQ", "NYSE"]), "cap": 10 ** rnd.uniform(8.6, 12.4)} for s in names}
    times = []
    t = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % INTERVAL)
    while len(times) < 130:
        if session_of(t.astimezone(ET)):
            times.append(t)
        t -= timedelta(minutes=INTERVAL)
    times.reverse()
    day, prev_close, vol = None, {}, {}
    for t in times:
        et = t.astimezone(ET)
        if et.date() != day:
            day = et.date()
            prev_close = {s: v["p"] for s, v in st.items()}
            vol = {s: 0.0 for s in st}
        for s, v in st.items():
            v["p"] = max(1.0, v["p"] * (1 + rnd.gauss(v["drift"] / 100, 0.25 * v["w"] / 10)))
            vol[s] += rnd.random() * 2.2e6 * v["w"] * (3 if session_of(et) == "reg" else 1)
            v["heat"] = max(5, v["heat"] * rnd.uniform(0.85, 1.2))
        ext_tag = session_of(et) if session_of(et) in ("pre", "post") else None
        info = {s: {"n": names[s], "x": st[s]["x"], "mc": int(st[s]["cap"]), **({"ext": ext_tag, "ec": 0.0} if ext_tag else {}), "p": round(st[s]["p"], 2), "c": round((st[s]["p"] / prev_close[s] - 1) * 100, 2), "v": int(vol[s])} for s in st}
        if ext_tag:                                          # demo: split the change into a regular-session part and an extended-hours part
            for s_ in info:
                info[s_]["rc"] = round(info[s_]["c"] * 0.6, 2)
                info[s_]["ec"] = round(info[s_]["c"] * 0.4, 2)
                info[s_]["rp"] = round(info[s_]["p"] / (1 + info[s_]["ec"] / 100), 2)
        gain = sorted(info, key=lambda s: -info[s]["c"])[:25]
        act = sorted(info, key=lambda s: -info[s]["p"] * info[s]["v"])[:25]
        buzz = sorted(st, key=lambda s: -st[s]["heat"])[:40]
        rows = {}
        for s in set(gain) | set(act) | set(buzz):
            rows[s] = dict(info[s])
            if s in gain: rows[s]["gr"] = gain.index(s) + 1
            if s in act: rows[s]["ar"] = act.index(s) + 1
            if s in buzz:
                rows[s].update(m=int(st[s]["heat"]), m24=int(st[s]["heat"] * rnd.uniform(0.6, 1.3)), br=buzz.index(s) + 1)
        write(STATE / "snaps" / (t.strftime("%Y%m%dT%H%M%SZ") + ".json"),
              {"t": iso(t), "ses": session_of(et), "ok": {"g": 1, "a": 1, "b": 1}, "fp": "", "bp": "", "rows": rows})
    sites = {"NVDA": "nvidia.com", "TSLA": "tesla.com", "AAPL": "apple.com", "AMD": "amd.com", "PLTR": "palantir.com", "SOFI": "sofi.com",
             "RIVN": "rivian.com", "AMZN": "amazon.com", "META": "meta.com", "MSFT": "microsoft.com", "GME": "gamestop.com", "HOOD": "robinhood.com",
             "COIN": "coinbase.com", "INTC": "intel.com", "UBER": "uber.com", "SNAP": "snap.com", "F": "ford.com", "BAC": "bankofamerica.com",
             "RKLB": "rocketlabusa.com", "IONQ": "ionq.com", "PFE": "pfizer.com", "NFLX": "netflix.com", "BABA": "alibaba.com"}
    write(STATE / "profiles.json", {s: {"w": "https://www." + d, "t": iso(datetime.now(timezone.utc))} for s, d in sites.items()})
    texts = ["Up on heavy volume after an analyst raised its price target, according to recent headlines.",
             "Reportedly moving on news of a new partnership; the Reddit chatter is up sharply from yesterday.",
             "No clear news found in the headlines; the move comes with unusually high trading volume.",
             "Shares rose after quarterly results beat expectations, with sales ahead of forecasts."]
    write(STATE / "why.json", {s: {"d": texts[i % len(texts)], "t": iso(datetime.now(timezone.utc) - timedelta(minutes=20 * (i % 9)))} for i, s in enumerate(list(names)[:24])})
    b = build_board()
    print(f"Demo data written to {DATA}/board.json ({len(b['stocks'])} stocks, {len(times)} snapshots). "
          f"Serve this folder and open index.html?data=demo_data")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    fn = {"run": run, "rebuild": rebuild, "check": check, "demo": demo}.get(cmd)
    if not fn:
        sys.exit(__doc__)
    sys.exit(fn())
