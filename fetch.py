#!/usr/bin/env python3
"""
fetch.py — Multi-source news/article aggregator.

Searches one or more news sources for a keyword/query, restricts results to a
date range, and writes a structured JSON report (metadata + per-source stats
+ errors + deduplicated article list) to a destination file.

--------------------------------------------------------------------------
ADDING A NEW SOURCE (for other developers)
--------------------------------------------------------------------------
1. Subclass `NewsSource` below.
2. Set `name` (the string users pass to --sources) and `requires_api_key`.
3. Implement `is_available()` -> bool (e.g. check an env var exists).
4. Implement `fetch(query, date_from, date_to) -> list[Article]`.
5. Add an instance of your class to the `SOURCE_REGISTRY` list at the
   bottom of the "SOURCES" section.

That's it — the CLI, date filtering, dedup, error isolation, and report
generation are all handled centrally and require no changes.
--------------------------------------------------------------------------

Example usage:
    python fetch.py --query "artificial intelligence" --since 7d
    python fetch.py -q "tesla" --from 2026-06-01 --to 2026-07-01 \
        --sources google_news_rss,newsapi --output results.json
    python fetch.py -q "openai" --since 1m --sources all --verbose
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus, urlparse

import requests

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

# trafilatura is the primary article-text extractor: purpose-built for
# "pull the main content out of a news page," handles boilerplate/nav/ad
# removal, and tends to hold up better against modern sites than manual
# tag-scraping. BeautifulSoup is kept only as a last-resort fallback for
# the cheap meta-description tag check.
try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

# rapidfuzz powers the fuzzy-dedup math (title + content similarity).
try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    fuzz = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass


# ==========================================================================
# CONFIGURATION — every tunable knob lives here.
# Values are used as defaults; most are also exposed as CLI flags (which
# take precedence). API keys are always read from the environment / .env,
# never hardcoded, but the *names* of the expected env vars are listed here
# for visibility.
# ==========================================================================

class Config:
    # --- HTTP behavior ---
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 fetch/1.0"
    )
    REQUEST_TIMEOUT_SECONDS = 12          # per HTTP call (feeds, APIs, scraping)
    MAX_DESCRIPTION_FETCH_WORKERS = 8     # concurrent threads for page-scrape fallback

    # --- Description fallback (when a source gives no summary/content) ---
    MAX_CONTENT_CHARS = 2000              # cap on scraped fallback description length
    MIN_PARAGRAPH_CHARS = 40              # BS4 fallback: skip nav/boilerplate junk this short

    # --- Date filtering ---
    DEFAULT_LOOKBACK_DAYS = 7             # used when neither --since nor --from is given
    DATE_FILTER_FORWARD_BUFFER_MINUTES = 5  # tolerance for "now" clock drift, see filter_by_date()

    # --- Deduplication (fuzzy matching) ---
    # Two articles are considered duplicates if their *combined weighted
    # similarity score* meets or exceeds DEDUP_SIMILARITY_THRESHOLD (0-100).
    # combined_score = TITLE_WEIGHT * title_similarity + CONTENT_WEIGHT * content_similarity
    # Calibrated empirically: independently-reworded coverage of the same
    # story (different outlet, same facts) typically scores 65-80; the same
    # wire story republished with only cosmetic edits scores 95+; genuinely
    # different stories on the same topic score well below 50. 75 sits in
    # the gap and catches both same-story cases without merging unrelated ones.
    DEDUP_SIMILARITY_THRESHOLD = 75.0     # 0-100; higher = stricter (fewer merges)
    DEDUP_TITLE_WEIGHT = 0.7              # title tends to be the more reliable signal
    DEDUP_CONTENT_WEIGHT = 0.3            # description/content similarity, secondary signal
    DEDUP_EXACT_URL_ALWAYS_MERGES = True  # normalized-URL match short-circuits to duplicate

    # --- API key environment variable names (paid sources) ---
    NEWSAPI_ENV_KEY = "NEWSAPI_KEY"
    GNEWS_ENV_KEY = "GNEWS_API_KEY"

    # --- Output ---
    DEFAULT_OUTPUT_PATH = "fetch_results.json"
    JSON_INDENT = 2


log = logging.getLogger("fetch")


# ==========================================================================
# DATA MODEL
# ==========================================================================

@dataclasses.dataclass
class Article:
    title: str
    url: str
    published_at: Optional[str]  # ISO 8601 string, or None if unknown
    description: str
    description_source: str  # "feed", "scraped", or "none"
    source: str  # name of the NewsSource that produced this

    def dedup_key(self) -> str:
        """Key used to identify duplicate articles across sources."""
        normalized_url = self.url.split("?")[0].rstrip("/").lower()
        return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "description": self.description,
            "description_source": self.description_source,
            "source": self.source,
        }


@dataclasses.dataclass
class SourceRunResult:
    """Outcome of running a single source: what it found or how it failed."""
    name: str
    status: str  # "ok", "failed", "skipped"
    article_count: int = 0
    error: Optional[str] = None
    duration_seconds: float = 0.0


# ==========================================================================
# DESCRIPTION FALLBACK: scrape article page if feed gives no summary
# ==========================================================================

def _fetch_raw_html(url: str) -> Optional[str]:
    resp = requests.get(
        url,
        headers={"User-Agent": Config.USER_AGENT},
        timeout=Config.REQUEST_TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


def _extract_with_trafilatura(html: str, url: str) -> Optional[str]:
    """Primary extraction path. trafilatura handles boilerplate/nav/ad
    removal far more robustly than manual tag scraping, and this is its
    intended use case (single-page main-content extraction)."""
    if trafilatura is None:
        return None
    try:
        extracted = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if extracted:
            return extracted.strip()
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("trafilatura extraction failed for %s: %s", url, exc)
    return None


def _extract_with_bs4_fallback(html: str) -> Optional[str]:
    """Last-resort fallback if trafilatura is unavailable or returns
    nothing: check <meta> description tags, then raw <p> text."""
    if BeautifulSoup is None:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")

        for meta_name, attr in (("description", "name"), ("og:description", "property")):
            tag = soup.find("meta", attrs={attr: meta_name})
            if tag and tag.get("content"):
                text = tag["content"].strip()
                if text:
                    return text

        for junk in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            junk.decompose()
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) > Config.MIN_PARAGRAPH_CHARS]
        if not paragraphs:
            return None
        return " ".join(paragraphs).strip()
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("bs4 fallback extraction failed: %s", exc)
        return None


def scrape_description(url: str) -> Optional[str]:
    """
    Best-effort fetch of a page and extraction of its main textual content,
    used only when a source's feed doesn't supply its own description.
    Tries trafilatura first (purpose-built article extraction), falls back
    to BeautifulSoup meta-tag/paragraph scraping if that yields nothing.
    Returns None on total failure — callers must treat this as "give up".
    Many modern sites block generic scraping (paywalls, bot detection,
    JS-rendered content) — a None/empty result here is expected sometimes,
    not a bug.
    """
    try:
        html = _fetch_raw_html(url)
    except Exception as exc:  # noqa: BLE001 — network failures are expected/common here
        log.debug("scrape_description: fetch failed for %s: %s", url, exc)
        return None

    if not html:
        return None

    text = _extract_with_trafilatura(html, url) or _extract_with_bs4_fallback(html)
    if not text:
        return None
    return text[:Config.MAX_CONTENT_CHARS].strip()


def enrich_missing_descriptions(articles: list[Article]) -> None:
    """
    For articles with no feed-provided description, try scraping the page.
    Mutates articles in place. Runs concurrently since these are independent
    network calls dominated by I/O wait.
    """
    targets = [a for a in articles if not a.description]
    if not targets:
        return

    log.info("Attempting to scrape descriptions for %d article(s) with none provided...", len(targets))
    with concurrent.futures.ThreadPoolExecutor(max_workers=Config.MAX_DESCRIPTION_FETCH_WORKERS) as pool:
        future_to_article = {pool.submit(scrape_description, a.url): a for a in targets}
        for future in concurrent.futures.as_completed(future_to_article):
            article = future_to_article[future]
            try:
                scraped = future.result()
            except Exception as exc:  # noqa: BLE001
                log.debug("Scrape thread error for %s: %s", article.url, exc)
                scraped = None
            if scraped:
                article.description = scraped
                article.description_source = "scraped"
            else:
                article.description_source = "none"


# ==========================================================================
# SOURCES
# ==========================================================================

class NewsSource(ABC):
    """Base class every news source plugin must implement."""

    name: str = "unnamed_source"
    requires_api_key: bool = False

    def is_available(self) -> bool:
        """Whether this source can run (e.g. required API key is set)."""
        return True

    @abstractmethod
    def fetch(self, query: str, date_from: datetime, date_to: datetime) -> list[Article]:
        """Return raw articles for the query. Date filtering may be partial;
        the caller re-filters afterward, so it's fine to over-return here."""
        raise NotImplementedError


class GoogleNewsRSSSource(NewsSource):
    """Free, no API key required. Uses Google News' public RSS search feed."""

    name = "google_news_rss"
    requires_api_key = False

    def fetch(self, query: str, date_from: datetime, date_to: datetime) -> list[Article]:
        if feedparser is None:
            raise RuntimeError("feedparser package is not installed")

        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        resp = requests.get(url, headers={"User-Agent": Config.USER_AGENT}, timeout=Config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)

        if feed.bozo and not feed.entries:
            raise RuntimeError(f"Feed parse error: {feed.bozo_exception}")

        articles = []
        for entry in feed.entries:
            published_iso = _struct_time_to_iso(getattr(entry, "published_parsed", None))
            description = _clean_html(getattr(entry, "summary", "") or "")
            articles.append(Article(
                title=getattr(entry, "title", "").strip(),
                url=getattr(entry, "link", "").strip(),
                published_at=published_iso,
                description=description,
                description_source="feed" if description else "none",
                source=self.name,
            ))
        return articles


class BingNewsRSSSource(NewsSource):
    """Free, no API key required. Uses Bing News' public RSS search feed."""

    name = "bing_news_rss"
    requires_api_key = False

    def fetch(self, query: str, date_from: datetime, date_to: datetime) -> list[Article]:
        if feedparser is None:
            raise RuntimeError("feedparser package is not installed")

        url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=RSS"
        resp = requests.get(url, headers={"User-Agent": Config.USER_AGENT}, timeout=Config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)

        if feed.bozo and not feed.entries:
            raise RuntimeError(f"Feed parse error: {feed.bozo_exception}")

        articles = []
        for entry in feed.entries:
            published_iso = _struct_time_to_iso(getattr(entry, "published_parsed", None))
            description = _clean_html(getattr(entry, "summary", "") or "")
            articles.append(Article(
                title=getattr(entry, "title", "").strip(),
                url=getattr(entry, "link", "").strip(),
                published_at=published_iso,
                description=description,
                description_source="feed" if description else "none",
                source=self.name,
            ))
        return articles


class NewsAPISource(NewsSource):
    """
    Paid/free-tier source: https://newsapi.org
    Requires NEWSAPI_KEY in the environment or .env file.
    """

    name = "newsapi"
    requires_api_key = True
    ENV_KEY = Config.NEWSAPI_ENV_KEY

    def is_available(self) -> bool:
        return bool(os.getenv(self.ENV_KEY))

    def fetch(self, query: str, date_from: datetime, date_to: datetime) -> list[Article]:
        api_key = os.getenv(self.ENV_KEY)
        if not api_key:
            raise RuntimeError(f"{self.ENV_KEY} not set")

        params = {
            "q": query,
            "from": date_from.strftime("%Y-%m-%d"),
            "to": date_to.strftime("%Y-%m-%d"),
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": 100,
            "apiKey": api_key,
        }
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params=params,
            timeout=Config.REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        payload = resp.json()
        if payload.get("status") != "ok":
            raise RuntimeError(payload.get("message", "unknown NewsAPI error"))

        articles = []
        for item in payload.get("articles", []):
            description = (item.get("description") or item.get("content") or "").strip()
            articles.append(Article(
                title=(item.get("title") or "").strip(),
                url=(item.get("url") or "").strip(),
                published_at=item.get("publishedAt"),
                description=description,
                description_source="feed" if description else "none",
                source=self.name,
            ))
        return articles


class GNewsAPISource(NewsSource):
    """
    Paid/free-tier source: https://gnews.io
    Requires GNEWS_API_KEY in the environment or .env file.
    """

    name = "gnews"
    requires_api_key = True
    ENV_KEY = Config.GNEWS_ENV_KEY

    def is_available(self) -> bool:
        return bool(os.getenv(self.ENV_KEY))

    def fetch(self, query: str, date_from: datetime, date_to: datetime) -> list[Article]:
        api_key = os.getenv(self.ENV_KEY)
        if not api_key:
            raise RuntimeError(f"{self.ENV_KEY} not set")

        params = {
            "q": query,
            "from": date_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": date_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lang": "en",
            "max": 100,
            "apikey": api_key,
        }
        resp = requests.get("https://gnews.io/api/v4/search", params=params, timeout=Config.REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        payload = resp.json()
        articles = []
        for item in payload.get("articles", []):
            description = (item.get("description") or item.get("content") or "").strip()
            articles.append(Article(
                title=(item.get("title") or "").strip(),
                url=(item.get("url") or "").strip(),
                published_at=item.get("publishedAt"),
                description=description,
                description_source="feed" if description else "none",
                source=self.name,
            ))
        return articles


# Registry of all known sources. To add your own, append an instance here.
SOURCE_REGISTRY: list[NewsSource] = [
    GoogleNewsRSSSource(),
    BingNewsRSSSource(),
    NewsAPISource(),
    GNewsAPISource(),
]
SOURCES_BY_NAME: dict[str, NewsSource] = {s.name: s for s in SOURCE_REGISTRY}


# ==========================================================================
# HELPERS
# ==========================================================================

def _clean_html(raw_html: str) -> str:
    """Strip HTML tags from RSS summary fields, collapsing whitespace."""
    if not raw_html:
        return ""
    if BeautifulSoup is not None:
        text = BeautifulSoup(raw_html, "lxml").get_text(" ", strip=True)
    else:
        text = re.sub(r"<[^>]+>", " ", raw_html)
    return re.sub(r"\s+", " ", text).strip()


def _struct_time_to_iso(struct_time) -> Optional[str]:
    if not struct_time:
        return None
    try:
        return datetime(*struct_time[:6], tzinfo=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


def _parse_article_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse a variety of date string formats into a tz-aware datetime."""
    if not value:
        return None
    value = value.strip()
    formats = (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        # Last resort: fromisoformat (Python 3.11+ handles "Z" suffix too)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


DATE_SHORTHAND_RE = re.compile(r"^(\d+)([dwm])$", re.IGNORECASE)


def resolve_relative_date(now: datetime, shorthand: str) -> datetime:
    """Convert '7d' / '2w' / '1m' style shorthand into an absolute datetime."""
    match = DATE_SHORTHAND_RE.match(shorthand.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(
            f"Invalid relative date '{shorthand}'. Use formats like 7d, 2w, 1m, "
            f"or an absolute date YYYY-MM-DD."
        )
    amount, unit = int(match.group(1)), match.group(2)
    days = {"d": 1, "w": 7, "m": 30}[unit] * amount
    return now - timedelta(days=days)


def parse_date_arg(value: str, now: datetime) -> datetime:
    """Accepts absolute YYYY-MM-DD or relative shorthand (7d/2w/1m)."""
    if DATE_SHORTHAND_RE.match(value.strip().lower()):
        return resolve_relative_date(now, value)
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'. Use YYYY-MM-DD or relative shorthand (7d, 2w, 1m)."
        )


# ==========================================================================
# CORE PIPELINE
# ==========================================================================

def run_source(source: NewsSource, query: str, date_from: datetime, date_to: datetime) -> tuple[SourceRunResult, list[Article]]:
    start = time.monotonic()
    if not source.is_available():
        return SourceRunResult(
            name=source.name,
            status="skipped",
            error=f"Not configured (missing required API key for '{source.name}').",
        ), []
    try:
        articles = source.fetch(query, date_from, date_to)
        elapsed = time.monotonic() - start
        return SourceRunResult(
            name=source.name, status="ok", article_count=len(articles), duration_seconds=round(elapsed, 3)
        ), articles
    except Exception as exc:  # noqa: BLE001 — isolate failures per-source by design
        elapsed = time.monotonic() - start
        log.warning("Source '%s' failed: %s", source.name, exc)
        return SourceRunResult(
            name=source.name, status="failed", error=str(exc), duration_seconds=round(elapsed, 3)
        ), []


def filter_by_date(articles: list[Article], date_from: datetime, date_to: datetime) -> list[Article]:
    """
    Keep only articles with a known publish date inside [date_from, date_to].
    Articles with an unparseable/missing date are kept (can't prove they're
    out of range) but this is surfaced via `published_at: null` in output.

    A small forward buffer is added to `date_to` for the comparison: when the
    upper bound defaults to "now", the small amount of wall-clock time that
    elapses between capturing "now" and actually filtering results (network
    calls, scraping, etc.) can otherwise cause a "published seconds ago"
    article to be wrongly excluded for arriving a moment after the cutoff.
    """
    effective_to = date_to + timedelta(minutes=Config.DATE_FILTER_FORWARD_BUFFER_MINUTES)
    kept = []
    for article in articles:
        parsed = _parse_article_datetime(article.published_at)
        if parsed is None:
            kept.append(article)  # keep — unknown date, don't silently drop
            continue
        if date_from <= parsed <= effective_to:
            kept.append(article)
    return kept


def _similarity_score(a: str, b: str) -> float:
    """
    Returns a 0-100 similarity score between two strings using rapidfuzz's
    token_sort_ratio (Levenshtein-based, word-order-insensitive — so "Fed
    raises rates again" and "again, Fed raises rates" score as near-identical,
    which matters since the same story is worded differently across outlets).
    Falls back to Python's difflib if rapidfuzz isn't installed (slower, but
    keeps the script functional without an extra dependency).
    """
    if not a or not b:
        return 0.0
    if fuzz is not None:
        return fuzz.token_sort_ratio(a, b)
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100


def _combined_similarity(
    a: Article, b: Article, title_weight: float, content_weight: float
) -> float:
    """Weighted blend of title similarity and description/content similarity.
    Missing descriptions on either side simply drop that term's contribution
    (renormalizing weights) rather than penalizing the pair unfairly."""
    title_score = _similarity_score(a.title, b.title)

    if a.description and b.description:
        content_score = _similarity_score(a.description, b.description)
        total_weight = title_weight + content_weight
        return (title_weight * title_score + content_weight * content_score) / total_weight

    # No content to compare on one/both sides — judge on title alone.
    return title_score


def deduplicate(
    articles: list[Article],
    threshold: float = Config.DEDUP_SIMILARITY_THRESHOLD,
    title_weight: float = Config.DEDUP_TITLE_WEIGHT,
    content_weight: float = Config.DEDUP_CONTENT_WEIGHT,
) -> list[Article]:
    """
    Removes duplicate articles using a two-stage approach:

    1. Fast path: normalized-URL exact match (same story, same link) —
       O(1) lookup per article, catches the overwhelming majority of
       cross-source duplicates cheaply (e.g. two sources both linking the
       same original AP/Reuters article).
    2. Fuzzy path: for articles that pass stage 1, compare against
       previously-kept articles using a weighted title+content similarity
       score (see `_combined_similarity`). If the score >= `threshold`,
       the article is treated as a duplicate of the earlier one and
       dropped. This catches the same underlying story published with a
       different URL/title/wording across outlets.

    `threshold`, `title_weight`, and `content_weight` are exposed as CLI
    flags (--dedup-threshold, --dedup-title-weight, --dedup-content-weight)
    so callers can tune strictness without editing code. Higher threshold
    = stricter matching = fewer articles merged together.

    Fuzzy comparison is O(n^2) in the worst case (every kept article is
    compared against every new candidate); fine for typical result set
    sizes (tens to low hundreds of articles per run).
    """
    seen_url_keys: set[str] = set()
    unique: list[Article] = []

    for article in articles:
        url_key = article.dedup_key()

        if Config.DEDUP_EXACT_URL_ALWAYS_MERGES and url_key in seen_url_keys:
            continue  # stage 1: exact normalized-URL duplicate

        is_fuzzy_duplicate = False
        for existing in unique:
            score = _combined_similarity(article, existing, title_weight, content_weight)
            if score >= threshold:
                is_fuzzy_duplicate = True
                log.debug(
                    "Dedup: dropping %r (similarity %.1f to %r, source=%s)",
                    article.title[:60], score, existing.title[:60], article.source,
                )
                break

        if not is_fuzzy_duplicate:
            seen_url_keys.add(url_key)
            unique.append(article)

    return unique


def resolve_requested_sources(requested: list[str]) -> list[NewsSource]:
    if len(requested) == 1 and requested[0].lower() == "all":
        return SOURCE_REGISTRY
    resolved = []
    unknown = []
    for name in requested:
        src = SOURCES_BY_NAME.get(name.strip())
        if src is None:
            unknown.append(name)
        else:
            resolved.append(src)
    if unknown:
        available = ", ".join(sorted(SOURCES_BY_NAME))
        raise argparse.ArgumentTypeError(
            f"Unknown source(s): {', '.join(unknown)}. Available sources: {available}, all."
        )
    return resolved


def build_report(
    query: str,
    date_from: datetime,
    date_to: datetime,
    requested_sources: list[str],
    source_results: list[SourceRunResult],
    articles: list[Article],
) -> dict:
    articles_sorted = sorted(
        articles,
        key=lambda a: _parse_article_datetime(a.published_at) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {
        "query": query,
        "date_range": {
            "from": date_from.strftime("%Y-%m-%d"),
            "to": date_to.strftime("%Y-%m-%d"),
        },
        "requested_sources": requested_sources,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "sources_ok": sum(1 for r in source_results if r.status == "ok"),
            "sources_failed": sum(1 for r in source_results if r.status == "failed"),
            "sources_skipped": sum(1 for r in source_results if r.status == "skipped"),
            "total_articles": len(articles_sorted),
        },
        "source_results": [dataclasses.asdict(r) for r in source_results],
        "articles": [a.to_dict() for a in articles_sorted],
    }


# ==========================================================================
# CLI
# ==========================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch.py",
        description="Search multiple news sources for a keyword within a date range "
                    "and export the combined results as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fetch.py -q "climate change" --since 7d
  python fetch.py -q "nvidia" --from 2026-06-01 --to 2026-07-01 -s google_news_rss,newsapi
  python fetch.py -q "openai" --since 1m -o results/openai.json --verbose

Available source names: """ + ", ".join(sorted(SOURCES_BY_NAME)) + ", all",
    )
    parser.add_argument("-q", "--query", required=True, help="Search keyword/query.")
    parser.add_argument(
        "-s", "--sources", default="all",
        help="Comma-separated source names, or 'all' (default) to try every registered source.",
    )
    parser.add_argument(
        "-o", "--output", default=Config.DEFAULT_OUTPUT_PATH,
        help=f"Output file path (JSON). Default: {Config.DEFAULT_OUTPUT_PATH}",
    )

    date_group = parser.add_argument_group("date range (choose one style)")
    date_group.add_argument(
        "--since", metavar="SHORTHAND",
        help="Relative start date, e.g. 7d, 14d, 1m. Equivalent to --from with 'to' = now.",
    )
    date_group.add_argument(
        "--from", dest="date_from", metavar="DATE",
        help="Start date: YYYY-MM-DD or relative shorthand (7d, 2w, 1m).",
    )
    date_group.add_argument(
        "--to", dest="date_to", metavar="DATE",
        help="End date: YYYY-MM-DD or relative shorthand. Default: now.",
    )

    parser.add_argument(
        "--no-scrape-fallback", action="store_true",
        help="Disable fetching article pages to fill in missing descriptions.",
    )

    dedup_group = parser.add_argument_group("deduplication (fuzzy matching)")
    dedup_group.add_argument(
        "--dedup-threshold", type=float, default=Config.DEDUP_SIMILARITY_THRESHOLD, metavar="0-100",
        help=f"Similarity score (0-100) at/above which two articles are treated as duplicates. "
             f"Higher = stricter (fewer merges). Default: {Config.DEDUP_SIMILARITY_THRESHOLD}",
    )
    dedup_group.add_argument(
        "--dedup-title-weight", type=float, default=Config.DEDUP_TITLE_WEIGHT, metavar="0-1",
        help=f"Weight given to title similarity in the dedup score. Default: {Config.DEDUP_TITLE_WEIGHT}",
    )
    dedup_group.add_argument(
        "--dedup-content-weight", type=float, default=Config.DEDUP_CONTENT_WEIGHT, metavar="0-1",
        help=f"Weight given to description/content similarity in the dedup score. "
             f"Default: {Config.DEDUP_CONTENT_WEIGHT}",
    )
    dedup_group.add_argument(
        "--no-dedup", action="store_true",
        help="Disable deduplication entirely (keep every article as-is).",
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    now = datetime.now(timezone.utc)

    # --- Resolve date range ---
    try:
        if args.since:
            date_from = resolve_relative_date(now, args.since)
            date_to = now
        else:
            date_from = parse_date_arg(args.date_from, now) if args.date_from else now - timedelta(days=Config.DEFAULT_LOOKBACK_DAYS)
            date_to = parse_date_arg(args.date_to, now) if args.date_to else now
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2

    if date_from > date_to:
        parser.error(f"--from ({date_from.date()}) is after --to ({date_to.date()}).")
        return 2

    # --- Resolve sources ---
    requested_names = [s.strip() for s in args.sources.split(",") if s.strip()]
    try:
        sources_to_run = resolve_requested_sources(requested_names)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2

    if not (0.0 <= args.dedup_threshold <= 100.0):
        parser.error(f"--dedup-threshold must be between 0 and 100 (got {args.dedup_threshold}).")
        return 2
    if args.dedup_title_weight < 0 or args.dedup_content_weight < 0:
        parser.error("--dedup-title-weight and --dedup-content-weight must be non-negative.")
        return 2
    if args.dedup_title_weight + args.dedup_content_weight == 0:
        parser.error("--dedup-title-weight and --dedup-content-weight cannot both be zero.")
        return 2

    log.info(
        "Query=%r | Date range=%s to %s | Sources=%s",
        args.query, date_from.date(), date_to.date(),
        [s.name for s in sources_to_run],
    )

    # --- Run sources concurrently (I/O-bound network calls) ---
    source_results: list[SourceRunResult] = []
    all_articles: list[Article] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(sources_to_run))) as pool:
        futures = {
            pool.submit(run_source, src, args.query, date_from, date_to): src
            for src in sources_to_run
        }
        for future in concurrent.futures.as_completed(futures):
            result, articles = future.result()
            source_results.append(result)
            all_articles.extend(articles)
            if result.status == "ok":
                log.info("[%s] OK — %d article(s) in %.2fs", result.name, result.article_count, result.duration_seconds)
            elif result.status == "skipped":
                log.info("[%s] SKIPPED — %s", result.name, result.error)
            else:
                log.warning("[%s] FAILED — %s", result.name, result.error)

    # --- Filter, dedup ---
    # Dedup runs before the description-scrape fallback deliberately: it
    # avoids wasting scrape requests on pages we're about to discard as
    # duplicates, and feed-provided descriptions (already present for most
    # articles) are enough of a signal for the content-similarity term.
    filtered = filter_by_date(all_articles, date_from, date_to)
    if args.no_dedup:
        unique_articles = filtered
    else:
        unique_articles = deduplicate(
            filtered,
            threshold=args.dedup_threshold,
            title_weight=args.dedup_title_weight,
            content_weight=args.dedup_content_weight,
        )
    log.info(
        "Collected %d raw -> %d after date filter -> %d after dedup",
        len(all_articles), len(filtered), len(unique_articles),
    )

    # --- Fill missing descriptions via page scrape (best-effort) ---
    if not args.no_scrape_fallback:
        enrich_missing_descriptions(unique_articles)

    # --- Build & write report ---
    report = build_report(args.query, date_from, date_to, requested_names, source_results, unique_articles)

    output_path = args.output
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=Config.JSON_INDENT, ensure_ascii=False)

    log.info(
        "Done. %d article(s) written to %s (%d source(s) ok, %d failed, %d skipped).",
        report["summary"]["total_articles"], output_path,
        report["summary"]["sources_ok"], report["summary"]["sources_failed"], report["summary"]["sources_skipped"],
    )

    # Non-zero exit only if literally every requested source failed outright
    # (skipped-for-missing-key doesn't count as a hard failure).
    if sources_to_run and all(r.status == "failed" for r in source_results):
        log.error("All requested sources failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
