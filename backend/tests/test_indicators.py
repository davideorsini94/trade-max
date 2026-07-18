"""Deterministic, network-free tests for app.data.indicators (blueprint section 10-A).

All input data is synthetically generated with a fixed closed-form formula
(no randomness, no network, no DB) so every assertion is fully reproducible.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.data.indicators import (
    INDICATOR_SERIES_FIELDS,
    atr,
    bollinger,
    compute_all,
    ema,
    macd,
    rsi,
    sma,
)


def _synthetic_close(n: int) -> pd.Series:
    """Deterministic close-price series: upward drift + smooth oscillation.

    No randomness, so every test run produces byte-identical results.
    """
    idx = pd.date_range("2023-01-01", periods=n, freq="D")
    trend = np.linspace(100.0, 160.0, n)
    wave = 6.0 * np.sin(np.arange(n) * (2 * math.pi / 20.0))
    close = trend + wave
    return pd.Series(close, index=idx, name="close")


def _synthetic_df(n: int = 300) -> pd.DataFrame:
    """Deterministic OHLCV frame built around ``_synthetic_close`` (high > low always)."""
    close = _synthetic_close(n)
    spread = 0.5 + 0.1 * np.abs(np.sin(np.arange(n) * 0.3))
    high = close + spread
    low = close - spread
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = 1_000_000 + 10_000 * (np.arange(n) % 7)
    return pd.DataFrame(
        {
            "open": open_.to_numpy(),
            "high": high.to_numpy(),
            "low": low.to_numpy(),
            "close": close.to_numpy(),
            "volume": volume,
        },
        index=close.index,
    )


# --- sma correctness ---------------------------------------------------


def test_sma_matches_manual_rolling_mean() -> None:
    close = _synthetic_close(60)
    window = 10
    result = sma(close, window)

    # Warm-up: first window-1 entries must be null.
    assert result.iloc[: window - 1].isna().all()

    for i in (window - 1, window + 5, 40, 59):
        expected = close.iloc[i - window + 1 : i + 1].mean()
        assert result.iloc[i] == pytest.approx(expected)


def test_sma_window_must_be_positive() -> None:
    with pytest.raises(ValueError):
        sma(_synthetic_close(10), 0)


# --- rsi bounds ----------------------------------------------------------


def test_rsi_stays_within_bounds() -> None:
    close = _synthetic_close(120)
    result = rsi(close, 14)
    valid = result.dropna()

    assert not valid.empty
    assert (valid >= 0.0).all()
    assert (valid <= 100.0).all()


def test_rsi_monotonic_uptrend_is_strong_but_bounded() -> None:
    # Strictly increasing prices -> no losses at all -> RSI should sit at the
    # bounded maximum (100), never overflow or divide-by-zero to NaN/inf.
    close = pd.Series(np.linspace(100.0, 200.0, 60))
    result = rsi(close, 14)
    valid = result.dropna()

    assert not valid.empty
    assert (valid <= 100.0).all()
    assert (valid >= 0.0).all()
    assert valid.iloc[-1] == pytest.approx(100.0)


# --- macd relation ---------------------------------------------------------


def test_macd_line_equals_ema_difference() -> None:
    close = _synthetic_close(120)
    macd_line, signal_line, hist = macd(close, fast=12, slow=26, signal=9)

    expected_macd = ema(close, 12) - ema(close, 26)
    pd.testing.assert_series_equal(macd_line, expected_macd, check_names=False)

    expected_hist = macd_line - signal_line
    valid_idx = hist.dropna().index
    assert not valid_idx.empty
    pd.testing.assert_series_equal(
        hist.loc[valid_idx], expected_hist.loc[valid_idx], check_names=False
    )


# --- bollinger ordering ----------------------------------------------------


def test_bollinger_bands_are_ordered() -> None:
    close = _synthetic_close(80)
    upper, mid, lower = bollinger(close, window=20, num_std=2.0)

    combined = pd.concat([upper, mid, lower], axis=1, keys=["upper", "mid", "lower"]).dropna()
    assert not combined.empty
    assert (combined["upper"] >= combined["mid"]).all()
    assert (combined["mid"] >= combined["lower"]).all()


# --- atr positivity ---------------------------------------------------------


def test_atr_is_positive() -> None:
    df = _synthetic_df(60)
    result = atr(df, window=14)
    valid = result.dropna()

    assert not valid.empty
    assert (valid > 0.0).all()


def test_atr_requires_ohlc_columns() -> None:
    with pytest.raises(ValueError):
        atr(pd.DataFrame({"close": [1.0, 2.0, 3.0]}), window=14)


# --- compute_all shape -------------------------------------------------------


def test_compute_all_shape_and_latest() -> None:
    df = _synthetic_df(300)
    result = compute_all(df)

    for field in INDICATOR_SERIES_FIELDS:
        assert field in result
        assert isinstance(result[field], list)
        assert len(result[field]) == len(df)
        assert all(v is None or isinstance(v, float) for v in result[field])

    assert "latest" in result
    latest = result["latest"]
    expected_latest_keys = set(INDICATOR_SERIES_FIELDS) | {
        "volatility_30d_pct",
        "drawdown_90d_pct",
    }
    assert set(latest.keys()) == expected_latest_keys

    for value in latest.values():
        assert value is None or isinstance(value, float)

    # With 300 days of daily data every indicator (incl. the 200d SMA) should
    # be warmed up by the last row.
    for field in INDICATOR_SERIES_FIELDS:
        assert latest[field] is not None
    assert latest["volatility_30d_pct"] is not None
    assert latest["drawdown_90d_pct"] is not None
    assert 0.0 <= latest["rsi14"] <= 100.0
    assert latest["drawdown_90d_pct"] >= 0.0


def test_compute_all_empty_dataframe() -> None:
    empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    result = compute_all(empty_df)

    for field in INDICATOR_SERIES_FIELDS:
        assert result[field] == []

    expected_latest = {field: None for field in INDICATOR_SERIES_FIELDS}
    expected_latest["volatility_30d_pct"] = None
    expected_latest["drawdown_90d_pct"] = None
    assert result["latest"] == expected_latest


def test_compute_all_missing_columns_raises() -> None:
    bad_df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError):
        compute_all(bad_df)
