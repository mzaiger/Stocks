"""Why is a stock trending? A short line written by Gemini flash-lite from recent headlines and the stock's numbers.

Same pattern as TrendingTopics: at most ONE Gemini call per run (all stocks that need a line go in one batch), the model
chain gemini-3.5-flash-lite -> gemini-3.1-flash-lite -> gemini-2.5-flash-lite, and the secret is named GEMINI_KEY.

A stock is only sent to Gemini when it has no description yet or its description is WHY_TTL_HOURS (4) hours old.
Headlines come from Yahoo Finance's ticker RSS feed, with Google News as the fallback. Neither needs a key.
Descriptions live in state/why.json and are copied into data/board.json by stocks.py.
"""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests

GEMINI_MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite")
WHY_TTL_HOURS = float(os.environ.get("WHY_TTL_HOURS", "4"))            # ask again only after this long
WHY_MAX = int(os.environ.get("WHY_MAX_PER_RUN", "25"))                 # stocks per Gemini call
NEWS_PER_STOCK = int(os.environ.get("NEWS_PER_STOCK", "5"))
NEWS_MAX_AGE_H = 72
KEEP_DAYS = 7
UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-board/1.0)", "Accept-Language": "en-US,en;q=0.9"}


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _read(path, default):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), "utf-8")


# ---------- headlines ----------
def _rss(url):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return []
    out = []
    for it in root.iter("item"):
        title = re.sub(r"\s+", " ", (it.findtext("title") or "")).strip()
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "")
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            when = None
        src = (it.findtext("source") or "").strip()
        if title:
            out.append((title, src, when))
    return out


def headlines(sym, name, now):
    """Up to NEWS_PER_STOCK recent headlines, newest first: [(title, source, datetime|None)]."""
    items = _rss(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(sym)}&region=US&lang=en-US")
    if len(items) < 2:
        q = quote_plus(f"{name or sym} {sym} stock when:3d")
        items = items + _rss(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")
    cutoff = now - timedelta(hours=NEWS_MAX_AGE_H)
    seen, out = set(), []
    for title, src, when in sorted(items, key=lambda x: x[2] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True):
        key = re.sub(r"\W+", "", title.lower())[:60]
        if key in seen or (when and when < cutoff):
            continue
        seen.add(key)
        out.append((title, src, when))
        if len(out) >= NEWS_PER_STOCK:
            break
    return out


# ---------- the prompt ----------
def _compact(v):
    if v is None:
        return None
    for lim, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= lim:
            return f"{v / lim:.1f}{suf}"
    return str(int(v))


def _material(sym, row, news, now):
    bits = []
    if row.get("c") is not None:
        bits.append(f"{'up' if row['c'] >= 0 else 'down'} {abs(row['c']):.1f}% today" + (f" at ${row['p']:.2f}" if row.get("p") else ""))
    if row.get("v"):
        bits.append(f"volume {_compact(row['v'])} shares")
    if row.get("mc"):
        bits.append(f"market cap ${_compact(row['mc'])}")
    lists = [n for k, n in (("gr", "top gainers"), ("ar", "most active"), ("m", "most discussed on Reddit")) if k in row]
    if lists:
        bits.append("on the " + " and ".join(lists) + " list" + ("s" if len(lists) > 1 else ""))
    if row.get("m"):
        extra = f" ({'up' if row['m'] >= row.get('m24', 0) else 'down'} from {row['m24']} the day before)" if row.get("m24") else ""
        bits.append(f"{row['m']} Reddit mentions in 24 hours{extra}")
    lines = [f"Figures: " + "; ".join(bits) + "."]
    if news:
        lines.append("Recent headlines (newest first):")
        for title, src, when in news:
            age = ""
            if when:
                h = max(0, int((now - when).total_seconds() // 3600))
                age = f"{h}h ago" if h < 48 else f"{h // 24}d ago"
            lines.append(f"- {title}" + (f" ({src}, {age})" if src and age else f" ({age})" if age else f" ({src})" if src else ""))
    else:
        lines.append("Recent headlines: none found.")
    return "\n".join(lines)


def _prompt(entries):
    blocks = [f"=== STOCK id={e['id']}: {e['sym']} ({e['name']}) ===\n{e['material']}" for e in entries]
    return (
        "You write one short line for a stock page that shows which stocks are trending.\n\n"
        f"There are {len(entries)} stocks below. For each, write ONE or TWO short sentences (max 40 words) saying why "
        "it is trending right now.\n\n"
        "Rules:\n"
        "- Use only the material shown under that stock. Do not invent news, numbers, dates or background.\n"
        "- If the headlines explain the move (earnings, an analyst call, a deal, a product, a court ruling...), say "
        "what happened in your own words. Do not copy headlines.\n"
        "- If the headlines do not explain it, say plainly that no clear news was found and describe the move from "
        "the figures, for example: \"Up 6% on heavy volume with no clear news in the headlines.\"\n"
        "- If something is unconfirmed, say \"reportedly\".\n"
        "- No predictions and no advice: never say buy, sell, or that a price will rise or fall.\n"
        "- Do not mix up stocks: each answer must come from its own stock's material.\n"
        "- Plain language. No hashtags, emojis or all-caps shouting. Do not start with the ticker.\n\n"
        "Return JSON only, one entry per stock id:\n"
        "{\"stocks\": [{\"id\": 1, \"description\": \"...\"}]}\n\n" + "\n\n".join(blocks)
    )


# ---------- Gemini ----------
class ModelUnavailable(Exception):
    pass


def _parse(raw):
    try:
        data = json.loads(raw)
        rows = data["stocks"] if isinstance(data, dict) else data
        out = {}
        for row in rows:
            d = str(row.get("description") or "").strip()
            if d:
                out[int(row["id"])] = d
        return out or None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def _call_model(model, prompt, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
        except requests.RequestException as e:
            print(f"  {model}: request error ({type(e).__name__})")
            time.sleep(2 ** attempt * 3)
            continue
        if 400 <= r.status_code < 500:
            raise ModelUnavailable(f"{r.status_code} {r.reason}")
        if r.status_code >= 500:
            time.sleep(2 ** attempt * 3)
            continue
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            raw = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, ValueError, TypeError) as e:
            print(f"  {model}: unreadable response ({type(e).__name__})")
            return None
        out = _parse(raw)
        if out is None:
            print(f"  {model}: reply was not the expected JSON")
        return out
    return None


def _gemini(prompt):
    key = os.environ.get("GEMINI_KEY", "").strip()
    if not key:
        print("  GEMINI_KEY not set - no 'why trending' lines.")
        return {}, None
    for model in GEMINI_MODELS:
        try:
            result = _call_model(model, prompt, key)
        except ModelUnavailable as e:
            print(f"  {model} unavailable, trying next: {e}")
            continue
        if result:
            return result, model
        print(f"  {model} gave no usable answer, trying next.")
    return {}, None


def _shorten(text, n=300):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= n:
        return text
    cut = text[:n]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > 80 else cut.rstrip(" ,;:") + "…"


# ---------- the entry point ----------
def stale(entry, now):
    return not entry or not entry.get("d") or (now - _parse_iso(entry["t"])).total_seconds() >= WHY_TTL_HOURS * 3600


def update(rows, now, state_dir, order=None):
    """Write a 'why trending' line for the stocks in `rows` that have none or an old one. Returns {sym: {"d": text, "t": iso}}.
    rows = {sym: snapshot row}; order = function(sym) used to sort the most important stocks first."""
    path = Path(state_dir) / "why.json"
    cache = _read(path, {})
    cache = {s: e for s, e in cache.items() if isinstance(e, dict) and e.get("t") and (now - _parse_iso(e["t"])).days < KEEP_DAYS}
    syms = sorted(rows, key=order) if order else sorted(rows)
    targets = [s for s in syms if stale(cache.get(s), now)][:WHY_MAX]
    if not targets or not os.environ.get("GEMINI_KEY", "").strip():
        if targets:
            print("  GEMINI_KEY not set - no 'why trending' lines.")
        _write(path, cache)
        return cache
    entries = []
    for i, s in enumerate(targets, 1):
        row = rows[s]
        news = headlines(s, row.get("n"), now)
        entries.append({"id": i, "sym": s, "name": row.get("n") or s, "material": _material(s, row, news, now)})
        time.sleep(0.3)
    print(f"Asking Gemini once: a 'why trending' line for {len(entries)} stock(s) "
          f"({sum('none found' not in e['material'] for e in entries)} with headlines)...")
    result, model = _gemini(_prompt(entries))
    for e in entries:
        d = result.get(e["id"])
        if d:
            cache[e["sym"]] = {"d": _shorten(d), "t": _iso(now)}
    if result:
        print(f"Got {len(result)} answer(s) from {model}")
    _write(path, cache)
    return cache
