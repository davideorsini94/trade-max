"""Curated news fetching via feedparser (blueprint sections 5.2, 5.4, 5.6, 10).

``NewsService`` reads only the whitelisted feeds in ``app.data.sources`` — no
URL is ever built from LLM or user-supplied data beyond substituting a
validated ticker into the ``CORPORATE_SOURCES`` templates. Every network call
is synchronous (httpx + feedparser are blocking); call the public methods via
``asyncio.to_thread`` from async code.

Robustness contract:
- every feed fetch is wrapped in try/except so one dead/slow feed never
  breaks the batch or propagates to the caller;
- all network calls use a 20s timeout;
- items are de-duplicated by URL against ``news_items`` and items older than
  7 days are pruned on every fetch;
- summaries are HTML-stripped and truncated to 400 characters;
- an in-memory TTL cache (30 minutes per source key) avoids re-fetching the
  same feed on every call within that window.

The 30-minute cache only helps if the *same* ``NewsService` instance is reused
across calls (e.g. by the scheduler's ``news_refresh`` job and the
orchestrator) — a shared ``news_service`` singleton is exported below for
exactly that purpose.
"""

from __future__ import annotations

import calendar
import html
import re
import time
from datetime import datetime, timedelta

import feedparser
import httpx
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.data.news_select import score_item, select_macro_items
from app.data.sources import CORPORATE_SOURCES, MACRO_SOURCES, SEC_EDGAR_USER_AGENT
from app.models import NewsItem

# Generic User-Agent for feeds that don't require a specific one (SEC EDGAR
# is the exception — it gets ``SEC_EDGAR_USER_AGENT`` instead, per blueprint
# section 5.6 / SEC's developer policy).
_DEFAULT_FEED_USER_AGENT = "trade-max/1.0 (+davide.orsini@promedital.it)"

_NETWORK_TIMEOUT_SECONDS = 20.0
_CACHE_TTL_SECONDS = 30 * 60.0
_MAX_SUMMARY_CHARS = 400
_MAX_TITLE_CHARS = 300
_MAX_URL_CHARS = 600
_FRESHNESS_HOURS = 72
_MAX_ITEMS_RETURNED = 15
_MAX_ITEMS_PER_SOURCE = 3
_PRUNE_AFTER_DAYS = 7

# Human-readable display names for the per-ticker CORPORATE_SOURCES templates
# (which, unlike MACRO_SOURCES, carry no "name" field in the whitelist itself).
_CORPORATE_SOURCE_NAMES: dict[str, str] = {
    "yahoo_sym": "Yahoo Finance",
    "sec_edgar": "SEC EDGAR",
    "cnbc_biz": "CNBC Business",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(raw: str) -> str:
    """Strip HTML tags/entities from an RSS/Atom summary and collapse whitespace."""
    if not raw:
        return ""
    without_tags = _HTML_TAG_RE.sub(" ", raw)
    unescaped = html.unescape(without_tags)
    return _WHITESPACE_RE.sub(" ", unescaped).strip()


def _parse_published(entry: dict) -> datetime | None:
    """Extract a naive-UTC publish datetime from a feedparser entry, if present."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    try:
        return datetime.utcfromtimestamp(calendar.timegm(struct))
    except (TypeError, ValueError, OverflowError):
        return None


class NewsService:
    """Feedparser-backed macro/corporate news fetching with DB dedup + TTL cache."""

    def __init__(
        self,
        request_timeout: float = _NETWORK_TIMEOUT_SECONDS,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        self._request_timeout = request_timeout
        self._cache_ttl_seconds = cache_ttl_seconds
        # cache_key -> (monotonic_fetch_time, parsed_entries)
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_macro(self, db: Session) -> list[dict]:
        """Refresh all MACRO_SOURCES feeds and return the freshest cached items.

        Returns up to 15 items younger than 72h as
        ``{source, title, summary, published_at}`` dicts, most recent first.
        """
        for source_key, meta in MACRO_SOURCES.items():
            try:
                entries = self._get_cached_or_fetch(source_key, meta["url"])
                self._persist(db, source_key=source_key, category="MACRO", ticker=None, entries=entries)
            except Exception:
                # One dead/misbehaving feed must never break the whole batch.
                continue

        try:
            self._prune_old(db)
        except Exception:
            db.rollback()

        return self._latest_from_db(db, category="MACRO", ticker=None)

    def fetch_corporate(self, db: Session, ticker: str) -> list[dict]:
        """Refresh all CORPORATE_SOURCES feeds for ``ticker`` and return latest items.

        Returns up to 15 items younger than 72h as
        ``{source, title, summary, published_at}`` dicts, most recent first.
        """
        normalized = (ticker or "").strip().upper()
        if not normalized:
            return []

        for source_key, url_template in CORPORATE_SOURCES.items():
            try:
                url = url_template.format(ticker=normalized)
                cache_key = f"{source_key}:{normalized}"
                headers = (
                    {"User-Agent": SEC_EDGAR_USER_AGENT} if source_key == "sec_edgar" else None
                )
                entries = self._get_cached_or_fetch(cache_key, url, headers=headers)
                self._persist(
                    db,
                    source_key=source_key,
                    category="CORPORATE",
                    ticker=normalized,
                    entries=entries,
                )
            except Exception:
                continue

        try:
            self._prune_old(db)
        except Exception:
            db.rollback()

        return self._latest_from_db(db, category="CORPORATE", ticker=normalized)

    # ------------------------------------------------------------------
    # Fetch + cache
    # ------------------------------------------------------------------

    def _get_cached_or_fetch(
        self, cache_key: str, url: str, headers: dict[str, str] | None = None
    ) -> list[dict]:
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None and (now - cached[0]) < self._cache_ttl_seconds:
            return cached[1]
        entries = self._parse_feed(url, headers)
        self._cache[cache_key] = (now, entries)
        return entries

    def _parse_feed(self, url: str, headers: dict[str, str] | None) -> list[dict]:
        raw = self._fetch_raw(url, headers)
        if raw is None:
            return []
        try:
            parsed = feedparser.parse(raw)
        except Exception:
            return []

        entries: list[dict] = []
        for entry in getattr(parsed, "entries", None) or []:
            try:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                if not title or not link:
                    continue
                raw_summary = entry.get("summary") or entry.get("description") or ""
                summary = _strip_html(raw_summary)[:_MAX_SUMMARY_CHARS]
                published_at = _parse_published(entry)
                entries.append(
                    {
                        "title": title[:_MAX_TITLE_CHARS],
                        "url": link[:_MAX_URL_CHARS],
                        "summary": summary,
                        "published_at": published_at,
                    }
                )
            except Exception:
                # A single malformed entry must not drop the rest of the feed.
                continue
        return entries

    def _fetch_raw(self, url: str, headers: dict[str, str] | None) -> bytes | None:
        request_headers = {"User-Agent": _DEFAULT_FEED_USER_AGENT}
        if headers:
            request_headers.update(headers)
        try:
            response = httpx.get(
                url,
                headers=request_headers,
                timeout=self._request_timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.content
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Persistence (dedup by url, pruning, freshness read)
    # ------------------------------------------------------------------

    def _persist(
        self,
        db: Session,
        *,
        source_key: str,
        category: str,
        ticker: str | None,
        entries: list[dict],
    ) -> None:
        if not entries:
            return
        try:
            for entry in entries:
                url = entry["url"]
                exists = db.query(NewsItem).filter(NewsItem.url == url).one_or_none()
                if exists is not None:
                    continue
                db.add(
                    NewsItem(
                        source_key=source_key,
                        category=category,
                        ticker=ticker,
                        title=entry["title"],
                        url=url,
                        summary=entry["summary"],
                        # Some feeds (e.g. ESMA) omit a pubDate entirely; without a
                        # fallback these rows get published_at=NULL and are excluded
                        # forever by _latest_from_db's "isnot(None)" freshness filter.
                        # First-seen time is a reasonable proxy given the 30-minute
                        # refresh cadence — ma è un valore INVENTATO, quindi viene
                        # dichiarato tale nella colonna qui sotto e la selezione non
                        # lo tratta come prova di freschezza (era il motivo per cui
                        # quattro elementi ESMA di boilerplate finivano nel payload).
                        published_at=entry["published_at"] or datetime.utcnow(),
                        published_is_estimated=entry["published_at"] is None,
                        fetched_at=datetime.utcnow(),
                    )
                )
            db.commit()
        except Exception:
            db.rollback()
            raise

    def _prune_old(self, db: Session) -> None:
        """Delete news_items older than 7 days (by published_at, or fetched_at as fallback)."""
        cutoff = datetime.utcnow() - timedelta(days=_PRUNE_AFTER_DAYS)
        db.query(NewsItem).filter(
            or_(
                and_(NewsItem.published_at.isnot(None), NewsItem.published_at < cutoff),
                and_(NewsItem.published_at.is_(None), NewsItem.fetched_at < cutoff),
            )
        ).delete(synchronize_session=False)
        db.commit()

    def _latest_from_db(
        self, db: Session, *, category: str, ticker: str | None
    ) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(hours=_FRESHNESS_HOURS)
        query = db.query(NewsItem).filter(
            NewsItem.category == category,
            NewsItem.published_at.isnot(None),
            NewsItem.published_at >= cutoff,
        )
        query = (
            query.filter(NewsItem.ticker.is_(None))
            if ticker is None
            else query.filter(NewsItem.ticker == ticker)
        )
        rows = query.order_by(NewsItem.published_at.desc()).all()

        if category == "MACRO":
            # Selezione per RILEVANZA con quote per tema (vedi
            # ``app.data.news_select``): la sola recenza faceva sparire dal
            # payload un attacco missilistico perché CNBC pubblica 76 elementi su
            # 120 e i suoi 3 posti andavano all'ultimo quarto d'ora. Il totale
            # resta 15: cambia quali 15, non quanti.
            selected = select_macro_items(rows, limit=_MAX_ITEMS_RETURNED)
        else:
            # Le notizie societarie restano ordinate per recenza: sono già
            # filtrate per ticker, quindi non esiste il problema di un tema che
            # ne scaccia un altro.
            selected = []
            overflow: list[NewsItem] = []
            per_source_count: dict[str, int] = {}
            for row in rows:
                if per_source_count.get(row.source_key, 0) < _MAX_ITEMS_PER_SOURCE:
                    selected.append(row)
                    per_source_count[row.source_key] = per_source_count.get(row.source_key, 0) + 1
                else:
                    overflow.append(row)
            if len(selected) < _MAX_ITEMS_RETURNED:
                selected.extend(overflow[: _MAX_ITEMS_RETURNED - len(selected)])
            selected.sort(key=lambda row: row.published_at, reverse=True)
            selected = selected[:_MAX_ITEMS_RETURNED]

        out: list[dict] = []
        for row in selected:
            item: dict = {
                "source": self._display_name(row.source_key, category),
                "title": row.title,
                "summary": row.summary,
                "published_at": row.published_at,
            }
            if category == "MACRO":
                # Il tema costa ~35 token su tutto il payload e aiuta l'agente a
                # raggruppare le evidenze invece di trattare 15 titoli come un
                # elenco piatto.
                topic, _score = score_item(row)
                if topic:
                    item["topic"] = topic
            out.append(item)
        return out

    @staticmethod
    def _display_name(source_key: str, category: str) -> str:
        if category == "MACRO":
            meta = MACRO_SOURCES.get(source_key)
            return meta["name"] if meta else source_key
        return _CORPORATE_SOURCE_NAMES.get(source_key, source_key)


# Shared instance so the 30-minute TTL cache is actually effective across
# repeated calls (scheduler jobs, orchestrator runs). Modules that need
# independent cache lifetimes may still construct their own ``NewsService()``.
news_service = NewsService()
