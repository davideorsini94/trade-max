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


# Shared instance for modules that just need a ready-to-use service without
# managing their own lifecycle (the class itself is stateless besides the
# httpx timeout, so constructing a private instance elsewhere is equally
# valid).
market_data_service = MarketDataService()
