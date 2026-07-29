"""Test della matematica pura del portafoglio simulato (blueprint §6 addendum).

Il modulo sotto test non tocca il DB: qui si verifica solo l'aritmetica, con
particolare attenzione a ciò che NON deve accadere — sommare valute diverse,
dichiarare un P&L su una posizione ancora aperta, o mostrare tassi su un
campione che non li sostiene.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.engine.sim_book import (
    SIM_BASE_NOTIONAL,
    SIM_STATS_MIN_N,
    SIM_STATS_MIN_SYMBOLS,
    aggregate_book,
    compact_book_for_prompt,
    compute_excursions,
    rebuild_position_state,
    shares_for_notional,
    sim_stats,
)


# --------------------------------------------------------------------------- #
# shares_for_notional
# --------------------------------------------------------------------------- #


def test_shares_from_notional() -> None:
    assert shares_for_notional(1000.0, 50.0) == pytest.approx(20.0)


@pytest.mark.parametrize("price", [None, 0.0, -3.0])
def test_shares_is_none_never_zero_when_price_unusable(price) -> None:
    """``None`` e ``0`` sono informazioni diverse: prezzo assente non è zero azioni."""
    assert shares_for_notional(1000.0, price) is None


# --------------------------------------------------------------------------- #
# rebuild_position_state — la definizione di verità dello stato
# --------------------------------------------------------------------------- #


def test_state_from_single_entry() -> None:
    state = rebuild_position_state([{"side": "BUY", "shares": 10.0, "price": 100.0, "notional": 1000.0}])
    assert state["shares_open"] == pytest.approx(10.0)
    assert state["avg_entry_price"] == pytest.approx(100.0)
    assert state["tranches_filled"] == 1
    # Posizione aperta: nessun P&L realizzato dichiarato.
    assert state["realized_pnl"] is None
    assert state["realized_pnl_pct"] is None


def test_average_entry_is_weighted_across_tranches() -> None:
    state = rebuild_position_state(
        [
            {"side": "BUY", "shares": 10.0, "price": 100.0, "notional": 1000.0},
            {"side": "BUY", "shares": 20.0, "price": 50.0, "notional": 1000.0},
        ]
    )
    assert state["tranches_filled"] == 2
    assert state["shares_open"] == pytest.approx(30.0)
    # 2000 spesi per 30 azioni, non la media aritmetica dei prezzi (75).
    assert state["avg_entry_price"] == pytest.approx(66.6667, abs=1e-3)


def test_full_exit_realizes_pnl() -> None:
    state = rebuild_position_state(
        [
            {"side": "BUY", "shares": 10.0, "price": 100.0, "notional": 1000.0},
            {"side": "SELL", "shares": 10.0, "price": 120.0, "notional": 1200.0},
        ]
    )
    assert state["shares_open"] == 0.0
    assert state["realized_pnl"] == pytest.approx(200.0)
    assert state["realized_pnl_pct"] == pytest.approx(20.0)


def test_partial_exit_does_not_declare_a_realized_pnl() -> None:
    """Un incasso parziale contro il costo pieno sarebbe un numero fuorviante."""
    state = rebuild_position_state(
        [
            {"side": "BUY", "shares": 10.0, "price": 100.0, "notional": 1000.0},
            {"side": "SELL", "shares": 4.0, "price": 120.0, "notional": 480.0},
        ]
    )
    assert state["shares_open"] == pytest.approx(6.0)
    assert state["realized_pnl"] is None


def test_floating_point_residue_counts_as_closed() -> None:
    state = rebuild_position_state(
        [
            {"side": "BUY", "shares": 3.3333333333, "price": 300.0, "notional": 1000.0},
            {"side": "SELL", "shares": 3.3333333333, "price": 310.0, "notional": 1033.33},
        ]
    )
    assert state["shares_open"] == 0.0
    assert state["realized_pnl"] is not None


# --------------------------------------------------------------------------- #
# compute_excursions — l'informazione che il rendimento puntuale non ha
# --------------------------------------------------------------------------- #


def test_excursions_capture_the_path_not_the_endpoint() -> None:
    """Due investimenti finiti allo stesso prezzo non sono lo stesso investimento."""
    bars = [
        (date(2026, 7, 1), 82.0, 101.0),  # è passato da -18%
        (date(2026, 7, 2), 95.0, 112.0),  # e da +12%
    ]
    mae, mfe = compute_excursions(bars, 100.0)
    assert mae == pytest.approx(-18.0)
    assert mfe == pytest.approx(12.0)


def test_excursions_are_signed_correctly_when_path_is_one_sided() -> None:
    """Un percorso sempre in guadagno ha MAE zero, non un MAE positivo."""
    mae, mfe = compute_excursions([(date(2026, 7, 1), 105.0, 110.0)], 100.0)
    assert mae == 0.0
    assert mfe == pytest.approx(10.0)


@pytest.mark.parametrize("entry", [None, 0.0, -1.0])
def test_excursions_degrade_to_none_without_a_reference_price(entry) -> None:
    assert compute_excursions([(date(2026, 7, 1), 90.0, 110.0)], entry) == (None, None)


def test_excursions_skip_unusable_bars() -> None:
    mae, mfe = compute_excursions(
        [(date(2026, 7, 1), None, None), (date(2026, 7, 2), 90.0, 105.0)], 100.0
    )
    assert mae == pytest.approx(-10.0)
    assert mfe == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# aggregate_book — mai sommare valute diverse
# --------------------------------------------------------------------------- #


def _pos(**kw):
    base = {
        "status": "OPEN",
        "currency": "USD",
        "weight_pct": 10.0,
        "cost_total": 1000.0,
        "realized_pnl": None,
        "unrealized_pnl": 50.0,
    }
    base.update(kw)
    return base


def test_pnl_is_grouped_by_currency_and_never_summed_across() -> None:
    book = aggregate_book(
        [
            _pos(status="CLOSED", currency="USD", realized_pnl=200.0),
            _pos(status="CLOSED", currency="EUR", realized_pnl=100.0),
            _pos(status="CLOSED", currency="EUR", realized_pnl=-30.0),
        ]
    )
    assert book["n_closed"] == 3
    assert book["by_currency"]["USD"]["realized_pnl"] == pytest.approx(200.0)
    assert book["by_currency"]["EUR"]["realized_pnl"] == pytest.approx(70.0)
    # Nessuna chiave che aggreghi tutte le valute insieme: non esiste un tasso.
    assert "total_pnl" not in book


def test_gross_exposure_counts_only_open_positions() -> None:
    book = aggregate_book(
        [
            _pos(weight_pct=10.0),
            _pos(weight_pct=15.0),
            _pos(status="CLOSED", weight_pct=20.0, realized_pnl=0.0),
            _pos(status="STALE", weight_pct=25.0),
        ]
    )
    assert book["gross_exposure_pct"] == pytest.approx(25.0)
    assert book["n_open"] == 2
    assert book["n_stale"] == 1


def test_one_missing_price_makes_the_unrealized_aggregate_declare_itself_partial() -> None:
    book = aggregate_book([_pos(unrealized_pnl=50.0), _pos(unrealized_pnl=None)])
    assert book["by_currency"]["USD"]["unrealized_pnl"] is None


def test_empty_book_degrades_cleanly() -> None:
    book = aggregate_book([])
    assert book["n_open"] == 0
    assert book["gross_exposure_pct"] == 0.0
    assert book["by_currency"] == {}


# --------------------------------------------------------------------------- #
# sim_stats — gli stessi cancelli di onestà della pagina Performance
# --------------------------------------------------------------------------- #


def _closed(symbol_id: int, opened: date, **kw):
    base = {
        "symbol_id": symbol_id,
        "opened_session": opened,
        "close_reason": "HORIZON",
        "realized_pnl_pct": 2.0,
        "mae_pct": -3.0,
        "mfe_pct": 5.0,
        "exit_ambiguous": False,
    }
    base.update(kw)
    return base


def _cohort(n_symbols: int, per_symbol: int):
    """``n_symbols`` titoli x ``per_symbol`` posizioni, ognuna in una settimana ISO diversa."""
    out = []
    for s in range(n_symbols):
        for i in range(per_symbol):
            out.append(_closed(s + 1, date(2026, 1, 5) + __import__("datetime").timedelta(days=7 * i)))
    return out


def test_stats_withheld_below_the_sample_floor() -> None:
    stats = sim_stats(_cohort(4, 2))  # 8 posizioni, 4 titoli
    assert stats["n"] == 8
    assert stats["status"] == "dati_insufficienti"
    assert stats["stop_hit_rate"] is None
    # I conteggi restano visibili, così l'interfaccia può spiegare quanto manca.
    assert stats["min_n"] == SIM_STATS_MIN_N
    assert stats["min_symbols"] == SIM_STATS_MIN_SYMBOLS


def test_stats_withheld_when_samples_concentrate_on_few_symbols() -> None:
    stats = sim_stats(_cohort(3, 5))  # 15 posizioni ma solo 3 titoli
    assert stats["n"] == 15
    assert stats["n_symbols"] == 3
    assert stats["status"] == "dati_insufficienti"


def test_stats_released_when_both_floors_clear() -> None:
    stats = sim_stats(_cohort(4, 3))  # 12 posizioni, 4 titoli
    assert stats["status"] == "ok"
    assert stats["n"] == 12
    assert stats["avg_mae_pct"] == pytest.approx(-3.0)
    assert stats["avg_mfe_pct"] == pytest.approx(5.0)
    assert stats["by_close_reason"]["HORIZON"] == 12


def test_same_symbol_same_week_counts_once() -> None:
    """Stessa difesa contro la pseudo-replicazione della pagina Performance."""
    same_week = [
        _closed(1, date(2026, 7, 6)),
        _closed(1, date(2026, 7, 7)),
        _closed(1, date(2026, 7, 8)),
    ]
    stats = sim_stats(same_week)
    assert stats["n_raw"] == 3
    assert stats["n"] == 1


def test_stop_and_tp_rates_and_ambiguous_count() -> None:
    rows = _cohort(4, 3)
    rows[0]["close_reason"] = "STOP_LOSS"
    rows[0]["exit_ambiguous"] = True
    rows[1]["close_reason"] = "TAKE_PROFIT"
    stats = sim_stats(rows)
    assert stats["status"] == "ok"
    assert stats["stop_hit_rate"] == pytest.approx(1 / 12, abs=1e-4)
    assert stats["tp_hit_rate"] == pytest.approx(1 / 12, abs=1e-4)
    assert stats["n_ambiguous"] == 1


# --------------------------------------------------------------------------- #
# compact_book_for_prompt — stato sì, performance no
# --------------------------------------------------------------------------- #


def test_prompt_block_is_omitted_entirely_when_no_position_is_open() -> None:
    """Sul caso comune (libro vuoto) il prompt non deve pagare un token."""
    assert compact_book_for_prompt(aggregate_book([]), []) is None


def test_prompt_block_carries_state_and_never_a_win_rate() -> None:
    positions = [
        _pos(weight_pct=15.0) | {"ticker": "AAA", "days_open": 4, "unrealized_pnl_pct": 3.21},
        _pos(weight_pct=5.0) | {"ticker": "BBB", "days_open": 11, "unrealized_pnl_pct": -1.0},
    ]
    block = compact_book_for_prompt(aggregate_book(positions), positions)
    assert block is not None
    assert block["n_open"] == 2
    assert block["gross_exposure_pct"] == pytest.approx(20.0)
    # Ordinato per peso: la posizione più grande è quella che conta per la
    # concentrazione.
    assert [row["t"] for row in block["positions"]] == ["AAA", "BBB"]
    assert block["positions"][0]["pnl_pct"] == pytest.approx(3.2)
    # Nessuna statistica di performance storica: è il presidio anti-ancoraggio.
    forbidden = {"win_rate", "accuracy", "stop_hit_rate", "avg_pnl_pct", "streak"}
    assert forbidden.isdisjoint(block.keys())


def test_prompt_block_caps_the_position_list_and_declares_truncation() -> None:
    positions = [
        _pos(weight_pct=float(10 + i)) | {"ticker": f"S{i}", "days_open": i}
        for i in range(8)
    ]
    block = compact_book_for_prompt(aggregate_book(positions), positions, max_positions=5)
    assert block is not None
    assert len(block["positions"]) == 5
    assert block["truncated"] is True


def test_base_notional_is_a_declared_constant() -> None:
    """Il capitale simbolico non è il budget dell'utente: è solo una scala."""
    assert SIM_BASE_NOTIONAL == 10_000.0
