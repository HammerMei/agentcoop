#!/usr/bin/env python3
"""Daily briefing data fetcher.

Fetches all sources in parallel and prints a single JSON object to stdout.
Claude Code reads this JSON and generates the HTML + summary in one LLM pass.

Usage:
    python3 runner.py
"""

from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import email.utils
import html
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICKERS = ["MSFT", "NVDA", "INTC", "PYPL", "BTC"]

YAHOO_QUOTE_URL = (
    "https://query1.finance.yahoo.com/v7/finance/quote"
    "?symbols=MSFT,NVDA,INTC,PYPL,BTC-USD"
)
STOOQ_URL = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"

TECHCRUNCH_RSS_URL = "https://techcrunch.com/feed/"

HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

GITHUB_TRENDING_WEEKLY_URL = "https://github.com/trending?since=weekly"

WORLD_NEWS_RSS_URLS = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
]

ENTERTAINMENT_RSS_URL = "https://tw.news.yahoo.com/rss/entertainment"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _fetch_text(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _fetch_json(url: str, timeout: int = 12):
    return json.loads(_fetch_text(url, timeout=timeout))


def _strip_html(raw: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _truncate(text: str, limit: int = 220) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _placeholder(section: str, n: int) -> list[dict[str, str]]:
    return [
        {
            "headline": f"Unavailable {section} item {i}",
            "summary": "Summary unavailable from source metadata.",
            "link": "Unavailable",
        }
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# Stocks
# ---------------------------------------------------------------------------


def _stooq_symbol(ticker: str) -> str:
    if ticker == "BTC":
        return "btc.v"
    return f"{ticker.lower()}.us"


def _yahoo_ticker(ticker: str) -> str:
    if ticker == "BTC":
        return "BTC-USD"
    return ticker


def fetch_stocks() -> list[dict[str, str]]:  # noqa: C901
    # --- Try Yahoo Finance v7 first ---
    try:
        data = _fetch_json(YAHOO_QUOTE_URL)
        yahoo_quotes = data["quoteResponse"]["result"]
        by_symbol: dict[str, dict] = {}
        for q in yahoo_quotes:
            by_symbol[q.get("symbol", "")] = q

        results: list[dict[str, str]] = []
        for ticker in TICKERS:
            yt = _yahoo_ticker(ticker)
            q = by_symbol.get(yt)
            if q is None:
                results.append(
                    {"symbol": ticker, "price": "Unavailable", "currency": "N/A",
                     "trend": "-", "change_pct": "N/A"}
                )
                continue
            price = q.get("regularMarketPrice")
            chg_pct = q.get("regularMarketChangePercent")
            currency = q.get("currency", "USD")
            if price is None:
                results.append(
                    {"symbol": ticker, "price": "Unavailable", "currency": currency,
                     "trend": "-", "change_pct": "N/A"}
                )
                continue
            trend = "▲" if (chg_pct or 0) > 0 else ("▼" if (chg_pct or 0) < 0 else "-")
            chg_str = (
                f"{chg_pct:+.2f}%" if chg_pct is not None else "N/A"
            )
            results.append(
                {
                    "symbol": ticker,
                    "price": f"{price:.2f}" if isinstance(price, float) else str(price),
                    "currency": currency,
                    "trend": trend,
                    "change_pct": chg_str,
                }
            )
        # If we got at least one real result, return it
        if any(r["price"] != "Unavailable" for r in results):
            return results
        raise ValueError("No usable Yahoo quotes")
    except Exception:
        pass  # fall through to stooq

    # --- Stooq fallback ---
    results = []
    for ticker in TICKERS:
        symbol = _stooq_symbol(ticker)
        url = STOOQ_URL.format(symbol=urllib.parse.quote(symbol))
        try:
            csv_text = _fetch_text(url)
            reader = csv.DictReader(io.StringIO(csv_text))
            row = next(reader)
            close = row.get("Close", "N/A")
            opened = row.get("Open", "N/A")
            if close in {"N/D", "", None}:
                raise ValueError("No quote data")
            trend = "-"
            try:
                if float(close) > float(opened):
                    trend = "▲"
                elif float(close) < float(opened):
                    trend = "▼"
            except Exception:
                pass
            results.append(
                {"symbol": ticker, "price": close, "currency": "USD",
                 "trend": trend, "change_pct": "N/A"}
            )
        except Exception:
            results.append(
                {"symbol": ticker, "price": "Unavailable", "currency": "N/A",
                 "trend": "-", "change_pct": "N/A"}
            )
    return results


# ---------------------------------------------------------------------------
# TechCrunch
# ---------------------------------------------------------------------------


def fetch_techcrunch_top10() -> list[dict[str, str]]:
    try:
        xml_text = _fetch_text(TECHCRUNCH_RSS_URL)
        root = ET.fromstring(xml_text)
        nodes = root.findall("./channel/item")[:10]
    except Exception:
        return _placeholder("TechCrunch", 10)

    items: list[dict[str, str]] = []
    for node in nodes:
        try:
            title = _strip_html(node.findtext("title") or "Untitled")
            link = _strip_html(node.findtext("link") or "")
            desc = _truncate(_strip_html(node.findtext("description") or ""))
            if not desc:
                desc = "Summary unavailable from source metadata."
            items.append({"headline": title, "summary": desc, "link": link or "Unavailable"})
        except Exception:
            items.append(_placeholder("TechCrunch", 1)[0])

    while len(items) < 10:
        items.extend(_placeholder("TechCrunch", 10 - len(items)))
    return items[:10]


# ---------------------------------------------------------------------------
# Hacker News  (items fetched in parallel)
# ---------------------------------------------------------------------------


def _fetch_hn_item(story_id: int) -> dict[str, str]:
    try:
        item = _fetch_json(HN_ITEM_URL.format(id=story_id))
        title = item.get("title") or "Untitled"
        link = item.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
        text_body = _truncate(_strip_html(item.get("text") or ""))
        summary = text_body or "Summary unavailable from source metadata."
        return {"headline": title, "summary": summary, "link": link}
    except Exception:
        return {
            "headline": f"HN story {story_id}",
            "summary": "Summary unavailable from source metadata.",
            "link": f"https://news.ycombinator.com/item?id={story_id}",
        }


def fetch_hn_top10() -> list[dict[str, str]]:
    try:
        ids: list[int] = _fetch_json(HN_TOP_URL)[:10]
    except Exception:
        return _placeholder("Hacker News", 10)

    stories: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_hn_item, sid): sid for sid in ids}
        # preserve ordering: map future back to its position
        ordered: dict[int, dict[str, str]] = {}
        for future in concurrent.futures.as_completed(futures):
            sid = futures[future]
            try:
                ordered[sid] = future.result()
            except Exception:
                ordered[sid] = {
                    "headline": f"HN story {sid}",
                    "summary": "Summary unavailable from source metadata.",
                    "link": f"https://news.ycombinator.com/item?id={sid}",
                }
    for sid in ids:
        stories.append(ordered.get(sid, _placeholder("Hacker News", 1)[0]))

    while len(stories) < 10:
        stories.extend(_placeholder("Hacker News", 10 - len(stories)))
    return stories[:10]


# ---------------------------------------------------------------------------
# GitHub Trending
# ---------------------------------------------------------------------------


def fetch_github_trending_top10() -> list[dict[str, str]]:  # noqa: C901
    try:
        page = _fetch_text(GITHUB_TRENDING_WEEKLY_URL)
    except Exception:
        return _placeholder("GitHub project", 10)

    projects: list[dict[str, str]] = []
    blocks = re.findall(
        r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>',
        page,
        flags=re.I | re.S,
    )
    for block in blocks:
        try:
            link_match = re.search(
                r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"', block, flags=re.I | re.S
            )
            if not link_match:
                continue
            repo_path = html.unescape(link_match.group(1)).strip().strip("/")
            repo_name = repo_path.replace(" ", "")
            if "/" not in repo_name:
                continue
            repo_link = f"https://github.com/{repo_name}"

            # GitHub wraps the repo description in <p class="col-9 color-fg-muted ...">
            # and quotes the text in double-quotes.  Target this class directly
            # rather than scanning all <p> tags (which picks up SVG path data).
            summary = ""
            desc_match = re.search(
                r'<p[^>]*\bcol-9\b[^>]*>(.*?)</p>', block, flags=re.I | re.S
            )
            if desc_match:
                cleaned = _truncate(_strip_html(desc_match.group(1))).strip('"').strip()
                if cleaned and len(cleaned) >= 10:
                    summary = cleaned
            if not summary:
                summary = "Summary unavailable from source metadata."

            projects.append({"headline": repo_name, "summary": summary, "link": repo_link})
            if len(projects) == 10:
                break
        except Exception:
            continue

    while len(projects) < 10:
        projects.extend(_placeholder("GitHub project", 10 - len(projects)))
    return projects[:10]


# ---------------------------------------------------------------------------
# World News
# ---------------------------------------------------------------------------


def fetch_world_news_top5() -> list[dict[str, str]]:  # noqa: C901
    candidates: list[tuple[dt.datetime | None, dict[str, str]]] = []

    for rss_url in WORLD_NEWS_RSS_URLS:
        try:
            xml_text = _fetch_text(rss_url)
            root = ET.fromstring(xml_text)
            nodes = root.findall("./channel/item")
        except Exception:
            continue

        for node in nodes:
            try:
                title = _strip_html(node.findtext("title") or "Untitled")
                link = _strip_html(node.findtext("link") or "")
                desc = _truncate(_strip_html(node.findtext("description") or ""))
                if not desc:
                    desc = "Summary unavailable from source metadata."
                pub = node.findtext("pubDate") or ""
                pub_dt: dt.datetime | None = None
                try:
                    pub_dt = email.utils.parsedate_to_datetime(pub)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=dt.timezone.utc)
                except Exception:
                    pass
                candidates.append(
                    (pub_dt, {"headline": title, "summary": desc, "link": link or "Unavailable"})
                )
            except Exception:
                continue

    seen: set[str] = set()
    selected: list[dict[str, str]] = []
    for _, row in sorted(
        candidates,
        key=lambda item: item[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    ):
        key = row["headline"].strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if len(selected) == 5:
            break

    while len(selected) < 5:
        selected.extend(_placeholder("US/Global news", 5 - len(selected)))
    return selected


# ---------------------------------------------------------------------------
# Entertainment
# ---------------------------------------------------------------------------


def fetch_entertainment_top5() -> list[dict[str, str]]:
    try:
        xml_text = _fetch_text(ENTERTAINMENT_RSS_URL)
        root = ET.fromstring(xml_text)
        nodes = root.findall("./channel/item")[:5]
    except Exception:
        return _placeholder("entertainment", 5)

    items: list[dict[str, str]] = []
    for node in nodes:
        try:
            title = _strip_html(node.findtext("title") or "Untitled")
            link = _strip_html(node.findtext("link") or "")
            desc = _truncate(_strip_html(node.findtext("description") or ""))
            if not desc:
                desc = "Summary unavailable from source metadata."
            items.append({"headline": title, "summary": desc, "link": link or "Unavailable"})
        except Exception:
            items.append(_placeholder("entertainment", 1)[0])

    while len(items) < 5:
        items.extend(_placeholder("entertainment", 5 - len(items)))
    return items[:5]


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def main() -> None:
    la_tz = ZoneInfo("America/Los_Angeles")
    now = dt.datetime.now(tz=la_tz)
    generated_at = now.strftime("%A, %B %-d, %Y %I:%M %p %Z")
    timestamp = int(now.timestamp())

    fetch_tasks = {
        "stocks": fetch_stocks,
        "techcrunch": fetch_techcrunch_top10,
        "hackernews": fetch_hn_top10,
        "github_trending": fetch_github_trending_top10,
        "world_news": fetch_world_news_top5,
        "entertainment": fetch_entertainment_top5,
    }

    results: dict[str, object] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(fetch_tasks)) as pool:
        future_to_key = {pool.submit(fn): key for key, fn in fetch_tasks.items()}
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
            except Exception:
                # Last-resort fallback — should never reach here due to per-function guards
                if key == "stocks":
                    results[key] = [
                        {"symbol": t, "price": "Unavailable", "currency": "N/A",
                         "trend": "-", "change_pct": "N/A"}
                        for t in TICKERS
                    ]
                elif key in ("techcrunch", "hackernews", "github_trending"):
                    results[key] = _placeholder(key, 10)
                else:
                    results[key] = _placeholder(key, 5)

    output = {
        "generated_at": generated_at,
        "timestamp": timestamp,
        "stocks": results.get("stocks", []),
        "techcrunch": results.get("techcrunch", []),
        "hackernews": results.get("hackernews", []),
        "github_trending": results.get("github_trending", []),
        "world_news": results.get("world_news", []),
        "entertainment": results.get("entertainment", []),
    }

    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute last resort — emit a minimal valid JSON so the caller never gets nothing
        import traceback
        error_payload = {
            "generated_at": "Unavailable",
            "timestamp": 0,
            "stocks": [],
            "techcrunch": [],
            "hackernews": [],
            "github_trending": [],
            "world_news": [],
            "entertainment": [],
            "_error": traceback.format_exc(),
        }
        print(json.dumps(error_payload, ensure_ascii=False))
        sys.exit(1)
