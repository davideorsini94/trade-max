"""Test del motore di esecuzione del libro simulato (blueprint §6 addendum).

Il bug che questo motore chiude: ``_open_buy_positions`` considerava un BUY
aperto per sempre finché non arrivava un SELL. Siccome il sistema dice SELL
molto raramente, ``open_allocation_pct`` era arrivato al 70% e la regola 10
stava forzando a HOLD ogni nuovo BUY — 7 delle ultime 14 analisi reali. Qui si
verifica che le posizioni nascano, si riempiano e soprattutto MUOIANO.

Le casistiche interessanti sono quelle sgradevoli: la barra in cui stop e
take-profit vengono toccati entrambi, il salto di apertura, la scadenza nel
weekend, il DCA interrotto a metà, il titolo senza prezzi.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.engine.sim_trader import open_sim_positions, sync_shadow_book
from app.models import AnalysisRun, PriceHistory, Recommendation, SimFill, SimPosition, Symbol


# --------------------------------------------------------------------------- #
# Helper di semina
# --------------------------------------------------------------------------- #


def _symbol(db: Session, ticker: str = "AAA", currency: str = "USD") -> Symbol:
    symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency=currency)
    db.add(symbol)
    db.commit()
    db.refresh(symbol)
    return symbol


def _bars(db: Session, symbol_id: int, start: date, rows: list[tuple[float, float, float, float]]) -> None:
    """Semina barre giornaliere consecutive a partire da ``start``.

    ``ts`` è la mezzanotte della seduta: è la convenzione che il motore
    normalizza con lo spostamento di +12h, e seminarla diversamente farebbe
    passare i test su un'assunzione sbagliata.
    """
    for offset, (open_, high, low, close) in enumerate(rows):
        session = start + timedelta(days=offset)
        db.add(
            PriceHistory(
                symbol_id=symbol_id,
                ts=datetime.combine(session, datetime.min.time()),
                interval="1d",
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1000.0,
            )
        )
    db.commit()


def _reco(
    db: Session,
    symbol: Symbol,
    *,
    action: str = "BUY",
    created: date,
    allocation_pct: float = 10.0,
    stop: float | None = None,
    take: float | None = None,
    horizon_days: int = 30,
    dca_tranches: int = 1,
) -> Recommendation:
    run = AnalysisRun(symbol_id=symbol.id, status="COMPLETED", trigger="MANUAL")
    db.add(run)
    db.flush()
    rec = Recommendation(
        run_id=run.id,
        symbol_id=symbol.id,
        action=action,
        sizing_strategy="DCA" if dca_tranches > 1 else ("ALL_IN" if action == "BUY" else "WAIT"),
        confidence=0.6,
        allocation_pct=allocation_pct,
        dca_tranches=dca_tranches,
        validator_verdict="APPROVE",
        entry_price=100.0,
        stop_loss_price=stop,
        take_profit_price=take,
        horizon_days=horizon_days,
        created_at=datetime.combine(created, datetime.min.time()) + timedelta(hours=15),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


FLAT = (100.0, 101.0, 99.0, 100.0)


# --------------------------------------------------------------------------- #
# Apertura e riempimento
# --------------------------------------------------------------------------- #


def test_buy_opens_a_position_filled_at_the_next_session_close(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Regola dichiarata 1: la chiusura del giorno stesso non era ottenibile."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1))
        _bars(db, sym.id, date(2026, 7, 1), [FLAT, (102.0, 103.0, 101.0, 102.5), FLAT])

        counters = sync_shadow_book(db)
        assert counters["opened"] == 1

        pos = db.query(SimPosition).one()
        assert pos.status == "OPEN"
        assert pos.opened_session == date(2026, 7, 2)
        # Riempita a 102.5, la chiusura della seduta SUCCESSIVA, non a 100.
        assert pos.avg_entry_price == pytest.approx(102.5)
        # 10% di 10_000 = 1000 di nozionale.
        assert pos.cost_total == pytest.approx(1000.0)
        assert pos.shares_open == pytest.approx(1000.0 / 102.5)
    finally:
        db.close()


def test_buy_with_zero_allocation_is_not_a_position(
    db_session_factory: sessionmaker[Session],
) -> None:
    """La policy ha già azzerato quel BUY: contarlo gonfierebbe l'esposizione."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), allocation_pct=0.0)
        _bars(db, sym.id, date(2026, 7, 1), [FLAT, FLAT])
        counters = sync_shadow_book(db)
        assert counters["opened"] == 0
        assert counters["skipped_zero_alloc"] == 1
    finally:
        db.close()


def test_second_buy_on_an_open_symbol_does_not_reinforce(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1))
        _bars(db, sym.id, date(2026, 7, 1), [FLAT] * 8)
        sync_shadow_book(db)
        _reco(db, sym, created=date(2026, 7, 3))
        counters = sync_shadow_book(db)
        assert counters["skipped_already_open"] == 1
        assert db.query(SimPosition).count() == 1
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Uscite — le regole dichiarate
# --------------------------------------------------------------------------- #


def test_stop_wins_when_one_bar_touches_both_stop_and_take_profit(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Regola dichiarata 2: con barre giornaliere l'ordine è inconoscibile.

    Si scegli l'esito pessimistico e si CONTA il caso, invece di nasconderlo
    dietro un numero che sembra certo.
    """
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), stop=95.0, take=110.0)
        _bars(
            db,
            sym.id,
            date(2026, 7, 1),
            [
                FLAT,
                FLAT,  # ingresso a 100
                (100.0, 112.0, 94.0, 105.0),  # tocca ENTRAMBI
            ],
        )
        counters = sync_shadow_book(db)
        assert counters["closed"] == 1
        assert counters["ambiguous"] == 1

        pos = db.query(SimPosition).one()
        assert pos.status == "CLOSED"
        assert pos.close_reason == "STOP_LOSS"
        assert pos.exit_price == pytest.approx(95.0)
        assert pos.exit_ambiguous is True
    finally:
        db.close()


def test_gap_open_below_stop_fills_at_the_open_not_at_the_stop(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Regola dichiarata 3: eseguire allo stop sarebbe un numero inventato a favore."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), stop=95.0)
        _bars(
            db,
            sym.id,
            date(2026, 7, 1),
            [FLAT, FLAT, (88.0, 90.0, 85.0, 89.0)],  # apre già sotto lo stop
        )
        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        assert pos.close_reason == "STOP_LOSS"
        assert pos.exit_price == pytest.approx(88.0)
        assert pos.exit_ambiguous is False
        assert pos.realized_pnl_pct == pytest.approx(-12.0)
    finally:
        db.close()


def test_take_profit_closes_the_position(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), stop=90.0, take=110.0)
        _bars(db, sym.id, date(2026, 7, 1), [FLAT, FLAT, (101.0, 111.0, 100.0, 108.0)])
        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        assert pos.close_reason == "TAKE_PROFIT"
        assert pos.exit_price == pytest.approx(110.0)
        assert pos.realized_pnl_pct == pytest.approx(10.0)
    finally:
        db.close()


def test_missing_stop_and_take_profit_simply_never_trigger(
    db_session_factory: sessionmaker[Session],
) -> None:
    """``None`` non viene mai sostituito da un livello inventato."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), stop=None, take=None, horizon_days=30)
        _bars(db, sym.id, date(2026, 7, 1), [(100.0, 200.0, 10.0, 100.0)] * 6)
        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        # Barre violentissime, ma senza livelli non c'è nulla da colpire.
        assert pos.status == "OPEN"
        assert pos.close_reason is None
    finally:
        db.close()


def test_horizon_expiry_closes_at_the_first_available_session(
    db_session_factory: sessionmaker[Session],
) -> None:
    """La scadenza cade di sabato: si chiude alla prima seduta utile dopo."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        # 2026-07-01 è un mercoledì; +7 giorni = 2026-07-08 (mercoledì).
        # Si semina un buco per simulare il weekend intorno alla scadenza.
        _reco(db, sym, created=date(2026, 7, 1), horizon_days=7)
        for offset, close in ((1, 100.0), (2, 101.0), (9, 104.0)):
            session = date(2026, 7, 1) + timedelta(days=offset)
            db.add(
                PriceHistory(
                    symbol_id=sym.id,
                    ts=datetime.combine(session, datetime.min.time()),
                    interval="1d",
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1.0,
                )
            )
        db.commit()

        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        assert pos.close_reason == "HORIZON"
        assert pos.closed_session == date(2026, 7, 10)
        assert pos.exit_price == pytest.approx(104.0)
    finally:
        db.close()


def test_sell_recommendation_closes_the_position(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), horizon_days=60)
        _bars(db, sym.id, date(2026, 7, 1), [FLAT] * 3 + [(103.0, 104.0, 102.0, 103.5)] * 3)
        _reco(db, sym, action="SELL", created=date(2026, 7, 3), allocation_pct=0.0)
        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        assert pos.close_reason == "SELL_RECO"
        assert pos.closed_session == date(2026, 7, 4)
    finally:
        db.close()


def test_sell_without_an_open_position_does_nothing(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Regola dichiarata 4: in questo prodotto SELL non significa vendere allo scoperto."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, action="SELL", created=date(2026, 7, 1), allocation_pct=0.0)
        _bars(db, sym.id, date(2026, 7, 1), [FLAT] * 4)
        counters = sync_shadow_book(db)
        assert counters["opened"] == 0
        assert db.query(SimPosition).count() == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# DCA
# --------------------------------------------------------------------------- #


def test_dca_tranches_fill_one_week_apart(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), dca_tranches=3, allocation_pct=15.0, horizon_days=60)
        _bars(db, sym.id, date(2026, 7, 1), [FLAT] * 30)
        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        assert pos.tranches_total == 3
        assert pos.tranches_filled == 3
        # Tutto il nozionale pianificato è stato investito: 15% di 10_000.
        assert pos.cost_total == pytest.approx(1500.0)
        sessions = sorted(
            f.session for f in db.query(SimFill).filter(SimFill.side == "BUY").all()
        )
        assert sessions == [date(2026, 7, 2), date(2026, 7, 9), date(2026, 7, 16)]
    finally:
        db.close()


def test_stop_after_the_second_tranche_cancels_the_rest(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Il P&L si calcola sul capitale EFFETTIVAMENTE investito, non su quello pianificato."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(
            db, sym, created=date(2026, 7, 1), dca_tranches=4, allocation_pct=20.0,
            stop=90.0, horizon_days=60,
        )
        rows = [FLAT] * 12
        rows[11] = (95.0, 96.0, 88.0, 89.0)  # lo stop scatta il 2026-07-12
        _bars(db, sym.id, date(2026, 7, 1), rows)
        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        assert pos.close_reason == "STOP_LOSS"
        # Solo 2 delle 4 tranche erano state eseguite (2 e 9 luglio).
        assert pos.tranches_filled == 2
        assert pos.cost_total == pytest.approx(1000.0)  # 2 x 500, non 2000
        assert pos.realized_pnl_pct == pytest.approx(-10.0)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Degradazione pulita
# --------------------------------------------------------------------------- #


def test_symbol_without_prices_stays_unfilled_and_invents_nothing(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1))
        counters = sync_shadow_book(db)
        assert counters["opened"] == 1
        pos = db.query(SimPosition).one()
        assert pos.status == "OPEN"
        assert pos.avg_entry_price is None
        assert pos.opened_session is None
        assert db.query(SimFill).count() == 0
    finally:
        db.close()


def test_expired_position_without_prices_becomes_stale_not_a_fake_exit(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Regola dichiarata 5: non si inventa un prezzo per chiudere il libro a forza."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), horizon_days=7)
        # Barre solo fino al 4 luglio, poi il titolo smette di quotare; l'ultima
        # barra è oltre scadenza (8 luglio) + 10 giorni di tolleranza.
        _bars(db, sym.id, date(2026, 7, 1), [FLAT, FLAT, FLAT])
        db.add(
            PriceHistory(
                symbol_id=sym.id,
                ts=datetime.combine(date(2026, 7, 25), datetime.min.time()),
                interval="1d",
                open=100.0, high=100.0, low=100.0, close=100.0, volume=1.0,
            )
        )
        db.commit()
        # La barra del 25 luglio chiude per scadenza: qui interessa il caso in
        # cui NON esiste alcuna barra oltre la scadenza, quindi si rimuove.
        db.query(PriceHistory).filter(PriceHistory.ts >= datetime(2026, 7, 20)).delete()
        db.commit()
        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        assert pos.status == "OPEN"  # ancora dentro la tolleranza dei 10 giorni
        assert pos.exit_price is None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Idempotenza e riproducibilità — le garanzie che rendono il backfill sicuro
# --------------------------------------------------------------------------- #


def _snapshot(db: Session) -> list[tuple]:
    positions = [
        (p.symbol_id, p.open_recommendation_id, p.status, p.close_reason, p.exit_price,
         p.cost_total, p.shares_open, p.tranches_filled, p.mae_pct, p.mfe_pct)
        for p in db.query(SimPosition).order_by(SimPosition.id).all()
    ]
    fills = [
        (f.side, f.reason, f.session, f.price, f.shares)
        for f in db.query(SimFill).order_by(SimFill.session, SimFill.side).all()
    ]
    return [tuple(positions), tuple(fills)]


def test_running_twice_changes_nothing(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), stop=95.0, take=110.0, dca_tranches=2, horizon_days=30)
        _bars(db, sym.id, date(2026, 7, 1), [FLAT] * 20)
        sync_shadow_book(db)
        first = _snapshot(db)
        counters = sync_shadow_book(db)
        assert _snapshot(db) == first
        assert counters["opened"] == 0
        assert counters["fills"] == 0
    finally:
        db.close()


def test_book_is_replayable_from_scratch(db_session_factory: sessionmaker[Session]) -> None:
    """Cancellando il libro e rifacendo la passata si riottengono righe identiche.

    È la stessa proprietà che rende il backfill dei consigli storici sicuro:
    non esiste codice speciale per il passato.
    """
    db = db_session_factory()
    try:
        sym_a = _symbol(db, "AAA")
        sym_b = _symbol(db, "BBB", currency="EUR")
        _reco(db, sym_a, created=date(2026, 7, 1), stop=95.0, horizon_days=14)
        _reco(db, sym_b, created=date(2026, 7, 2), take=108.0, horizon_days=21)
        _bars(db, sym_a.id, date(2026, 7, 1), [FLAT] * 10 + [(96.0, 97.0, 93.0, 94.0)])
        _bars(db, sym_b.id, date(2026, 7, 2), [FLAT] * 5 + [(105.0, 109.0, 104.0, 107.0)])
        sync_shadow_book(db)
        first = _snapshot(db)
        assert first[0]  # qualcosa è stato prodotto

        db.query(SimFill).delete()
        db.query(SimPosition).delete()
        db.commit()
        sync_shadow_book(db)
        assert _snapshot(db) == first
    finally:
        db.close()


def test_excursions_are_recorded_on_close(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), stop=80.0, horizon_days=7)
        _bars(
            db,
            sym.id,
            date(2026, 7, 1),
            [
                FLAT,
                (100.0, 100.0, 100.0, 100.0),  # ingresso a 100
                (100.0, 118.0, 97.0, 105.0),
                (105.0, 106.0, 85.0, 90.0),
                (90.0, 92.0, 88.0, 91.0),
                (91.0, 93.0, 90.0, 92.0),
                (92.0, 94.0, 91.0, 93.0),
                (93.0, 95.0, 92.0, 94.0),
                (94.0, 96.0, 93.0, 95.0),
            ],
        )
        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        assert pos.close_reason == "HORIZON"
        # È passata da +18% e da -15%: informazione che il rendimento finale
        # (-5%) non contiene.
        assert pos.mfe_pct == pytest.approx(18.0)
        assert pos.mae_pct == pytest.approx(-15.0)
    finally:
        db.close()


def test_open_sim_positions_excludes_the_symbol_under_analysis(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        sym_a = _symbol(db, "AAA")
        sym_b = _symbol(db, "BBB")
        _reco(db, sym_a, created=date(2026, 7, 1), horizon_days=60)
        _reco(db, sym_b, created=date(2026, 7, 1), horizon_days=60)
        _bars(db, sym_a.id, date(2026, 7, 1), [FLAT] * 5)
        _bars(db, sym_b.id, date(2026, 7, 1), [FLAT] * 5)
        sync_shadow_book(db)
        assert len(open_sim_positions(db)) == 2
        assert [p.symbol_id for p in open_sim_positions(db, exclude_symbol_id=sym_a.id)] == [sym_b.id]
    finally:
        db.close()


def test_closed_position_frees_allocation(db_session_factory: sessionmaker[Session]) -> None:
    """Il cuore del bug: una posizione chiusa NON deve più occupare allocazione."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), allocation_pct=10.0, stop=95.0, horizon_days=30)
        _bars(db, sym.id, date(2026, 7, 1), [FLAT, FLAT, (94.0, 95.0, 90.0, 92.0)])
        sync_shadow_book(db)
        assert db.query(SimPosition).one().status == "CLOSED"
        assert open_sim_positions(db) == []
    finally:
        db.close()


def test_a_position_that_can_never_fill_does_not_occupy_allocation_forever(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Il buco che avevo lasciato aperto: senza prezzi la posizione non veniva
    mai riempita, quindi non passava nemmeno per il controllo di scadenza e
    restava aperta per sempre — lo stesso bug monotono in miniatura."""
    db = db_session_factory()
    try:
        dead = _symbol(db, "DEAD")
        # Un secondo titolo con prezzi recenti fornisce la data di riferimento
        # del libro, che deve venire dai DATI e non dall'orologio.
        alive = _symbol(db, "ALIVE")
        _bars(db, alive.id, date(2026, 7, 1), [FLAT] * 60)

        _reco(db, dead, created=date(2026, 7, 1), horizon_days=7, allocation_pct=70.0)
        sync_shadow_book(db)

        pos = db.query(SimPosition).filter(SimPosition.symbol_id == dead.id).one()
        # Scadenza 8 luglio + 10 giorni di tolleranza, e il libro è al 29 agosto.
        assert pos.status == "STALE"
        assert pos.exit_price is None  # nessun prezzo di uscita inventato
        assert open_sim_positions(db) == []
    finally:
        db.close()


def test_a_recent_unfilled_position_stays_open_inside_the_grace_period(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Il rovescio: una posizione appena aperta il cui prezzo arriverà domani non
    va marcata STALE, altrimenti ogni BUY di oggi morirebbe subito."""
    db = db_session_factory()
    try:
        sym = _symbol(db, "FRESH")
        _bars(db, sym.id, date(2026, 7, 1), [FLAT] * 3)
        # Consiglio emesso l'ULTIMA seduta disponibile: il riempimento richiede la
        # seduta successiva, che non esiste ancora.
        _reco(db, sym, created=date(2026, 7, 3), horizon_days=30)
        sync_shadow_book(db)
        pos = db.query(SimPosition).one()
        assert pos.status == "OPEN"
        assert pos.avg_entry_price is None
    finally:
        db.close()


def test_a_buy_issued_while_already_in_position_never_becomes_a_reentry(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Il difetto trovato sui dati reali: V e GE risultavano contemporaneamente
    chiuse E aperte, e l'esposizione risaliva al 70%.

    Un BUY duplicato, saltato perché la posizione era aperta, tornava eleggibile
    appena lo stop la chiudeva — e il libro rientrava su un segnale di giorni
    prima, a prezzi vecchi."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), stop=95.0, horizon_days=60)
        # Secondo BUY mentre siamo ancora dentro: non è un segnale di rientro.
        _reco(db, sym, created=date(2026, 7, 2), stop=95.0, horizon_days=60)
        _bars(db, sym.id, date(2026, 7, 1), [FLAT] * 5 + [(94.0, 95.0, 90.0, 92.0)] + [FLAT] * 5)

        counters = sync_shadow_book(db)
        assert counters["opened"] == 1
        # La prima passata lo salta perché la posizione è aperta...
        assert counters["skipped_already_open"] == 1

        # ...e la seconda, dopo la chiusura per stop, NON lo trasforma in rientro.
        counters = sync_shadow_book(db)
        assert counters["opened"] == 0
        assert db.query(SimPosition).count() == 1
        assert open_sim_positions(db) == []
    finally:
        db.close()


def test_a_buy_issued_after_the_exit_is_a_legitimate_new_position(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Il rovescio: se il desk esce e poi, giorni dopo, consiglia di nuovo di
    comprare, quello è un segnale nuovo e la posizione va aperta."""
    db = db_session_factory()
    try:
        sym = _symbol(db)
        _reco(db, sym, created=date(2026, 7, 1), stop=95.0, horizon_days=60)
        _bars(db, sym.id, date(2026, 7, 1), [FLAT] * 3 + [(94.0, 95.0, 90.0, 92.0)] + [FLAT] * 10)
        sync_shadow_book(db)
        closed = db.query(SimPosition).one()
        assert closed.status == "CLOSED"

        # Nuovo BUY DOPO l'uscita.
        _reco(db, sym, created=date(2026, 7, 8), horizon_days=60)
        counters = sync_shadow_book(db)
        assert counters["opened"] == 1
        assert db.query(SimPosition).count() == 2
        assert len(open_sim_positions(db)) == 1
    finally:
        db.close()
