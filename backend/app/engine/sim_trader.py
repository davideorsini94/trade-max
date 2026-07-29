"""Motore di esecuzione del portafoglio simulato del sistema (blueprint §6 addendum).

Perché esiste
-------------
Prima di questo motore una posizione "aperta" era dedotta con un'euristica:
*l'ultimo BUY di un titolo non ancora seguito da un SELL*. Quell'euristica non
faceva scadere nulla, e siccome il sistema dice SELL molto raramente,
``open_allocation_pct`` cresceva in modo monotono. Con riserva di liquidità al
30% e sette posizioni fantasma al 70%, la regola 10 del PolicyEngine ha iniziato
a forzare a HOLD *ogni* nuovo BUY. Qui le posizioni hanno un ciclo di vita:
nascono su un BUY, muoiono su stop-loss, take-profit, scadenza dell'orizzonte o
un SELL, e liberano allocazione quando muoiono.

Garanzie
--------
* **Deterministico.** Il risultato è funzione solo di (consigli persistiti,
  barre giornaliere persistite). Nessuna rete, nessun LLM, nessun orologio: il
  momento in cui la passata gira non cambia le righe prodotte.
* **Idempotente.** Rieseguirlo non crea né modifica nulla, grazie al vincolo di
  unicità su ``open_recommendation_id`` e a quello su (posizione, lato, seduta).
* **Riproducibile.** Cancellando ``sim_positions`` e ``sim_fills`` e rilanciando
  la passata si riottengono righe identiche. Questo è anche il modo in cui il
  backfill storico funziona: non c'è codice speciale per i consigli passati.
* **Niente sguardo al futuro.** Un riempimento usa solo barre alla data del
  consiglio o successive, e stop/take-profit sono quelli congelati *al momento*
  del consiglio.

Regole dichiarate (le scelte discutibili, rese esplicite)
---------------------------------------------------------
1. **Riempimento alla chiusura della seduta SUCCESSIVA** a quella del consiglio.
   Un consiglio prodotto a metà seduta non poteva realisticamente ottenere la
   chiusura di quella stessa seduta. Questo divarica di proposito dal
   ``realized_return_7d`` del valutatore, che parte dalla chiusura del giorno del
   consiglio: le due cose rispondono a domande diverse ("il giudizio era
   giusto?" contro "era eseguibile?") e non vanno riconciliate.
2. **Sulle barre ambigue vince lo stop.** Se in una stessa barra giornaliera il
   minimo perfora lo stop e il massimo raggiunge il take-profit, l'ordine dei due
   eventi è inconoscibile senza dati intraday. Si sceglie l'esito pessimistico —
   coerente con un desk che dichiara la conservazione del capitale — e il caso
   viene contato in ``exit_ambiguous`` invece di essere nascosto.
3. **I salti di apertura si eseguono all'apertura.** Se la seduta apre già oltre
   lo stop (o oltre il take-profit), il prezzo ottenibile è l'apertura, non il
   livello teorico: eseguire allo stop sarebbe un numero inventato a favore.
4. **Nessuno scoperto.** Un SELL su un titolo senza posizione aperta non fa
   nulla: in questo prodotto SELL significa "esci / stai alla larga", non
   "vendi al ribasso".
5. **Posizioni scadute senza prezzi restano STALE.** Non viene MAI inventato un
   prezzo di uscita per chiudere il libro a forza.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.sim_book import (
    SIM_BASE_NOTIONAL,
    SIM_FEE_PCT,
    rebuild_position_state,
    shares_for_notional,
)
from app.models import PriceHistory, Recommendation, SimFill, SimPosition, Symbol

logger = logging.getLogger(__name__)

#: ``price_history.ts`` è la mezzanotte LOCALE della borsa salvata come UTC
#: naive, quindi una borsa non americana può cadere alle 22:00-23:00 UTC del
#: giorno di calendario precedente. Lo spostamento di +12h normalizza la riga
#: alla sua data di SEDUTA. La stessa costante esiste in
#: ``app.evaluation.evaluator`` (``_SESSION_DATE_SHIFT``) e la stessa logica in
#: ``app.engine.positions``: è duplicata di proposito per non creare una
#: dipendenza engine -> evaluation, ma le due DEVONO restare allineate.
_SESSION_DATE_SHIFT = timedelta(hours=12)

#: Giorni fra una tranche DCA e la successiva.
_DCA_TRANCHE_SPACING_DAYS = 7

#: Oltre questi giorni dalla scadenza senza una sola barra utilizzabile, la
#: posizione viene marcata STALE invece di restare aperta per sempre.
_STALE_GRACE_DAYS = 10

#: Orizzonte ammesso, allineato ai limiti che il valutatore già applica.
_HORIZON_MIN_DAYS = 7
_HORIZON_MAX_DAYS = 60


class _Bar:
    """Una barra giornaliera già normalizzata alla sua data di seduta."""

    __slots__ = ("session", "open", "high", "low", "close")

    def __init__(self, session: date, open_: float, high: float, low: float, close: float) -> None:
        self.session = session
        self.open = open_
        self.high = high
        self.low = low
        self.close = close


def _session_date(ts: datetime) -> date:
    return (ts + _SESSION_DATE_SHIFT).date()


def _load_bars(db: Session, symbol_id: int, since: date) -> list[_Bar]:
    """Barre giornaliere dalla seduta ``since`` in avanti, ordinate."""
    # Il filtro parte 2 giorni prima per non perdere una barra la cui ``ts``
    # grezza cade nel giorno di calendario precedente alla sua seduta.
    cutoff = datetime.combine(since - timedelta(days=2), datetime.min.time())
    rows = db.execute(
        select(PriceHistory.ts, PriceHistory.open, PriceHistory.high, PriceHistory.low, PriceHistory.close)
        .where(
            PriceHistory.symbol_id == symbol_id,
            PriceHistory.interval == "1d",
            PriceHistory.ts >= cutoff,
        )
        .order_by(PriceHistory.ts.asc())
    ).all()
    bars: list[_Bar] = []
    for ts, open_, high, low, close in rows:
        session = _session_date(ts)
        if session < since or close is None:
            continue
        bars.append(
            _Bar(
                session,
                float(open_) if open_ is not None else float(close),
                float(high) if high is not None else float(close),
                float(low) if low is not None else float(close),
                float(close),
            )
        )
    return bars


def _book_as_of(db: Session) -> date | None:
    """La seduta più recente presente nel database, usata come "oggi" del libro.

    Deve venire dai DATI e non da ``datetime.utcnow()``: il motore promette che
    il risultato è funzione solo di (consigli, barre persistite), e leggere
    l'orologio romperebbe sia il determinismo sia la riproducibilità.
    """
    latest = db.execute(
        select(PriceHistory.ts).where(PriceHistory.interval == "1d").order_by(PriceHistory.ts.desc()).limit(1)
    ).scalar_one_or_none()
    return _session_date(latest) if latest is not None else None


def _mark_stale_if_abandoned(
    position: SimPosition,
    *,
    created_session: date,
    as_of: date | None,
    counters: dict[str, int],
) -> None:
    """Marca STALE una posizione che non può più progredire.

    Serve al caso che avevo lasciato aperto: una posizione il cui titolo non ha
    (o non ha più) prezzi non veniva mai riempita, e quindi non passava nemmeno
    per il controllo di scadenza — restando aperta per sempre e occupando
    allocazione. Era esattamente il bug monotono che questo motore doveva
    chiudere, in miniatura.
    """
    if as_of is None:
        return
    expiry = created_session + timedelta(days=position.horizon_days)
    if as_of >= expiry + timedelta(days=_STALE_GRACE_DAYS):
        position.status = "STALE"
        counters["stale"] += 1


def _clamped_horizon(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 30
    return max(_HORIZON_MIN_DAYS, min(_HORIZON_MAX_DAYS, value))


def _tranche_target_dates(created: date, tranches: int) -> list[date]:
    """Prima data ammissibile per ciascuna tranche.

    La prima tranche non può eseguire prima della seduta *successiva* a quella
    del consiglio (regola dichiarata 1); le successive sono distanziate di sette
    giorni.
    """
    return [
        created + timedelta(days=1 + _DCA_TRANCHE_SPACING_DAYS * k) for k in range(max(1, tranches))
    ]


def _first_bar_at_or_after(bars: Iterable[_Bar], target: date) -> _Bar | None:
    for bar in bars:
        if bar.session >= target:
            return bar
    return None


def _resolve_exit(
    bars: list[_Bar],
    *,
    from_session: date,
    stop: float | None,
    take: float | None,
    expiry: date,
    sell_session: date | None,
) -> tuple[_Bar, str, float, bool] | None:
    """Prima uscita che scatta, o ``None`` se la posizione è ancora viva.

    Restituisce ``(barra, motivo, prezzo, ambigua)``. L'ordine di valutazione
    dentro una singola barra è dichiarato nel docstring del modulo: salto di
    apertura, poi il caso ambiguo (vince lo stop), poi stop, poi take-profit,
    poi scadenza. Un SELL consigliato chiude alla chiusura della prima seduta
    utile dalla data del consiglio di vendita.
    """
    for bar in bars:
        if bar.session < from_session:
            continue

        # --- salto di apertura: il prezzo ottenibile è l'apertura ---------- #
        if stop is not None and bar.open <= stop:
            return bar, "STOP_LOSS", bar.open, False
        if take is not None and bar.open >= take:
            return bar, "TAKE_PROFIT", bar.open, False

        hit_stop = stop is not None and bar.low <= stop
        hit_take = take is not None and bar.high >= take

        # --- barra ambigua: con dati giornalieri l'ordine è inconoscibile -- #
        if hit_stop and hit_take:
            return bar, "STOP_LOSS", float(stop), True
        if hit_stop:
            return bar, "STOP_LOSS", float(stop), False
        if hit_take:
            return bar, "TAKE_PROFIT", float(take), False

        # --- SELL consigliato -------------------------------------------- #
        if sell_session is not None and bar.session >= sell_session:
            return bar, "SELL_RECO", bar.close, False

        # --- scadenza dell'orizzonte ------------------------------------- #
        if bar.session >= expiry:
            return bar, "HORIZON", bar.close, False

    return None


def _first_sell_session(db: Session, symbol_id: int, after: datetime) -> date | None:
    """Data di seduta a partire dalla quale un SELL consigliato può eseguire.

    Come per gli ingressi, un consiglio di vendita non può ottenere la chiusura
    della seduta in cui è stato emesso: si parte dal giorno successivo.
    """
    row = db.execute(
        select(Recommendation.created_at)
        .where(
            Recommendation.symbol_id == symbol_id,
            Recommendation.action == "SELL",
            Recommendation.created_at > after,
        )
        .order_by(Recommendation.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    return (row.date() + timedelta(days=1)) if row is not None else None


def _open_new_positions(db: Session, counters: dict[str, int]) -> list[SimPosition]:
    """Crea una posizione per ogni BUY che non ne ha ancora una."""
    existing = set(db.execute(select(SimPosition.open_recommendation_id)).scalars().all())
    buys = (
        db.execute(
            select(Recommendation)
            .where(Recommendation.action == "BUY")
            .order_by(Recommendation.created_at.asc())
        )
        .scalars()
        .all()
    )

    created: list[SimPosition] = []
    for rec in buys:
        if rec.id in existing:
            continue
        weight = float(rec.allocation_pct or 0.0)
        if weight <= 0.0:
            # Un BUY con allocazione zero è già stato azzerato dalla policy:
            # non è una posizione, e contarla gonfierebbe l'esposizione.
            counters["skipped_zero_alloc"] += 1
            continue
        symbol = db.get(Symbol, rec.symbol_id)
        if symbol is None:
            counters["skipped_no_symbol"] += 1
            continue
        # Un secondo BUY su un titolo già in posizione non rinforza nulla: il
        # libro tiene una posizione per titolo.
        #
        # E soprattutto NON dev'essere un rientro quando quella posizione si
        # chiude. Un consiglio emesso mentre eravamo già dentro non è un segnale
        # di riacquisto: senza questo controllo un BUY duplicato di giorni prima
        # tornava eleggibile appena lo stop faceva chiudere la posizione, e il
        # libro rientrava sullo stesso titolo a prezzi vecchi facendo risalire
        # l'esposizione — con V e GE contemporaneamente chiuse E aperte.
        # Un BUY *successivo* all'uscita, invece, è un segnale legittimo.
        latest = db.execute(
            select(SimPosition)
            .where(SimPosition.symbol_id == rec.symbol_id)
            .order_by(SimPosition.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is not None:
            if latest.status in ("OPEN", "STALE"):
                counters["skipped_already_open"] += 1
                existing.add(rec.id)
                continue
            if latest.closed_session is None or rec.created_at.date() <= latest.closed_session:
                counters["skipped_stale_signal"] += 1
                existing.add(rec.id)
                continue

        tranches = max(1, int(rec.dca_tranches or 1))
        position = SimPosition(
            symbol_id=rec.symbol_id,
            open_recommendation_id=rec.id,
            currency=symbol.currency or "USD",
            weight_pct=weight,
            planned_notional=SIM_BASE_NOTIONAL * weight / 100.0,
            tranches_total=tranches,
            tranches_filled=0,
            stop_loss_price=rec.stop_loss_price,
            take_profit_price=rec.take_profit_price,
            horizon_days=_clamped_horizon(rec.horizon_days),
            status="OPEN",
        )
        db.add(position)
        db.flush()
        created.append(position)
        existing.add(rec.id)
        counters["opened"] += 1
    return created


def _fill_and_close(
    db: Session, position: SimPosition, counters: dict[str, int], as_of: date | None = None
) -> None:
    """Porta avanti una posizione: tranche mancanti e, se dovuta, l'uscita."""
    rec = db.get(Recommendation, position.open_recommendation_id)
    if rec is None:  # pragma: no cover - il vincolo di FK lo impedisce
        return
    created_session = rec.created_at.date()
    bars = _load_bars(db, position.symbol_id, created_session)
    if not bars:
        # Nessun prezzo dalla data del consiglio: non si può riempire nulla.
        # Senza questa guardia la posizione resterebbe aperta per sempre.
        _mark_stale_if_abandoned(
            position, created_session=created_session, as_of=as_of, counters=counters
        )
        return

    existing_fills = (
        db.execute(
            select(SimFill).where(SimFill.position_id == position.id).order_by(SimFill.session.asc())
        )
        .scalars()
        .all()
    )
    filled_sessions = {(f.side, f.session) for f in existing_fills}

    # --- ingressi (tranche) ----------------------------------------------- #
    notional_per_tranche = position.planned_notional / max(1, position.tranches_total)
    entry_bar: _Bar | None = None
    for index, target in enumerate(_tranche_target_dates(created_session, position.tranches_total)):
        bar = _first_bar_at_or_after(bars, target)
        if bar is None:
            break  # la tranche non è ancora eseguibile: si riprenderà alla prossima passata
        if entry_bar is None:
            entry_bar = bar
        if ("BUY", bar.session) in filled_sessions:
            continue
        shares = shares_for_notional(notional_per_tranche, bar.close)
        if shares is None:
            continue
        db.add(
            SimFill(
                position_id=position.id,
                side="BUY",
                reason="ENTRY" if index == 0 else "DCA",
                session=bar.session,
                price=bar.close,
                shares=shares,
                notional=notional_per_tranche,
            )
        )
        filled_sessions.add(("BUY", bar.session))
        counters["fills"] += 1

    db.flush()
    fills = (
        db.execute(
            select(SimFill).where(SimFill.position_id == position.id).order_by(SimFill.session.asc())
        )
        .scalars()
        .all()
    )
    if not fills:
        # Ci sono barre, ma nessuna abbastanza recente da eseguire la prima
        # tranche (o senza un prezzo utilizzabile): stessa guardia di sopra.
        _mark_stale_if_abandoned(
            position, created_session=created_session, as_of=as_of, counters=counters
        )
        return

    state = rebuild_position_state([_fill_dict(f) for f in fills])
    position.tranches_filled = state["tranches_filled"]
    position.shares_open = state["shares_open"]
    position.cost_total = state["cost_total"]
    position.avg_entry_price = state["avg_entry_price"]
    first_buy = min(f.session for f in fills if f.side == "BUY")
    position.opened_session = first_buy

    if position.status != "OPEN" or state["shares_open"] <= 0.0:
        return

    # --- uscita ------------------------------------------------------------ #
    expiry = created_session + timedelta(days=position.horizon_days)
    sell_session = _first_sell_session(db, position.symbol_id, rec.created_at)
    exit_hit = _resolve_exit(
        bars,
        from_session=first_buy,
        stop=position.stop_loss_price,
        take=position.take_profit_price,
        expiry=expiry,
        sell_session=sell_session,
    )

    if exit_hit is None:
        # Scaduta ma senza barre utili oltre la scadenza: si dichiara STALE
        # invece di inventare un prezzo di uscita.
        last_session = bars[-1].session
        if last_session >= expiry + timedelta(days=_STALE_GRACE_DAYS):
            position.status = "STALE"
            counters["stale"] += 1
        return

    bar, reason, price, ambiguous = exit_hit
    if ("SELL", bar.session) in filled_sessions:
        return

    shares = state["shares_open"]
    proceeds_gross = shares * price
    db.add(
        SimFill(
            position_id=position.id,
            side="SELL",
            reason=reason,
            session=bar.session,
            price=price,
            shares=shares,
            notional=proceeds_gross,
        )
    )
    db.flush()

    final = rebuild_position_state(
        [_fill_dict(f) for f in fills]
        + [{"side": "SELL", "shares": shares, "price": price, "notional": proceeds_gross}]
    )
    position.shares_open = final["shares_open"]
    position.status = "CLOSED"
    position.close_reason = reason
    position.closed_session = bar.session
    position.exit_price = price
    position.exit_ambiguous = ambiguous
    position.proceeds_net = proceeds_gross * (1.0 - SIM_FEE_PCT / 100.0)
    position.realized_pnl = final["realized_pnl"]
    position.realized_pnl_pct = final["realized_pnl_pct"]

    from app.engine.sim_book import compute_excursions

    span = [(b.session, b.low, b.high) for b in bars if first_buy <= b.session <= bar.session]
    position.mae_pct, position.mfe_pct = compute_excursions(span, position.avg_entry_price)
    counters["closed"] += 1
    if ambiguous:
        counters["ambiguous"] += 1


def _fill_dict(fill: SimFill) -> dict[str, Any]:
    return {
        "side": fill.side,
        "reason": fill.reason,
        "session": fill.session,
        "price": fill.price,
        "shares": fill.shares,
        "notional": fill.notional,
    }


def sync_shadow_book(db: Session) -> dict[str, int]:
    """Allinea il libro simulato ai consigli e ai prezzi persistiti.

    Unico punto d'ingresso del motore. Sicuro da chiamare quante volte si vuole:
    apre solo le posizioni mancanti, aggiunge solo le esecuzioni mancanti e
    chiude solo ciò che i prezzi dicono già chiuso. Restituisce contatori per il
    log — utili anche a rendere visibile ciò che è stato SALTATO, perché una
    copertura ridotta in silenzio si legge come "ho considerato tutto".
    """
    counters = {
        "opened": 0,
        "fills": 0,
        "closed": 0,
        "stale": 0,
        "ambiguous": 0,
        "skipped_zero_alloc": 0,
        "skipped_already_open": 0,
        # Consigli BUY emessi mentre la posizione era ancora aperta: non sono
        # rientri, e contarli qui rende visibile ciò che è stato scartato invece
        # di far sembrare che tutto sia stato considerato.
        "skipped_stale_signal": 0,
        "skipped_no_symbol": 0,
    }
    _open_new_positions(db, counters)

    as_of = _book_as_of(db)
    open_positions = (
        db.execute(select(SimPosition).where(SimPosition.status == "OPEN")).scalars().all()
    )
    for position in open_positions:
        _fill_and_close(db, position, counters, as_of)

    db.commit()
    if any(counters[k] for k in ("opened", "fills", "closed", "stale")):
        logger.info("Libro simulato aggiornato: %s", counters)
    return counters


def closed_position_rows(db: Session) -> list[dict[str, Any]]:
    """Posizioni chiuse in forma di dict, pronte per ``sim_book.sim_stats``.

    Tenuto qui (e non in ``sim_book``) perché tocca il database: ``sim_book``
    resta puro e testabile senza sessione.
    """
    positions = (
        db.execute(select(SimPosition).where(SimPosition.status == "CLOSED")).scalars().all()
    )
    return [
        {
            "symbol_id": pos.symbol_id,
            "opened_session": pos.opened_session,
            "close_reason": pos.close_reason,
            "realized_pnl_pct": pos.realized_pnl_pct,
            "mae_pct": pos.mae_pct,
            "mfe_pct": pos.mfe_pct,
            "exit_ambiguous": pos.exit_ambiguous,
        }
        for pos in positions
    ]


def open_sim_positions(db: Session, exclude_symbol_id: int | None = None) -> list[SimPosition]:
    """Posizioni attualmente aperte, opzionalmente escludendo un titolo.

    È la sorgente di verità che ha sostituito l'euristica "ultimo BUY senza
    SELL" per l'allocazione aperta della regola 10 e per le correlazioni.
    """
    stmt = select(SimPosition).where(SimPosition.status == "OPEN")
    if exclude_symbol_id is not None:
        stmt = stmt.where(SimPosition.symbol_id != exclude_symbol_id)
    return list(db.execute(stmt).scalars().all())
