"""Pure pandas technical-indicator functions (blueprint section 5.6 / 10-A).

Every function here is a pure transformation over a :class:`pandas.Series` or
:class:`pandas.DataFrame` — no I/O, no network, no side effects — so they are
fully covered by deterministic synthetic-data unit tests (``tests/test_indicators.py``).

``compute_all`` is the single entry point used by the rest of the app (agents,
API routers): it takes an OHLCV DataFrame indexed by timestamp (as returned by
``app.data.market.MarketDataService.get_history_df``) and returns a dict whose
series keys line up 1:1 with the ``IndicatorSeries`` Pydantic schema fields,
plus a ``"latest"`` dict of scalar last values (including the two
scalar-only metrics ``volatility_30d_pct`` and ``drawdown_90d_pct``).
"""

from __future__ import annotations

import pandas as pd

# Trading days per calendar year, used to annualize daily-return volatility.
TRADING_DAYS_PER_YEAR = 252

# Series field names, in the exact order/spelling of the ``IndicatorSeries``
# Pydantic schema (blueprint section 3). ``compute_all`` returns exactly these
# keys as aligned lists, plus a "latest" dict (see module docstring).
INDICATOR_SERIES_FIELDS: tuple[str, ...] = (
    "sma20",
    "sma50",
    "sma200",
    "ema12",
    "ema26",
    "rsi14",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "atr14",
)


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average. NaN (warm-up) for the first ``window - 1`` rows."""
    if window <= 0:
        raise ValueError("window must be a positive integer")
    return series.astype(float).rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average (recursive form, ``adjust=False``).

    NaN for rows before ``span`` non-null observations have been seen, matching
    the "null during warm-up" contract used across all indicator series.
    """
    if span <= 0:
        raise ValueError("span must be a positive integer")
    return series.astype(float).ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder smoothing), bounded to ``[0, 100]``.

    Edge cases are handled explicitly so the result is never out of bounds or
    a raw division-by-zero artifact:
    - no losses at all in the window and some gains -> RSI = 100 (max strength).
    - no losses and no gains (perfectly flat prices) -> RSI = 50 (neutral).
    """
    if window <= 0:
        raise ValueError("window must be a positive integer")
    delta = series.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    result = 100.0 - (100.0 / (1.0 + rs))
    result = result.astype(float)

    no_loss = avg_loss == 0.0
    neutral_or_max = pd.Series(
        [100.0 if g > 0.0 else 50.0 for g in avg_gain], index=avg_gain.index, dtype=float
    )
    result = result.where(~no_loss, neutral_or_max)
    return result.clip(lower=0.0, upper=100.0)


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line and histogram. Returns ``(macd, macd_signal, macd_hist)``."""
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger(
    series: pd.Series, window: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands. Returns ``(bb_upper, bb_mid, bb_lower)``.

    ``bb_upper >= bb_mid >= bb_lower`` always holds wherever defined, since the
    rolling standard deviation used for the offset is never negative.
    """
    mid = sma(series, window)
    std = series.astype(float).rolling(window=window, min_periods=window).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing) from an OHLC DataFrame.

    Requires ``high``, ``low`` and ``close`` columns. Always positive wherever
    defined (true range is a max of non-negative absolute differences over
    price data with ``high >= low``).
    """
    if window <= 0:
        raise ValueError("window must be a positive integer")
    required = {"high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"df is missing required columns for atr(): {sorted(missing)}")

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    true_range = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1, skipna=True)

    return true_range.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()


def volatility(series: pd.Series, window: int = 30) -> pd.Series:
    """Annualized volatility (%) from the rolling stdev of daily returns.

    ``latest`` value of this series is exposed as ``volatility_30d_pct``.
    """
    if window <= 0:
        raise ValueError("window must be a positive integer")
    daily_returns = series.astype(float).pct_change()
    rolling_std = daily_returns.rolling(window=window, min_periods=window).std()
    return rolling_std * (TRADING_DAYS_PER_YEAR**0.5) * 100.0


def drawdown(series: pd.Series, window: int = 90) -> pd.Series:
    """Drawdown (%) from the trailing ``window``-day rolling maximum close.

    Expressed as a non-negative magnitude (0 = at/above the recent high, larger
    = deeper decline), matching the ``MAX_DRAWDOWN_90D_BUY`` policy threshold
    convention (blueprint section 6). ``latest`` value is ``drawdown_90d_pct``.
    """
    if window <= 0:
        raise ValueError("window must be a positive integer")
    closes = series.astype(float)
    rolling_max = closes.rolling(window=window, min_periods=1).max()
    pct = (rolling_max - closes) / rolling_max.replace(0.0, float("nan")) * 100.0
    return pct.astype(float).clip(lower=0.0)


def _series_to_list(series: pd.Series) -> list[float | None]:
    """Convert a float Series to a JSON-friendly list, NaN -> None."""
    return [None if pd.isna(value) else float(value) for value in series.to_numpy(dtype=float)]


def _last_scalar(series: pd.Series) -> float | None:
    """Last value of a Series as a plain float, or None if empty/NaN."""
    if series.empty:
        return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def compute_all(df: pd.DataFrame) -> dict:
    """Compute every indicator over an OHLCV DataFrame.

    ``df`` must have ``open``, ``high``, ``low``, ``close``, ``volume`` columns
    (as returned by ``MarketDataService.get_history_df``), indexed by
    timestamp in ascending order.

    Returns a dict with:
    - one key per ``INDICATOR_SERIES_FIELDS`` entry, each a list of
      ``len(df)`` floats/None aligned to ``df``'s row order (None during
      warm-up), matching the ``IndicatorSeries`` schema fields.
    - ``"latest"``: dict of scalar last values for all of the above plus
      ``volatility_30d_pct`` and ``drawdown_90d_pct``.
    """
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"df is missing required columns for compute_all(): {sorted(missing)}")

    if len(df) == 0:
        empty_latest: dict[str, float | None] = {name: None for name in INDICATOR_SERIES_FIELDS}
        empty_latest["volatility_30d_pct"] = None
        empty_latest["drawdown_90d_pct"] = None
        result: dict = {name: [] for name in INDICATOR_SERIES_FIELDS}
        result["latest"] = empty_latest
        return result

    close = df["close"]

    macd_line, macd_signal_series, macd_hist_series = macd(close)
    bb_upper_series, bb_mid_series, bb_lower_series = bollinger(close)

    series_map: dict[str, pd.Series] = {
        "sma20": sma(close, 20),
        "sma50": sma(close, 50),
        "sma200": sma(close, 200),
        "ema12": ema(close, 12),
        "ema26": ema(close, 26),
        "rsi14": rsi(close, 14),
        "macd": macd_line,
        "macd_signal": macd_signal_series,
        "macd_hist": macd_hist_series,
        "bb_upper": bb_upper_series,
        "bb_mid": bb_mid_series,
        "bb_lower": bb_lower_series,
        "atr14": atr(df, 14),
    }

    volatility_series = volatility(close, 30)
    drawdown_series = drawdown(close, 90)

    result = {name: _series_to_list(series) for name, series in series_map.items()}

    latest = {name: _last_scalar(series) for name, series in series_map.items()}
    latest["volatility_30d_pct"] = _last_scalar(volatility_series)
    latest["drawdown_90d_pct"] = _last_scalar(drawdown_series)

    result["latest"] = latest
    return result
