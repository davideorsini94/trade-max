"""Offline tests for per-exchange market hours (blueprint §7.3 addendum).

Pure functions over an explicit ``now``: no DB, no network, no clock
dependency. Every case passes a timezone-AWARE datetime so the assertions do
not depend on the machine's local timezone.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.scheduler import (
    _DEFAULT_SESSION,
    _EXCHANGE_SESSIONS,
    is_market_open,
    is_symbol_market_open,
    session_for_ticker,
)

_ROME = ZoneInfo("Europe/Rome")

# Monday 2026-07-27; Saturday 2026-07-25.
_MONDAY_10_00_ROME = datetime(2026, 7, 27, 10, 0, tzinfo=_ROME)
_MONDAY_15_14_ROME = datetime(2026, 7, 27, 15, 14, tzinfo=_ROME)
_MONDAY_16_30_ROME = datetime(2026, 7, 27, 16, 30, tzinfo=_ROME)
_MONDAY_21_00_ROME = datetime(2026, 7, 27, 21, 0, tzinfo=_ROME)
_SATURDAY_11_00_ROME = datetime(2026, 7, 25, 11, 0, tzinfo=_ROME)


# --------------------------------------------------------------------------- #
# session_for_ticker
# --------------------------------------------------------------------------- #


def test_bare_ticker_falls_back_to_the_us_session():
    assert session_for_ticker("AAPL") is _DEFAULT_SESSION
    assert session_for_ticker("BRK-B") is _DEFAULT_SESSION
    assert session_for_ticker("") is _DEFAULT_SESSION


@pytest.mark.parametrize(
    ("ticker", "tz_name"),
    [
        ("ENI.MI", "Europe/Rome"),
        ("AIR.PA", "Europe/Paris"),
        ("SHEL.L", "Europe/London"),
        ("Z260.MU", "Europe/Berlin"),
        ("0P0001P5FD.F", "Europe/Berlin"),
        ("ABC.TO", "America/Toronto"),
    ],
)
def test_suffix_maps_to_its_own_exchange_timezone(ticker: str, tz_name: str):
    assert str(session_for_ticker(ticker).tz) == tz_name


def test_longest_suffix_wins_so_lisbon_is_not_read_as_london():
    """``.LS`` (Lisbon) must beat ``.L`` (London) — the reason for longest-match."""
    assert str(session_for_ticker("XYZ.LS").tz) == "Europe/Lisbon"
    assert str(session_for_ticker("XYZ.L").tz) == "Europe/London"


def test_suffix_lookup_is_case_insensitive():
    assert session_for_ticker("eni.mi") is _EXCHANGE_SESSIONS[".MI"]


# --------------------------------------------------------------------------- #
# is_symbol_market_open — the actual regression this fixes
# --------------------------------------------------------------------------- #


def test_european_listing_is_open_during_the_european_morning():
    """The old single US window (15:30-22:00 Rome) reported this as CLOSED,
    so European titles were never refreshed or analysed before 15:30."""
    assert is_symbol_market_open("ENI.MI", _MONDAY_10_00_ROME) is True
    assert is_symbol_market_open("AIR.PA", _MONDAY_10_00_ROME) is True
    assert is_symbol_market_open("SHEL.L", _MONDAY_10_00_ROME) is True


def test_us_listing_is_closed_during_the_european_morning():
    assert is_symbol_market_open("AAPL", _MONDAY_10_00_ROME) is False


def test_us_listing_is_closed_just_before_its_open_and_open_after():
    # 15:14 Rome is 09:14 New York — 16 minutes before the bell.
    assert is_symbol_market_open("AAPL", _MONDAY_15_14_ROME) is False
    assert is_symbol_market_open("AAPL", _MONDAY_16_30_ROME) is True


def test_european_listing_is_closed_in_the_late_evening_while_us_still_trades():
    assert is_symbol_market_open("ENI.MI", _MONDAY_21_00_ROME) is False
    assert is_symbol_market_open("AAPL", _MONDAY_21_00_ROME) is True


def test_weekends_are_closed_everywhere():
    for ticker in ("AAPL", "ENI.MI", "SHEL.L", "ABC.TO"):
        assert is_symbol_market_open(ticker, _SATURDAY_11_00_ROME) is False


def test_london_opens_an_hour_before_the_continent():
    # 08:30 Rome == 07:30 London: before the LSE open, after nothing else.
    at_08_30_rome = datetime(2026, 7, 27, 8, 30, tzinfo=_ROME)
    assert is_symbol_market_open("SHEL.L", at_08_30_rome) is False
    # 09:30 Rome == 08:30 London: LSE open, Milan open too.
    at_09_30_rome = datetime(2026, 7, 27, 9, 30, tzinfo=_ROME)
    assert is_symbol_market_open("SHEL.L", at_09_30_rome) is True
    assert is_symbol_market_open("ENI.MI", at_09_30_rome) is True


# --------------------------------------------------------------------------- #
# is_market_open — the "any exchange I follow" badge
# --------------------------------------------------------------------------- #


def test_badge_is_open_when_any_followed_exchange_is_open():
    assert is_market_open(_MONDAY_10_00_ROME, ["AAPL", "ENI.MI"]) is True


def test_badge_is_closed_when_every_followed_exchange_is_closed():
    assert is_market_open(_MONDAY_10_00_ROME, ["AAPL", "ABC.TO"]) is False
    assert is_market_open(_SATURDAY_11_00_ROME, ["AAPL", "ENI.MI"]) is False


def test_badge_with_no_tickers_is_closed():
    assert is_market_open(_MONDAY_10_00_ROME, []) is False


def test_badge_fails_closed_when_the_symbol_list_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``tickers`` argument means a DB read; a failure must not crash a job."""
    import app.scheduler as scheduler_module

    def _boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(scheduler_module, "_active_symbols", _boom)
    assert is_market_open(_MONDAY_10_00_ROME) is False


# --------------------------------------------------------------------------- #
# Per-symbol gating inside the scheduled jobs (the behavioural change)
# --------------------------------------------------------------------------- #


async def test_price_refresh_skips_only_the_symbols_whose_exchange_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One mixed batch, one partial refresh: EU refreshed, US skipped — instead of
    the old all-or-nothing global gate that skipped the whole batch."""
    import app.scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module,
        "_active_symbols",
        lambda favorites=None: [(1, "AAPL"), (2, "ENI.MI"), (3, "SHEL.L")],
    )
    # Freeze "now" inside the European morning for the gate the job actually calls.
    monkeypatch.setattr(
        scheduler_module,
        "is_symbol_market_open",
        lambda ticker, now=None: is_symbol_market_open(ticker, _MONDAY_10_00_ROME),
    )
    refreshed: list[int] = []
    monkeypatch.setattr(
        scheduler_module,
        "_refresh_symbol_prices",
        lambda symbol_id, interval, days: refreshed.append(symbol_id) or 0,
    )

    await scheduler_module._refresh_prices_for(
        True, "1h", 30, "test_job", only_when_open=True
    )

    assert refreshed == [2, 3]  # ENI.MI and SHEL.L only; AAPL's market is shut


async def test_price_refresh_without_the_gate_touches_every_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-of-day job must keep refreshing everything, market open or not."""
    import app.scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module,
        "_active_symbols",
        lambda favorites=None: [(1, "AAPL"), (2, "ENI.MI")],
    )
    refreshed: list[int] = []
    monkeypatch.setattr(
        scheduler_module,
        "_refresh_symbol_prices",
        lambda symbol_id, interval, days: refreshed.append(symbol_id) or 0,
    )

    await scheduler_module._refresh_prices_for(None, "1d", 730, "test_eod")

    assert refreshed == [1, 2]


async def test_scheduled_analysis_only_runs_symbols_whose_exchange_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module,
        "_symbols_due_for_analysis",
        lambda favorites, interval_hours: [(1, "AAPL"), (2, "ENI.MI")],
    )
    monkeypatch.setattr(
        scheduler_module,
        "is_symbol_market_open",
        lambda ticker, now=None: is_symbol_market_open(ticker, _MONDAY_10_00_ROME),
    )
    monkeypatch.setattr(scheduler_module, "_INTER_SYMBOL_PAUSE_S", 0.0)

    analysed: list[int] = []

    async def _fake_run_analysis(symbol_id: int, trigger: str = "SCHEDULED") -> int:
        analysed.append(symbol_id)
        return symbol_id

    # The job imports run_analysis lazily from the engine package.
    import app.engine.orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "run_analysis", _fake_run_analysis)

    await scheduler_module._run_scheduled_analyses(
        True, 4, "test_analysis", only_when_open=True
    )

    assert analysed == [2]  # ENI.MI only: no LLM call burned on a shut US market
