"""Offline tests for the curated universe ("Universo titoli").

Covers the pure ranking functions (``compute_reliability`` / ``compute_composite``)
and the ``GET /api/universe`` router: pagination math, every sort key with
NULLs-last, the ``q`` filter, the monitored/favorite mapping onto ``Symbol`` and
the empty-table bootstrap (static seed, no network).

No test touches the network: universe rows are inserted directly, and the
background refresh "kick" is monkeypatched to a no-op for the whole module.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.data.universe import UNIVERSE, compute_composite, compute_reliability
from app.models import Symbol, UniverseStat


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_background_refresh(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the background refresh kick with a no-op (records call count).

    Guarantees every test stays fully offline even on the empty-table bootstrap
    path, which is the only path that would otherwise schedule a refresh.
    """
    calls = {"count": 0}

    def _noop() -> None:
        calls["count"] += 1

    monkeypatch.setattr("app.api.universe._kick_background_refresh", _noop)
    return calls


def _mk(ticker: str, **overrides: object) -> UniverseStat:
    """Build a ``UniverseStat`` with sensible defaults, overriding as needed."""
    defaults: dict = {
        "name": f"{ticker} Corp",
        "exchange": "NYSE",
        "country": "US",
        "sector": "Technology",
        "currency": "USD",
        "fame_rank": 3,
        "market_cap_bn": 100.0,
        "last_price": None,
        "change_pct_1d": None,
        "change_pct_30d": None,
        "volatility_30d_pct": None,
        "above_sma200": None,
        "reliability_score": None,
        "composite_score": None,
        "updated_at": None,
    }
    defaults.update(overrides)
    return UniverseStat(ticker=ticker, **defaults)


def _seed(factory: sessionmaker[Session], stats: list[UniverseStat]) -> None:
    with factory() as db:
        db.add_all(stats)
        db.commit()


# Five rows chosen so every sortable column has a NULL somewhere:
#   composite_score NULL -> DDD ; change_pct_30d NULL -> CCC ;
#   market_cap_bn NULL -> DDD  ; reliability_score NULL -> CCC, EEE.
def _sortable_rows() -> list[UniverseStat]:
    return [
        _mk("AAA", fame_rank=5, composite_score=90.0, change_pct_30d=15.0, market_cap_bn=800.0, reliability_score=85.0),
        _mk("BBB", fame_rank=4, composite_score=60.0, change_pct_30d=-10.0, market_cap_bn=200.0, reliability_score=50.0),
        _mk("CCC", fame_rank=3, composite_score=75.0, change_pct_30d=None, market_cap_bn=500.0, reliability_score=None),
        _mk("DDD", fame_rank=2, composite_score=None, change_pct_30d=5.0, market_cap_bn=None, reliability_score=30.0),
        _mk("EEE", fame_rank=1, composite_score=40.0, change_pct_30d=25.0, market_cap_bn=50.0, reliability_score=None),
    ]


def _tickers(client: TestClient, **params: object) -> list[str]:
    resp = client.get("/api/universe", params=params)
    assert resp.status_code == 200, resp.text
    return [item["ticker"] for item in resp.json()["items"]]


# --------------------------------------------------------------------------- #
# Pure ranking functions
# --------------------------------------------------------------------------- #


def test_compute_reliability_all_inputs_missing_is_none() -> None:
    assert compute_reliability(None, None, None) is None


def test_compute_reliability_known_value() -> None:
    # stability: (100-20)*0.5 = 40 ; +20 (above SMA200) ; size: 500/1000*30 = 15.
    assert compute_reliability(20.0, True, 500.0) == pytest.approx(75.0)


def test_compute_reliability_partial_inputs_not_none() -> None:
    # Only the above-SMA200 flag present -> 20.0 (not None).
    assert compute_reliability(None, True, None) == pytest.approx(20.0)


def test_compute_reliability_bounds() -> None:
    # Best case saturates at 100; worst finite case stays >= 0.
    assert compute_reliability(0.0, True, 1000.0) == pytest.approx(100.0)
    high_vol = compute_reliability(500.0, False, 5000.0)
    assert high_vol is not None
    assert 0.0 <= high_vol <= 100.0
    assert high_vol == pytest.approx(30.0)  # size-only, cap clamped to 1000.


def test_compute_reliability_deterministic() -> None:
    assert compute_reliability(18.0, True, 320.0) == compute_reliability(18.0, True, 320.0)


def test_compute_composite_known_value() -> None:
    # fame 100*.35=35 ; trend 0.75*100*.25=18.75 ; value 100*.20=20 ; rel 80*.20=16.
    assert compute_composite(5, 10.0, 1000.0, 80.0) == pytest.approx(89.75)


def test_compute_composite_missing_pieces_contribute_zero() -> None:
    # Only fame present (rank 3 -> 60 * 0.35 = 21).
    assert compute_composite(3, None, None, None) == pytest.approx(21.0)


def test_compute_composite_bounds() -> None:
    assert compute_composite(5, 1000.0, 100000.0, 100.0) == pytest.approx(100.0)
    low = compute_composite(1, -1000.0, 0.0, 0.0)
    assert 0.0 <= low <= 100.0
    assert low == pytest.approx(7.0)  # fame 1 -> 20 * 0.35.


def test_compute_composite_always_float() -> None:
    value = compute_composite(None, None, None, None)
    assert isinstance(value, float)
    assert value == pytest.approx(0.0)


def test_compute_composite_deterministic() -> None:
    assert compute_composite(4, 3.0, 250.0, 55.0) == compute_composite(4, 3.0, 250.0, 55.0)


# --------------------------------------------------------------------------- #
# GET /api/universe — pagination
# --------------------------------------------------------------------------- #


def test_pagination_math(client: TestClient, db_session_factory: sessionmaker[Session]) -> None:
    # 12 rows with strictly decreasing composite so ordering is deterministic.
    rows = [_mk(f"T{i:02d}", composite_score=float(100 - i)) for i in range(12)]
    _seed(db_session_factory, rows)

    page1 = client.get("/api/universe", params={"page_size": 5, "page": 1}).json()
    assert page1["total"] == 12
    assert page1["page"] == 1
    assert page1["page_size"] == 5
    assert len(page1["items"]) == 5
    assert page1["items"][0]["ticker"] == "T00"  # highest composite first.

    page3 = client.get("/api/universe", params={"page_size": 5, "page": 3}).json()
    assert page3["total"] == 12
    assert page3["page"] == 3
    assert len(page3["items"]) == 2  # 12 = 5 + 5 + 2.


def test_page_size_is_clamped(client: TestClient, db_session_factory: sessionmaker[Session]) -> None:
    _seed(db_session_factory, _sortable_rows())

    too_big = client.get("/api/universe", params={"page_size": 1000}).json()
    assert too_big["page_size"] == 100

    too_small = client.get("/api/universe", params={"page_size": 1}).json()
    assert too_small["page_size"] == 5


# --------------------------------------------------------------------------- #
# GET /api/universe — sorting (NULLs always last)
# --------------------------------------------------------------------------- #


def test_sort_composite_desc_default(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    _seed(db_session_factory, _sortable_rows())
    # composite: A=90, C=75, B=60, E=40, D=None -> NULL last.
    assert _tickers(client) == ["AAA", "CCC", "BBB", "EEE", "DDD"]


def test_sort_composite_asc_nulls_still_last(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    _seed(db_session_factory, _sortable_rows())
    # asc: E=40, B=60, C=75, A=90, then D=None LAST (nulls last regardless of order).
    assert _tickers(client, sort="composite", order="asc") == ["EEE", "BBB", "CCC", "AAA", "DDD"]


def test_sort_fame_desc_with_composite_tiebreak(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    _seed(db_session_factory, _sortable_rows())
    # fame_rank distinct 5..1.
    assert _tickers(client, sort="fame") == ["AAA", "BBB", "CCC", "DDD", "EEE"]


def test_sort_fame_tiebreak_uses_composite(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    # Two rows share fame_rank=5: higher composite must come first.
    rows = [
        _mk("LO", fame_rank=5, composite_score=10.0),
        _mk("HI", fame_rank=5, composite_score=90.0),
        _mk("MID", fame_rank=3, composite_score=99.0),
    ]
    _seed(db_session_factory, rows)
    assert _tickers(client, sort="fame") == ["HI", "LO", "MID"]


def test_sort_trend_desc_nulls_last(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    _seed(db_session_factory, _sortable_rows())
    # change_30d: E=25, A=15, D=5, B=-10, C=None -> last.
    assert _tickers(client, sort="trend") == ["EEE", "AAA", "DDD", "BBB", "CCC"]


def test_sort_value_desc_nulls_last(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    _seed(db_session_factory, _sortable_rows())
    # market_cap_bn: A=800, C=500, B=200, E=50, D=None -> last.
    assert _tickers(client, sort="value") == ["AAA", "CCC", "BBB", "EEE", "DDD"]


def test_sort_reliability_desc_nulls_last(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    _seed(db_session_factory, _sortable_rows())
    # reliability: A=85, B=50, D=30, then C & E None -> last (ticker asc tiebreak).
    assert _tickers(client, sort="reliability") == ["AAA", "BBB", "DDD", "CCC", "EEE"]


# --------------------------------------------------------------------------- #
# GET /api/universe — q filter
# --------------------------------------------------------------------------- #


def test_q_filter_matches_ticker_or_name_case_insensitive(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    _seed(
        db_session_factory,
        [
            _mk("AAPL", name="Apple", composite_score=90.0),
            _mk("MSFT", name="Microsoft", composite_score=80.0),
            _mk("ENI.MI", name="Eni", composite_score=70.0),
        ],
    )

    # Matches ticker (case-insensitive).
    by_ticker = client.get("/api/universe", params={"q": "aapl"}).json()
    assert by_ticker["total"] == 1
    assert by_ticker["items"][0]["ticker"] == "AAPL"

    # Matches name substring (case-insensitive).
    by_name = client.get("/api/universe", params={"q": "micro"}).json()
    assert by_name["total"] == 1
    assert by_name["items"][0]["ticker"] == "MSFT"

    # No match -> empty page, total 0.
    none = client.get("/api/universe", params={"q": "zzzz"}).json()
    assert none["total"] == 0
    assert none["items"] == []


# --------------------------------------------------------------------------- #
# GET /api/universe — monitored / favorite mapping
# --------------------------------------------------------------------------- #


def test_monitored_and_favorite_mapping(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    _seed(
        db_session_factory,
        [
            _mk("AAPL", name="Apple", composite_score=90.0),
            _mk("MSFT", name="Microsoft", composite_score=80.0),
            _mk("TSLA", name="Tesla", composite_score=70.0),
        ],
    )
    # A monitored favorite (AAPL) and a monitored non-favorite (MSFT).
    with db_session_factory() as db:
        db.add(Symbol(ticker="AAPL", name="Apple", is_favorite=True))
        db.add(Symbol(ticker="MSFT", name="Microsoft", is_favorite=False))
        db.commit()

    items = {item["ticker"]: item for item in client.get("/api/universe").json()["items"]}

    assert items["AAPL"]["monitored"] is True
    assert items["AAPL"]["is_favorite"] is True
    assert items["AAPL"]["symbol_id"] is not None

    assert items["MSFT"]["monitored"] is True
    assert items["MSFT"]["is_favorite"] is False
    assert items["MSFT"]["symbol_id"] is not None

    assert items["TSLA"]["monitored"] is False
    assert items["TSLA"]["is_favorite"] is False
    assert items["TSLA"]["symbol_id"] is None


# --------------------------------------------------------------------------- #
# GET /api/universe — empty-table bootstrap (static seed, no network)
# --------------------------------------------------------------------------- #


def test_empty_table_seeds_static_metadata_without_network(
    client: TestClient, _no_background_refresh: dict
) -> None:
    resp = client.get("/api/universe", params={"page_size": 100})
    assert resp.status_code == 200
    body = resp.json()

    # The whole curated universe was seeded synchronously.
    assert body["total"] == len(UNIVERSE)
    # Background refresh was kicked (but is a no-op here -> no network).
    assert _no_background_refresh["count"] >= 1

    # Static metadata is present; market data is not (prices None until refresh).
    aapl = next(item for item in body["items"] if item["ticker"] == "AAPL")
    assert aapl["name"] == "Apple"
    assert aapl["fame_rank"] == 5
    assert aapl["market_cap_bn"] is not None
    assert aapl["last_price"] is None
    assert aapl["change_pct_30d"] is None
    assert aapl["updated_at"] is None
    # Composite is computed from static fame/value even before refresh.
    assert aapl["composite_score"] is not None


def test_seed_runs_only_once(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    first = client.get("/api/universe", params={"page_size": 100}).json()
    assert first["total"] == len(UNIVERSE)

    # A second call must not duplicate rows (table no longer empty -> no reseed).
    second = client.get("/api/universe", params={"page_size": 100}).json()
    assert second["total"] == len(UNIVERSE)

    with db_session_factory() as db:
        assert db.query(UniverseStat).count() == len(UNIVERSE)
