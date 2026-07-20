"""Market data access layer built on yfinance (blueprint sections 4, 5.2, 5.5).

``MarketDataService`` wraps every yfinance/network call needed by the rest of
the app: symbol search & validation, OHLCV history refresh/read, fundamentals
and cheap quotes. yfinance is a synchronous library, so every method here is
plain blocking code — callers on the asyncio event loop (API routers, the
orchestrator) MUST invoke these via ``asyncio.to_thread`` (blueprint section
5.5 / 10, friction point 3).

A shared ``market_data_service`` instance is exported for convenience; the
class itself holds no mutable state beyond an httpx timeout, so constructing
a private instance elsewhere is equally safe.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import httpx
import pandas as pd
import yfinance as yf
from sqlalchemy.orm import Session

from app.models import PriceHistory, Symbol

# Fallback Yahoo Finance search endpoint used when the bundled yfinance
# ``Search`` helper fails or returns nothing (blueprint section 4, endpoint #2).
_YAHOO_SEARCH_FALLBACK_URL = "https://query2.finance.yahoo.com/v1/finance/search"

# A generic desktop User-Agent avoids naive bot-blocking on the Yahoo search
# fallback endpoint; SEC/other feed-specific User-Agents live in app.data.news.
_DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; trade-max/1.0; +davide.orsini@promedital.it)"

# Fundamentals fields exposed to the agent layer (blueprint section 5.2), with
# their source key(s) in yfinance's ``Ticker.info`` dict.
_FUNDAMENTALS_FIELDS: dict[str, tuple[str, ...]] = {
    "pe": ("trailingPE",),
    "forward_pe": ("forwardPE",),
    "eps": ("trailingEps",),
    "market_cap": ("marketCap",),
    "dividend_yield": ("dividendYield",),
    "beta": ("beta",),
    "margins": ("profitMargins",),
    "revenue_growth": ("revenueGrowth",),
    "debt_to_equity": ("debtToEquity",),
    "analyst_target": ("targetMeanPrice",),
}

_PRICE_SUMMARY_EMPTY: dict[str, float | None] = {
    "close": None,
    "change_pct_1d": None,
    "change_pct_5d": None,
    "change_pct_30d": None,
    "change_pct_90d": None,
    "high_52w": None,
    "low_52w": None,
    "avg_volume": None,
}

_TRADING_DAYS_52W = 252

# --------------------------------------------------------------------------- #
# Sentiment & relative-performance caches and constants (blueprint addendum)
# --------------------------------------------------------------------------- #

# In-memory sentiment cache: {ticker: (data, fetched_at)}. Mirrors the
# fundamentals cache convention in ``app.api.symbols`` (same shape/logic); the
# many ``Ticker`` sub-reads a snapshot needs are slow, so a recent snapshot is
# reused. A fully-empty/failed snapshot is NOT cached, so a transient error
# self-heals on the next call.
_SENTIMENT_CACHE_TTL = timedelta(minutes=60)
_sentiment_cache: dict[str, tuple[dict, datetime]] = {}

# In-memory benchmark-changes cache keyed by the BENCHMARK ticker (not the
# stock), so one index fetch serves every symbol on that exchange for 6 hours.
_BENCHMARK_CACHE_TTL = timedelta(hours=6)
_benchmark_cache: dict[str, tuple[tuple[float | None, float | None], datetime]] = {}

# Exchange-suffix -> benchmark index (longest matching suffix wins). Index
# tickers are Yahoo Finance symbols; the default fallback is the S&P 500.
_BENCHMARK_SUFFIX_MAP: dict[str, str] = {
    ".MI": "FTSEMIB.MI",
    ".DE": "^GDAXI",
    ".PA": "^FCHI",
    ".L": "^FTSE",
    ".AS": "^AEX",
    ".MC": "^IBEX",
    ".SW": "^SSMI",
    ".TO": "^GSPTSE",
}
_DEFAULT_BENCHMARK = "^GSPC"


def _empty_sentiment() -> dict:
    """A fresh, fully-empty sentiment snapshot (every key present, safe defaults)."""
    return {
        "recommendation_mean": None,
        "recommendation_key": None,
        "analyst_count": None,
        "target_mean": None,
        "target_high": None,
        "target_low": None,
        "ratings_trend": [],
        "ratings_changes": [],
        "insider_net_shares_6m": None,
        "insider_buy_trans_6m": None,
        "insider_sell_trans_6m": None,
        "insider_transactions": [],
        "insiders_pct_held": None,
        "institutions_pct_held": None,
        "top_institutional_holders": [],
        "short_percent_of_float": None,
        "short_ratio": None,
        "next_earnings_date": None,
        "days_to_earnings": None,
    }


def _safe_float(value: object) -> float | None:
    """Best-effort conversion to a plain finite float, else None."""
    if value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _safe_int(value: object) -> int | None:
    """Best-effort conversion to a plain int (via :func:`_safe_float`), else None."""
    result = _safe_float(value)
    return int(result) if result is not None else None


def _safe_str(value: object) -> str | None:
    """Trimmed string, or None for None / pandas NaN / empty / non-scalar."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text or None


def _to_naive_utc(ts: object) -> datetime:
    """Normalize a (possibly tz-aware) pandas Timestamp to a naive UTC datetime.

    All datetimes stored in the DB are UTC-naive by convention (blueprint
    section 2): ``datetime.utcnow()`` semantics throughout.
    """
    timestamp = pd.Timestamp(ts)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp.to_pydatetime()


def _fast_info_get(fast_info: object, *keys: str) -> object:
    """Read the first present key from a yfinance ``fast_info`` object.

    ``fast_info`` behaves like a mapping in most yfinance versions but not
    reliably across all of them; try both ``[]`` and attribute access.
    """
    for key in keys:
        value: object = None
        try:
            value = fast_info[key]  # type: ignore[index]
        except Exception:
            value = None
        if value is not None:
            return value
        value = getattr(fast_info, key, None)
        if value is not None:
            return value
    return None


def _normalize_asset_type(quote_type: object) -> str:
    """Map a yfinance ``quoteType`` to the short asset_type string (schema field)."""
    if not quote_type or not isinstance(quote_type, str):
        return "EQUITY"
    return quote_type.strip().upper()[:20] or "EQUITY"


class MarketDataService:
    """Blocking yfinance/httpx-backed market data operations.

    Every public method is synchronous; call via ``asyncio.to_thread`` from
    async code.
    """

    def __init__(self, request_timeout: float = 15.0) -> None:
        self._request_timeout = request_timeout

    # ------------------------------------------------------------------
    # Symbol search & validation
    # ------------------------------------------------------------------

    def search(self, q: str) -> list[dict]:
        """Search tickers by free-text query.

        Tries yfinance's bundled ``Search`` helper first; falls back to the
        public Yahoo Finance search endpoint via httpx if that fails or
        yields nothing (blueprint section 4, endpoint #2).
        """
        query = (q or "").strip()
        if not query:
            return []

        results = self._search_via_yfinance(query)
        if results:
            return results
        return self._search_via_httpx_fallback(query)

    def _search_via_yfinance(self, query: str) -> list[dict]:
        try:
            search_obj = yf.Search(query, max_results=10)
            quotes = getattr(search_obj, "quotes", None) or []
        except Exception:
            return []
        return self._quotes_to_results(quotes)

    def _search_via_httpx_fallback(self, query: str) -> list[dict]:
        try:
            response = httpx.get(
                _YAHOO_SEARCH_FALLBACK_URL,
                params={"q": query, "quotesCount": 10, "newsCount": 0},
                headers={"User-Agent": _DEFAULT_USER_AGENT},
                timeout=self._request_timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            return []
        quotes = data.get("quotes", []) if isinstance(data, dict) else []
        return self._quotes_to_results(quotes)

    @staticmethod
    def _quotes_to_results(quotes: list) -> list[dict]:
        results: list[dict] = []
        for item in quotes:
            if not isinstance(item, dict):
                continue
            symbol = item.get("symbol")
            if not symbol:
                continue
            name = item.get("shortname") or item.get("longname") or symbol
            results.append(
                {
                    "ticker": str(symbol).strip().upper(),
                    "name": str(name).strip(),
                    "exchange": item.get("exchange") or item.get("exchDisp"),
                    "asset_type": _normalize_asset_type(
                        item.get("quoteType") or item.get("typeDisp")
                    ),
                }
            )
        return results

    def validate_and_profile(self, ticker: str) -> dict | None:
        """Validate a ticker exists on Yahoo Finance and return its profile.

        Returns ``dict(ticker, name, exchange, currency, asset_type)`` or
        ``None`` if the ticker cannot be resolved to real market data.
        """
        normalized = (ticker or "").strip().upper()
        if not normalized:
            return None
        try:
            yf_ticker = yf.Ticker(normalized)
            try:
                info: dict = yf_ticker.info or {}
            except Exception:
                info = {}
            last_price = None
            try:
                last_price = _fast_info_get(yf_ticker.fast_info, "last_price", "lastPrice")
            except Exception:
                last_price = None

            has_price = _safe_float(last_price) is not None
            has_name = bool(info.get("shortName") or info.get("longName"))
            if not has_price and not has_name:
                return None

            name = info.get("shortName") or info.get("longName") or normalized
            exchange = info.get("exchange") or info.get("fullExchangeName")
            currency = info.get("currency") or "USD"
            asset_type = _normalize_asset_type(info.get("quoteType"))
            return {
                "ticker": normalized,
                "name": str(name).strip(),
                "exchange": str(exchange).strip() if exchange else None,
                "currency": (str(currency).strip().upper() or "USD"),
                "asset_type": asset_type,
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Price history: refresh (network -> DB) and read (DB -> DataFrame)
    # ------------------------------------------------------------------

    def refresh_prices(
        self, db: Session, symbol: Symbol, interval: str = "1d", days: int = 730
    ) -> int:
        """Fetch OHLCV history from yfinance and upsert into ``price_history``.

        Idempotent: existing rows for ``(symbol_id, ts, interval)`` are
        updated in place rather than duplicated, honouring ``uq_price_point``.
        Returns the number of rows written (inserted or updated). Any network
        failure is swallowed and yields 0 (defensive: a stale-data hiccup on
        one symbol must not crash the whole analysis pipeline).
        """
        end = datetime.utcnow()
        start = end - timedelta(days=days)
        try:
            yf_ticker = yf.Ticker(symbol.ticker)
            history = yf_ticker.history(
                start=start,
                end=end,
                interval=interval,
                auto_adjust=False,
                actions=False,
            )
        except Exception:
            return 0

        if history is None or history.empty:
            return 0

        upserted = 0
        for ts, row in history.iterrows():
            try:
                open_ = float(row["Open"])
                high = float(row["High"])
                low = float(row["Low"])
                close = float(row["Close"])
            except (KeyError, TypeError, ValueError):
                continue
            if any(math.isnan(v) or math.isinf(v) for v in (open_, high, low, close)):
                continue
            try:
                volume_raw = row.get("Volume", 0.0)
                volume = float(volume_raw) if volume_raw is not None else 0.0
                if math.isnan(volume) or math.isinf(volume):
                    volume = 0.0
            except (TypeError, ValueError):
                volume = 0.0

            ts_naive = _to_naive_utc(ts)
            existing = (
                db.query(PriceHistory)
                .filter(
                    PriceHistory.symbol_id == symbol.id,
                    PriceHistory.ts == ts_naive,
                    PriceHistory.interval == interval,
                )
                .one_or_none()
            )
            if existing is not None:
                existing.open = open_
                existing.high = high
                existing.low = low
                existing.close = close
                existing.volume = volume
            else:
                db.add(
                    PriceHistory(
                        symbol_id=symbol.id,
                        ts=ts_naive,
                        interval=interval,
                        open=open_,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                    )
                )
            upserted += 1

        db.commit()
        return upserted

    def get_history_df(
        self, db: Session, symbol_id: int, interval: str = "1d", days: int = 180
    ) -> pd.DataFrame:
        """Read locally-stored OHLCV history as a DataFrame indexed by ``ts``.

        Never touches the network — purely a DB read. Returns a (possibly
        empty) DataFrame with ``open, high, low, close, volume`` columns.
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = (
            db.query(PriceHistory)
            .filter(
                PriceHistory.symbol_id == symbol_id,
                PriceHistory.interval == interval,
                PriceHistory.ts >= cutoff,
            )
            .order_by(PriceHistory.ts.asc())
            .all()
        )
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        frame = pd.DataFrame(
            {
                "ts": [r.ts for r in rows],
                "open": [r.open for r in rows],
                "high": [r.high for r in rows],
                "low": [r.low for r in rows],
                "close": [r.close for r in rows],
                "volume": [r.volume for r in rows],
            }
        )
        return frame.set_index("ts")

    # ------------------------------------------------------------------
    # Fundamentals & quotes
    # ------------------------------------------------------------------

    def get_fundamentals(self, ticker: str) -> dict:
        """Fundamentals snapshot from yfinance ``.info`` (blueprint section 5.2).

        Every field defaults to ``None`` if missing or if the network call
        fails, so downstream agents can flag ``data_quality`` accordingly.
        """
        result: dict[str, float | None] = {name: None for name in _FUNDAMENTALS_FIELDS}
        normalized = (ticker or "").strip().upper()
        if not normalized:
            return result
        try:
            info = yf.Ticker(normalized).info or {}
        except Exception:
            return result
        for field_name, source_keys in _FUNDAMENTALS_FIELDS.items():
            for source_key in source_keys:
                value = _safe_float(info.get(source_key))
                if value is not None:
                    result[field_name] = value
                    break
        # String fields (bypass _safe_float): consumed by the macro/news agents.
        for str_field in ("sector", "industry"):
            raw = info.get(str_field)
            result[str_field] = str(raw).strip() if isinstance(raw, str) and raw.strip() else None
        return result

    def get_quote(self, ticker: str) -> dict:
        """Cheap last price + 1-day change (%) for dashboard/list views.

        Prefers ``fast_info`` (cheap); falls back to a short daily history
        pull if fast_info is unavailable or incomplete.
        """
        normalized = (ticker or "").strip().upper()
        last_price: float | None = None
        previous_close: float | None = None

        if normalized:
            try:
                fast_info = yf.Ticker(normalized).fast_info
                last_price = _safe_float(_fast_info_get(fast_info, "last_price", "lastPrice"))
                previous_close = _safe_float(
                    _fast_info_get(fast_info, "previous_close", "previousClose")
                )
            except Exception:
                last_price = None
                previous_close = None

            if last_price is None or previous_close is None:
                try:
                    history = yf.Ticker(normalized).history(
                        period="5d", interval="1d", auto_adjust=False, actions=False
                    )
                    closes = (
                        history["Close"].dropna()
                        if history is not None and not history.empty
                        else pd.Series(dtype=float)
                    )
                    if last_price is None and len(closes) >= 1:
                        last_price = _safe_float(closes.iloc[-1])
                    if previous_close is None and len(closes) >= 2:
                        previous_close = _safe_float(closes.iloc[-2])
                except Exception:
                    pass

        change_pct_1d = None
        if last_price is not None and previous_close:
            change_pct_1d = (last_price - previous_close) / previous_close * 100.0

        return {"last_price": last_price, "change_pct_1d": change_pct_1d}

    # ------------------------------------------------------------------
    # Derived summary
    # ------------------------------------------------------------------

    def price_summary(self, df: pd.DataFrame) -> dict:
        """Build the ``price_summary`` dict fed to agents (blueprint section 5.2).

        Fields: ``close``, ``change_pct_1d/5d/30d/90d``, ``high_52w``,
        ``low_52w``, ``avg_volume``. Missing/insufficient data yields ``None``
        for the affected fields rather than raising.
        """
        if df is None or df.empty or "close" not in df.columns:
            return dict(_PRICE_SUMMARY_EMPTY)

        closes = df["close"].dropna()
        if closes.empty:
            return dict(_PRICE_SUMMARY_EMPTY)

        last_close = float(closes.iloc[-1])

        def _change_pct(n_periods: int) -> float | None:
            if len(closes) <= n_periods:
                return None
            past = float(closes.iloc[-1 - n_periods])
            if not past:
                return None
            return (last_close - past) / past * 100.0

        window_52w = closes.tail(_TRADING_DAYS_52W)
        volumes = df["volume"].dropna() if "volume" in df.columns else pd.Series(dtype=float)
        avg_volume = float(volumes.tail(30).mean()) if not volumes.empty else None

        return {
            "close": last_close,
            "change_pct_1d": _change_pct(1),
            "change_pct_5d": _change_pct(5),
            "change_pct_30d": _change_pct(30),
            "change_pct_90d": _change_pct(90),
            "high_52w": float(window_52w.max()) if not window_52w.empty else None,
            "low_52w": float(window_52w.min()) if not window_52w.empty else None,
            "avg_volume": avg_volume,
        }

    # ------------------------------------------------------------------
    # Market sentiment & positioning (blueprint addendum)
    # ------------------------------------------------------------------

    def get_sentiment_snapshot(self, ticker: str) -> dict:
        """Analyst / insider / institutional / short-interest snapshot for a ticker.

        ALWAYS returns every key of :func:`_empty_sentiment` (None / empty list on
        any failure) and never raises. Each yfinance sub-read (info,
        recommendations, upgrades/downgrades, insider transactions & purchases,
        institutional holders, calendar) is wrapped in its OWN try/except so one
        missing dataset never blanks the others. Behind a 60-minute in-memory
        cache keyed by ticker; a fully-empty/failed snapshot is NOT cached, so a
        transient error self-heals on the next call.
        """
        result = _empty_sentiment()
        normalized = (ticker or "").strip().upper()
        if not normalized:
            return result

        now = datetime.utcnow()
        cached = _sentiment_cache.get(normalized)
        if cached is not None and (now - cached[1]) < _SENTIMENT_CACHE_TTL:
            return cached[0]

        try:
            yf_ticker = yf.Ticker(normalized)
        except Exception:
            return result  # construction failed; do not cache (self-heals)

        # --- analyst consensus, targets, ownership %, short interest (info) --- #
        try:
            info = yf_ticker.info or {}
            if isinstance(info, dict):
                result["recommendation_mean"] = _safe_float(info.get("recommendationMean"))
                result["recommendation_key"] = _safe_str(info.get("recommendationKey"))
                result["analyst_count"] = _safe_float(info.get("numberOfAnalystOpinions"))
                result["target_mean"] = _safe_float(info.get("targetMeanPrice"))
                result["target_high"] = _safe_float(info.get("targetHighPrice"))
                result["target_low"] = _safe_float(info.get("targetLowPrice"))
                result["insiders_pct_held"] = _safe_float(info.get("heldPercentInsiders"))
                result["institutions_pct_held"] = _safe_float(
                    info.get("heldPercentInstitutions")
                )
                result["short_percent_of_float"] = _safe_float(info.get("shortPercentOfFloat"))
                result["short_ratio"] = _safe_float(info.get("shortRatio"))
        except Exception:
            pass

        # --- ratings trend (month-over-month strongBuy/buy/hold/sell mix) --- #
        try:
            recs = yf_ticker.recommendations
            if isinstance(recs, pd.DataFrame) and not recs.empty:
                trend: list[dict] = []
                for idx, row in recs.head(4).iterrows():
                    period = _safe_str(row.get("period"))
                    trend.append(
                        {
                            "period": period if period is not None else _safe_str(idx),
                            "strong_buy": _safe_int(row.get("strongBuy")),
                            "buy": _safe_int(row.get("buy")),
                            "hold": _safe_int(row.get("hold")),
                            "sell": _safe_int(row.get("sell")),
                            "strong_sell": _safe_int(row.get("strongSell")),
                        }
                    )
                result["ratings_trend"] = trend
        except Exception:
            pass

        # --- recent rating changes (upgrades / downgrades, last 90 days) --- #
        try:
            changes_df = yf_ticker.upgrades_downgrades
            if isinstance(changes_df, pd.DataFrame) and not changes_df.empty:
                cutoff = datetime.utcnow() - timedelta(days=90)
                ordered = changes_df.sort_index(ascending=False)  # newest first
                changes: list[dict] = []
                for idx, row in ordered.iterrows():
                    try:
                        when = _to_naive_utc(idx)
                    except Exception:
                        continue
                    if when < cutoff:
                        continue
                    changes.append(
                        {
                            "date": when.strftime("%Y-%m-%d"),
                            "firm": _safe_str(row.get("Firm")),
                            "to_grade": _safe_str(row.get("ToGrade")),
                            "from_grade": _safe_str(row.get("FromGrade")),
                            "action": _safe_str(row.get("Action")),
                        }
                    )
                    if len(changes) >= 8:
                        break
                result["ratings_changes"] = changes
        except Exception:
            pass

        # --- insider purchases summary (net shares + buy/sell trans, 6m) --- #
        try:
            purchases = yf_ticker.insider_purchases
            if isinstance(purchases, pd.DataFrame) and not purchases.empty:
                label_col = purchases.columns[0]
                for _, row in purchases.iterrows():
                    label = _safe_str(row.get(label_col)) or ""
                    if label == "Purchases":
                        result["insider_buy_trans_6m"] = _safe_int(row.get("Trans"))
                    elif label == "Sales":
                        result["insider_sell_trans_6m"] = _safe_int(row.get("Trans"))
                    elif label.startswith("Net Shares Purchased"):
                        result["insider_net_shares_6m"] = _safe_float(row.get("Shares"))
        except Exception:
            pass

        # --- individual insider transactions (newest first, max 8) --- #
        try:
            txns_df = yf_ticker.insider_transactions
            if isinstance(txns_df, pd.DataFrame) and not txns_df.empty:
                ordered = (
                    txns_df.sort_values("Start Date", ascending=False)
                    if "Start Date" in txns_df.columns
                    else txns_df
                )
                txns: list[dict] = []
                for _, row in ordered.head(8).iterrows():
                    try:
                        when = _to_naive_utc(row.get("Start Date")).strftime("%Y-%m-%d")
                    except Exception:
                        when = None
                    txns.append(
                        {
                            "date": when,
                            "insider": _safe_str(row.get("Insider")),
                            "position": _safe_str(row.get("Position")),
                            "text": _safe_str(row.get("Text")),
                            "shares": _safe_float(row.get("Shares")),
                            "value": _safe_float(row.get("Value")),
                        }
                    )
                result["insider_transactions"] = txns
        except Exception:
            pass

        # --- top institutional holders (max 5) --- #
        try:
            holders_df = yf_ticker.institutional_holders
            if isinstance(holders_df, pd.DataFrame) and not holders_df.empty:
                holders: list[dict] = []
                for _, row in holders_df.head(5).iterrows():
                    holders.append(
                        {
                            "holder": _safe_str(row.get("Holder")),
                            "pct_held": _safe_float(row.get("pctHeld")),
                            "pct_change": _safe_float(row.get("pctChange")),
                        }
                    )
                result["top_institutional_holders"] = holders
        except Exception:
            pass

        # --- next earnings date / days to earnings (calendar) --- #
        try:
            calendar = yf_ticker.calendar
            earnings_date: object = None
            if isinstance(calendar, dict):
                raw = calendar.get("Earnings Date")
                if isinstance(raw, (list, tuple)) and raw:
                    earnings_date = raw[0]
                elif raw is not None:
                    earnings_date = raw
            elif isinstance(calendar, pd.DataFrame) and not calendar.empty:
                if "Earnings Date" in calendar.index:
                    earnings_date = calendar.loc["Earnings Date"].iloc[0]
            if earnings_date is not None:
                when = _to_naive_utc(earnings_date)
                result["next_earnings_date"] = when.strftime("%Y-%m-%d")
                days = (when.date() - datetime.utcnow().date()).days
                if days >= 0:
                    result["days_to_earnings"] = days
        except Exception:
            pass

        # Cache only a snapshot that actually carries data, so a transient
        # all-empty failure self-heals on the next call.
        if result != _empty_sentiment():
            _sentiment_cache[normalized] = (result, now)
        return result

    def benchmark_for(self, ticker: str) -> str:
        """Return the benchmark index ticker for ``ticker`` (longest suffix wins).

        Falls back to the S&P 500 (``^GSPC``) when no exchange suffix matches.
        """
        normalized = (ticker or "").strip().upper()
        best_suffix: str | None = None
        for suffix in _BENCHMARK_SUFFIX_MAP:
            if normalized.endswith(suffix) and (
                best_suffix is None or len(suffix) > len(best_suffix)
            ):
                best_suffix = suffix
        return _BENCHMARK_SUFFIX_MAP[best_suffix] if best_suffix is not None else _DEFAULT_BENCHMARK

    def _benchmark_changes(self, benchmark: str) -> tuple[float | None, float | None]:
        """(30d, 90d) % change for a benchmark index, cached per-benchmark (6h TTL).

        Uses the SAME trading-session-lookback arithmetic as :meth:`price_summary`
        (``iloc[-1 - n]`` over the close series). A fully-failed fetch returns
        ``(None, None)`` and is NOT cached, so a transient error self-heals.
        """
        now = datetime.utcnow()
        cached = _benchmark_cache.get(benchmark)
        if cached is not None and (now - cached[1]) < _BENCHMARK_CACHE_TTL:
            return cached[0]

        try:
            history = yf.Ticker(benchmark).history(
                period="6mo", interval="1d", auto_adjust=False, actions=False
            )
        except Exception:
            return (None, None)  # do not cache (self-heals)

        changes: tuple[float | None, float | None] = (None, None)
        try:
            if history is not None and not history.empty and "Close" in history.columns:
                closes = history["Close"].dropna()
                if not closes.empty:
                    last_close = float(closes.iloc[-1])

                    def _change_pct(n_periods: int) -> float | None:
                        if len(closes) <= n_periods:
                            return None
                        past = float(closes.iloc[-1 - n_periods])
                        if not past:
                            return None
                        return (last_close - past) / past * 100.0

                    changes = (_change_pct(30), _change_pct(90))
        except Exception:
            changes = (None, None)

        if changes != (None, None):
            _benchmark_cache[benchmark] = (changes, now)
        return changes

    def get_relative_performance(
        self,
        ticker: str,
        stock_change_30d_pct: float | None,
        stock_change_90d_pct: float | None,
    ) -> dict:
        """Relative strength of a stock vs its exchange benchmark over 30/90 days.

        ``relative_*`` is the percentage-point spread (stock minus benchmark),
        computed only when both sides are non-None. Never raises.
        """
        benchmark = self.benchmark_for(ticker)
        bench_30d, bench_90d = self._benchmark_changes(benchmark)

        relative_30d = (
            stock_change_30d_pct - bench_30d
            if stock_change_30d_pct is not None and bench_30d is not None
            else None
        )
        relative_90d = (
            stock_change_90d_pct - bench_90d
            if stock_change_90d_pct is not None and bench_90d is not None
            else None
        )
        return {
            "benchmark": benchmark,
            "stock_change_30d_pct": stock_change_30d_pct,
            "benchmark_change_30d_pct": bench_30d,
            "relative_30d_pct": relative_30d,
            "stock_change_90d_pct": stock_change_90d_pct,
            "benchmark_change_90d_pct": bench_90d,
            "relative_90d_pct": relative_90d,
        }


# Shared instance for modules that just need a ready-to-use service without
# managing their own lifecycle (the class itself is stateless besides the
# httpx timeout, so constructing a private instance elsewhere is equally
# valid).
market_data_service = MarketDataService()
