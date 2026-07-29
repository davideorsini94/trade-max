"""Matematica pura del portafoglio simulato del sistema (blueprint §6 addendum).

Questo modulo NON tocca il database e non fa rete: sono funzioni pure su
strutture semplici, così l'aritmetica del libro è testabile in isolamento e il
motore in ``app.engine.sim_trader`` resta libero di occuparsi solo di *quando*
succedono le cose.

Tre principi ereditati dal resto del progetto:

* **Niente dati inventati.** Ogni valore non calcolabile è ``None`` e viene
  dichiarato tale. In particolare non esiste alcun tasso di cambio: gli
  aggregati monetari sono raggruppati per valuta e mai sommati fra valute.
* **Nessuna commissione fittizia.** ``SIM_FEE_PCT`` è 0: una commissione
  positiva sarebbe un numero inventato. Il libro è un segnale comparativo, non
  contabilità. Si rivedrà solo se l'utente fornirà la sua commissione reale.
* **Onestà statistica.** Le statistiche di percorso passano dagli stessi
  cancelli della pagina Performance (vedi ``app.evaluation.summary``): sotto
  soglia si dichiara "dati insufficienti" invece di mostrare un numero che a
  quel campione non significa nulla.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Iterable, Sequence

# Capitale nozionale simbolico del libro. Non è denaro dell'utente e non è la
# sua ``total_budget``: è solo la scala che trasforma una percentuale di
# allocazione in un numero di azioni, così le escursioni di prezzo diventano
# confrontabili tra titoli. Espresso nella valuta di CIASCUN titolo, perché
# l'app non ha uno strato di cambio (vedi il docstring del modulo).
SIM_BASE_NOTIONAL: float = 10_000.0

# Commissione simulata: zero per scelta dichiarata, non per dimenticanza.
SIM_FEE_PCT: float = 0.0

# Soglie di onestà per le statistiche di percorso: identiche a quelle delle
# statistiche per feature, per non avere due definizioni di "campione
# sufficiente" nello stesso prodotto.
SIM_STATS_MIN_N: int = 12
SIM_STATS_MIN_SYMBOLS: int = 4

_CLOSE_REASONS: tuple[str, ...] = ("STOP_LOSS", "TAKE_PROFIT", "HORIZON", "SELL_RECO")


def _f(value: Any) -> float | None:
    """Coercizione difensiva a float; ``None`` se non è un numero utilizzabile."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):  # NaN o infinito
        return None
    return out


def shares_for_notional(notional: float, price: float | None) -> float | None:
    """Azioni acquistabili con ``notional`` al prezzo dato, o ``None``.

    Restituisce ``None`` — mai zero, che sarebbe un'informazione diversa —
    quando il prezzo non è disponibile o non è positivo.
    """
    px = _f(price)
    amount = _f(notional)
    if px is None or px <= 0.0 or amount is None or amount <= 0.0:
        return None
    return amount / px


def rebuild_position_state(fills: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Ricostruisce lo stato di una posizione dalle sue sole esecuzioni.

    È la definizione di verità dello stato: le colonne denormalizzate su
    ``SimPosition`` devono sempre coincidere con questo risultato, ed è ciò che
    i test verificano.

    Ogni esecuzione è un dict con almeno ``side`` ("BUY"/"SELL"), ``shares``,
    ``price``, ``notional``. Il costo medio d'ingresso è ponderato sulle sole
    esecuzioni in acquisto.
    """
    shares_bought = 0.0
    shares_sold = 0.0
    cost_total = 0.0
    proceeds_total = 0.0
    tranches_filled = 0

    for fill in fills:
        side = str(fill.get("side") or "").upper()
        shares = _f(fill.get("shares")) or 0.0
        notional = _f(fill.get("notional")) or 0.0
        if side == "BUY":
            shares_bought += shares
            cost_total += notional
            tranches_filled += 1
        elif side == "SELL":
            shares_sold += shares
            proceeds_total += notional

    shares_open = shares_bought - shares_sold
    # Le esecuzioni sono generate dal motore a partire dallo stesso conteggio di
    # azioni, quindi un residuo diverso da zero è solo errore di virgola mobile.
    if abs(shares_open) < 1e-9:
        shares_open = 0.0

    avg_entry = (cost_total / shares_bought) if shares_bought > 0.0 else None

    # Il P&L realizzato è definito solo quando la posizione è effettivamente
    # chiusa: su una posizione ancora aperta il costo residuo non è confrontabile
    # con l'incasso parziale, e un numero parziale sarebbe fuorviante.
    realized_pnl: float | None = None
    realized_pnl_pct: float | None = None
    if shares_sold > 0.0 and shares_open == 0.0 and cost_total > 0.0:
        realized_pnl = proceeds_total - cost_total
        realized_pnl_pct = realized_pnl / cost_total * 100.0

    return {
        "shares_open": shares_open,
        "shares_bought": shares_bought,
        "shares_sold": shares_sold,
        "cost_total": cost_total,
        "proceeds_total": proceeds_total,
        "avg_entry_price": avg_entry,
        "tranches_filled": tranches_filled,
        "realized_pnl": realized_pnl,
        "realized_pnl_pct": realized_pnl_pct,
    }


def compute_excursions(
    bars: Iterable[tuple[date, float | None, float | None]],
    avg_entry_price: float | None,
) -> tuple[float | None, float | None]:
    """Escursione avversa e favorevole massima, in percentuale sul prezzo medio.

    ``bars`` è una sequenza di ``(seduta, minimo, massimo)`` fra il primo
    riempimento e l'uscita. MAE è il ribasso peggiore toccato, MFE il rialzo
    migliore: entrambi misurano il PERCORSO, non il punto d'arrivo, ed è
    l'informazione che il rendimento a 7 giorni non può contenere. Un titolo che
    è finito a +1% dopo essere passato da -18% non è lo stesso investimento di
    uno che è salito lineare, e finora il sistema non poteva distinguerli.

    Con più tranche il riferimento è il prezzo medio finale: è
    un'approssimazione dichiarata, perché il prezzo medio cambia a ogni tranche.
    Restituisce ``(None, None)`` se manca il prezzo di riferimento o se nessuna
    barra ha dati utilizzabili.
    """
    entry = _f(avg_entry_price)
    if entry is None or entry <= 0.0:
        return None, None

    worst: float | None = None
    best: float | None = None
    for _session, low, high in bars:
        lo = _f(low)
        hi = _f(high)
        if lo is not None and lo > 0.0:
            excursion = (lo - entry) / entry * 100.0
            worst = excursion if worst is None else min(worst, excursion)
        if hi is not None and hi > 0.0:
            excursion = (hi - entry) / entry * 100.0
            best = excursion if best is None else max(best, excursion)

    # MAE è per definizione ≤ 0 e MFE ≥ 0: se il percorso è stato interamente
    # sopra (o sotto) il prezzo medio, l'escursione nell'altra direzione è 0,
    # non un valore col segno sbagliato.
    if worst is not None:
        worst = min(worst, 0.0)
    if best is not None:
        best = max(best, 0.0)
    return worst, best


def aggregate_book(positions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregati del libro: conteggi, esposizione e P&L RAGGRUPPATO PER VALUTA.

    Ogni posizione è un dict con ``status``, ``currency``, ``weight_pct``,
    ``cost_total``, ``realized_pnl``, ``market_value`` e ``unrealized_pnl``
    (gli ultimi due ``None`` quando non c'è un prezzo recente: si dichiara, non
    si stima).

    L'esposizione lorda è la somma dei ``weight_pct`` delle posizioni aperte —
    la stessa unità in cui ragiona già il PolicyEngine, e l'unica sommabile
    senza un tasso di cambio.
    """
    open_positions = [p for p in positions if str(p.get("status")) == "OPEN"]
    closed = [p for p in positions if str(p.get("status")) == "CLOSED"]
    stale = [p for p in positions if str(p.get("status")) == "STALE"]

    gross_exposure_pct = sum(_f(p.get("weight_pct")) or 0.0 for p in open_positions)

    by_currency: dict[str, dict[str, Any]] = {}
    for pos in positions:
        currency = str(pos.get("currency") or "?")
        bucket = by_currency.setdefault(
            currency,
            {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "cost_open": 0.0, "n_closed": 0, "n_open": 0,
             "unrealized_complete": True},
        )
        status = str(pos.get("status"))
        if status == "CLOSED":
            bucket["realized_pnl"] += _f(pos.get("realized_pnl")) or 0.0
            bucket["n_closed"] += 1
        elif status == "OPEN":
            bucket["n_open"] += 1
            bucket["cost_open"] += _f(pos.get("cost_total")) or 0.0
            unrealized = _f(pos.get("unrealized_pnl"))
            if unrealized is None:
                # Anche una sola posizione senza prezzo rende l'aggregato
                # parziale: lo si dichiara invece di far finta che sia completo.
                bucket["unrealized_complete"] = False
            else:
                bucket["unrealized_pnl"] += unrealized

    return {
        "n_open": len(open_positions),
        "n_closed": len(closed),
        "n_stale": len(stale),
        "gross_exposure_pct": round(gross_exposure_pct, 2),
        "by_currency": {
            cur: {
                "realized_pnl": round(v["realized_pnl"], 2),
                "unrealized_pnl": round(v["unrealized_pnl"], 2) if v["unrealized_complete"] else None,
                "cost_open": round(v["cost_open"], 2),
                "n_open": v["n_open"],
                "n_closed": v["n_closed"],
            }
            for cur, v in sorted(by_currency.items())
        },
    }


def _iso_week_key(symbol_id: Any, session: date | None) -> tuple[Any, int, int] | None:
    if not isinstance(session, date):
        return None
    iso = session.isocalendar()
    return (symbol_id, iso[0], iso[1])


def _dedupe_by_symbol_week(positions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Una posizione per (titolo, settimana ISO di apertura).

    Stessa difesa della pagina Performance contro la pseudo-replicazione: i
    preferiti rianalizzati più volte producono consigli fortemente correlati, e
    contarli come campioni indipendenti gonfia la numerosità senza aggiungere
    informazione. A parità di chiave vince la posizione aperta prima.
    """
    best: dict[tuple[Any, int, int], dict[str, Any]] = {}
    for pos in positions:
        key = _iso_week_key(pos.get("symbol_id"), pos.get("opened_session"))
        if key is None:
            continue
        current = best.get(key)
        if current is None:
            best[key] = pos
            continue
        if (pos.get("opened_session") or date.max) < (current.get("opened_session") or date.max):
            best[key] = pos
    return list(best.values())


def sim_stats(closed_positions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Statistiche di percorso sulle posizioni chiuse, dietro i cancelli di onestà.

    Ogni posizione è un dict con ``symbol_id``, ``opened_session``,
    ``close_reason``, ``realized_pnl_pct``, ``mae_pct``, ``mfe_pct``,
    ``exit_ambiguous``.

    Restituisce sempre i conteggi — così l'interfaccia può spiegare quanto manca
    — ma i tassi e le medie solo quando il campione supera ENTRAMBE le soglie
    (``SIM_STATS_MIN_N`` posizioni chiuse su ``SIM_STATS_MIN_SYMBOLS`` titoli
    diversi). È la lezione della metrica che oscillava: con n piccolo un tasso
    binario non misura la strategia, misura il denominatore.
    """
    deduped = _dedupe_by_symbol_week(closed_positions)
    n = len(deduped)
    symbols = {p.get("symbol_id") for p in deduped if p.get("symbol_id") is not None}
    n_symbols = len(symbols)

    reasons = Counter(
        str(p.get("close_reason")) for p in deduped if str(p.get("close_reason")) in _CLOSE_REASONS
    )
    out: dict[str, Any] = {
        "n": n,
        "n_raw": len(closed_positions),
        "n_symbols": n_symbols,
        "min_n": SIM_STATS_MIN_N,
        "min_symbols": SIM_STATS_MIN_SYMBOLS,
        "by_close_reason": {reason: reasons.get(reason, 0) for reason in _CLOSE_REASONS},
        "n_ambiguous": sum(1 for p in deduped if bool(p.get("exit_ambiguous"))),
        "status": "dati_insufficienti",
        "stop_hit_rate": None,
        "tp_hit_rate": None,
        "avg_mae_pct": None,
        "avg_mfe_pct": None,
        "avg_pnl_pct": None,
        "avg_pnl_pct_by_reason": {},
    }
    if n < SIM_STATS_MIN_N or n_symbols < SIM_STATS_MIN_SYMBOLS:
        return out

    out["status"] = "ok"
    out["stop_hit_rate"] = round(reasons.get("STOP_LOSS", 0) / n, 4)
    out["tp_hit_rate"] = round(reasons.get("TAKE_PROFIT", 0) / n, 4)

    def _mean(key: str, rows: Sequence[dict[str, Any]]) -> float | None:
        values = [v for v in (_f(r.get(key)) for r in rows) if v is not None]
        return round(sum(values) / len(values), 4) if values else None

    out["avg_mae_pct"] = _mean("mae_pct", deduped)
    out["avg_mfe_pct"] = _mean("mfe_pct", deduped)
    out["avg_pnl_pct"] = _mean("realized_pnl_pct", deduped)
    out["avg_pnl_pct_by_reason"] = {
        reason: value
        for reason in _CLOSE_REASONS
        if (
            value := _mean(
                "realized_pnl_pct",
                [p for p in deduped if str(p.get("close_reason")) == reason],
            )
        )
        is not None
    }
    return out


def compact_book_for_prompt(
    aggregates: dict[str, Any], positions: Sequence[dict[str, Any]], max_positions: int = 5
) -> dict[str, Any] | None:
    """Blocco compatto sullo STATO del libro da iniettare nei prompt.

    Volutamente contiene solo stato — esposizione, concentrazione, giorni di
    permanenza — e NESSUNA statistica di performance: dare a un modello la
    propria serie di vittorie è il modo più diretto per farlo ancorare a una
    striscia fortunata. Le performance arrivano all'LLM solo attraverso il coach
    settimanale, e solo se superano i cancelli di onestà.

    Restituisce ``None`` quando non c'è nessuna posizione aperta, così sul caso
    comune (libro vuoto) il prompt non paga un solo token in più.
    """
    open_positions = [p for p in positions if str(p.get("status")) == "OPEN"]
    if not open_positions:
        return None

    ranked = sorted(open_positions, key=lambda p: _f(p.get("weight_pct")) or 0.0, reverse=True)
    rows = []
    for pos in ranked[:max_positions]:
        row: dict[str, Any] = {"t": pos.get("ticker"), "w_pct": round(_f(pos.get("weight_pct")) or 0.0, 1)}
        days = pos.get("days_open")
        if isinstance(days, int):
            row["days"] = days
        pnl_pct = _f(pos.get("unrealized_pnl_pct"))
        if pnl_pct is not None:
            row["pnl_pct"] = round(pnl_pct, 1)
        rows.append(row)

    return {
        "n_open": aggregates.get("n_open"),
        "gross_exposure_pct": aggregates.get("gross_exposure_pct"),
        "positions": rows,
        "truncated": len(ranked) > max_positions or None,
    }
