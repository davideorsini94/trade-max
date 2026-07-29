"""Selezione per RILEVANZA dei titoli macro (blueprint §5.4 addendum).

Il problema che questo modulo risolve, misurato sui dati reali
--------------------------------------------------------------
La domanda era: "l'agente che guarda le notizie indaga anche sulle varie guerre
che ci sono?". La risposta era no, e la causa non erano le fonti: era la
selezione. Il prompt dell'agente macro elenca già "geopolitics" fra i canali di
trasmissione, e nel database c'erano titoli come *"Iran launches surprise
ballistic missile attack on U.S. forces in the Middle East"* e *"Oil jumps as
U.S.-Iran resume strikes"*. Ma la selezione ordinava per sola RECENZA con un
tetto di 3 elementi per fonte, e CNBC pubblica ~76 elementi su 120: i suoi 3
posti finivano a quello che era uscito negli ultimi dieci minuti. Riproducendo la
selezione di quel giorno, i 15 titoli che l'agente vedeva davvero contenevano
ZERO elementi di conflitto — al loro posto c'erano *"I've seen families destroyed
by this common estate-planning mistake"* e *"New Q&As available"* di ESMA, mentre
l'attacco missilistico restava inutilizzato nel database.

La soluzione: quote per tema
----------------------------
Nessun elemento in più nel payload — resta a 15, perché il costo in token è un
vincolo fisso del progetto. Cambia QUALI 15. Cinque temi si riservano dei posti,
così nessun tema può essere spinto fuori dal volume di un editore, e dentro ogni
tema vince la materialità e non l'orologio. Tutto deterministico: zero chiamate
LLM, zero costo aggiuntivo.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Sequence

#: Temi, in ordine fisso di priorità (l'ordine rompe i pareggi in modo
#: deterministico). Le parole chiave sono in inglese E in italiano: Il Sole 24
#: Ore pubblica in italiano, e una tassonomia solo inglese lo renderebbe
#: invisibile alla classificazione.
#:
#: Le voci di una sola parola vengono confrontate sui confini di parola, quindi
#: "war" non corrisponde a "warning" e nemmeno a "Warren" — un errore che una
#: ricerca per sottostringa commette subito, come ho verificato di persona
#: contando "Warren, Schiff urge SEC..." fra i titoli di guerra.
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "monetary_policy": (
        "federal reserve", "fed", "fomc", "ecb", "bce", "central bank", "banca centrale",
        "interest rate", "tasso", "tassi", "rate cut", "rate hike", "basis point",
        "monetary policy", "politica monetaria", "quantitative", "bond yield", "rendimento",
        "treasury", "bund", "btp", "spread",
    ),
    "inflation_labor": (
        "inflation", "inflazione", "cpi", "ppi", "deflation", "disinflation",
        "unemployment", "disoccupazione", "payroll", "jobs report", "labor market",
        "mercato del lavoro", "occupazione", "wage", "salari", "gdp", "pil",
        "recession", "recessione", "consumer price",
    ),
    "geopolitics_conflict": (
        "war", "guerra", "guerre", "conflict", "conflitto", "missile", "missili",
        "airstrike", "air strike", "invasion", "invasione", "ceasefire", "tregua",
        "cessate il fuoco", "escalation", "escalate", "sanction", "sanctions",
        "sanzioni", "embargo", "tariff", "tariffs", "dazi", "military", "militare",
        "troops", "truppe", "nato", "ukraine", "ucraina", "russia", "russian",
        "iran", "iranian", "israel", "israele", "gaza", "hamas", "hezbollah",
        "houthi", "taiwan", "north korea", "corea del nord", "middle east",
        "medio oriente", "geopolit", "drone", "droni", "blockade", "blocco navale",
        "mobilization", "mobilitazione", "coup", "terror",
    ),
    "energy_commodities": (
        "oil", "petrolio", "brent", "wti", "crude", "greggio", "opec", "gas",
        "lng", "pipeline", "gasdotto", "energy", "energia", "electricity",
        "elettricit", "power grid", "refinery", "raffineria", "gold", "oro",
        "copper", "rame", "wheat", "grano", "commodity", "materie prime",
        "uranium", "coal", "carbone", "strait of hormuz", "hormuz",
    ),
    "regulation_markets": (
        "regulat", "regolament", "esma", "sec ", "supervis", "vigilanza",
        "compliance", "antitrust", "capital requirement", "basel", "mifid",
        "disclosure", "guideline", "linee guida", "consultation", "consultazione",
        "technical standard", "enforcement", "fine", "sanzione amministrativa",
    ),
}

#: Parole che alzano il punteggio a prescindere dal tema: segnalano un evento con
#: una trasmissione ai mercati concreta, non un commento.
MATERIALITY_KEYWORDS: tuple[str, ...] = (
    "missile", "airstrike", "air strike", "invasion", "invasione", "sanction",
    "sanzioni", "embargo", "ceasefire", "tregua", "escalation", "opec",
    "rate cut", "rate hike", "taglio dei tassi", "cpi", "inflation", "inflazione",
    "unemployment", "disoccupazione", "payroll", "recession", "recessione",
    "default", "downgrade", "blockade", "pipeline", "shortage", "carenza",
    "surge", "plunge", "crolla", "balza", "record",
)

#: Fonti ufficiali/istituzionali: un dato statistico o una decisione di una banca
#: centrale pesa più del commento della stampa finanziaria sullo stesso tema.
#: È la stessa gerarchia che il prompt dell'agente macro già dichiara.
OFFICIAL_SOURCE_KEYS: frozenset[str] = frozenset(
    {
        "fed_press", "ecb_press", "boe_press", "bls_cpi", "bls_empsit",
        "esma_press", "eia_today", "ec_daily", "un_news",
    }
)

#: Posti riservati per tema. Sommano a 12 sui 15 totali: i 3 rimanenti vanno a
#: chi ha il punteggio più alto, qualunque tema abbia. Una quota si applica solo
#: se ci sono candidati per quel tema: un tema senza notizie non spreca posti.
TOPIC_QUOTAS: dict[str, int] = {
    "monetary_policy": 3,
    "inflation_labor": 2,
    "geopolitics_conflict": 3,
    "energy_commodities": 2,
    "regulation_markets": 2,
}

#: Tetto per fonte, invariato rispetto a prima: impedisce che un solo editore
#: occupi il payload anche quando i suoi articoli sono a tema.
MAX_ITEMS_PER_SOURCE: int = 3

_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _matches(text: str, keyword: str) -> bool:
    """Confronto con confini di parola per i termini singoli, sottostringa per le frasi.

    Senza il confine di parola "war" corrisponde a "warning", "Warren" e
    "warehouse": tre falsi positivi che ho contato davvero fra i titoli
    classificati come conflitto.
    """
    if " " in keyword:
        return keyword in text
    pattern = _WORD_RE_CACHE.get(keyword)
    if pattern is None:
        pattern = re.compile(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])")
        _WORD_RE_CACHE[keyword] = pattern
    return pattern.search(text) is not None


def _count_hits(text: str, keywords: Iterable[str]) -> int:
    return sum(1 for keyword in keywords if _matches(text, keyword))


def classify(title: str, summary: str = "") -> tuple[str | None, int]:
    """Tema dominante e punteggio di materialità di un elemento.

    Punteggio = 2x le corrispondenze nel titolo + 1x quelle nel sommario (ognuna
    limitata a 3, così un elenco di parole chiave non può gonfiare il totale)
    + 2 se contiene una parola di materialità + 1 se la fonte è ufficiale (il
    bonus fonte lo aggiunge :func:`score_item`, che conosce la fonte).

    Il tema è quello con più corrispondenze nel titolo+sommario; a pari merito
    vince l'ordine fisso di ``TOPIC_KEYWORDS``. ``None`` se nessun tema
    corrisponde: l'elemento resta comunque selezionabile, ma solo dai posti
    liberi.
    """
    text_title = (title or "").lower()
    text_all = f"{text_title} {(summary or '').lower()}"

    best_topic: str | None = None
    best_hits = 0
    score = 0
    for topic, keywords in TOPIC_KEYWORDS.items():
        title_hits = min(3, _count_hits(text_title, keywords))
        summary_hits = min(3, _count_hits((summary or "").lower(), keywords))
        total_hits = title_hits + summary_hits
        if total_hits > best_hits:
            best_hits = total_hits
            best_topic = topic
        score = max(score, 2 * title_hits + summary_hits)

    if _count_hits(text_all, MATERIALITY_KEYWORDS) > 0:
        score += 2
    return best_topic, score


def score_item(item: Any) -> tuple[str | None, int]:
    """``classify`` più il bonus per fonte ufficiale."""
    topic, score = classify(getattr(item, "title", "") or "", getattr(item, "summary", "") or "")
    if getattr(item, "source_key", None) in OFFICIAL_SOURCE_KEYS:
        score += 1
    return topic, score


def _sort_key(entry: tuple[Any, str | None, int]) -> tuple:
    """Ordinamento: punteggio decrescente, poi data decrescente.

    Una data STIMATA (``published_is_estimated``) non è prova di freschezza: è il
    momento in cui l'abbiamo scaricata, perché il feed non ne pubblicava una.
    Trattarla come recente è ciò che ha portato quattro elementi ESMA di
    boilerplate nel payload. A pari punteggio, quindi, gli elementi con data vera
    passano davanti.
    """
    item, _topic, score = entry
    estimated = bool(getattr(item, "published_is_estimated", False))
    published = getattr(item, "published_at", None) or datetime.min
    return (-score, 1 if estimated else 0, -published.timestamp())


def select_macro_items(rows: Sequence[Any], limit: int = 15) -> list[Any]:
    """Sceglie ``limit`` elementi macro bilanciati per tema.

    ``rows`` sono righe ``NewsItem`` (o qualunque oggetto con ``title``,
    ``summary``, ``source_key``, ``published_at``, ``published_is_estimated``),
    già filtrate per categoria e finestra di freschezza dal chiamante.

    Tre fasi:

    1. **Quote per tema**, nell'ordine fisso di ``TOPIC_QUOTAS``, rispettando il
       tetto per fonte. È la fase che garantisce a un attacco missilistico un
       posto che il volume di CNBC non può togliergli.
    2. **Posti liberi** ai migliori rimanenti per punteggio, tetto per fonte
       ancora attivo.
    3. **Riempimento nei giorni magri**: se il totale non è raggiunto perché
       poche fonti sono attive, si completa per recenza lasciando cadere il tetto
       — esattamente il comportamento precedente, che nei giorni poveri di
       notizie era corretto.

    L'uscita è riordinata per data decrescente: l'agente legge una cronologia,
    non una classifica.
    """
    if limit <= 0:
        return []

    scored = [(row, *score_item(row)) for row in rows]
    scored.sort(key=_sort_key)

    selected: list[Any] = []
    chosen_ids: set[int] = set()
    per_source: dict[str, int] = {}

    def _take(entry: tuple[Any, str | None, int], *, respect_source_cap: bool) -> bool:
        item = entry[0]
        if id(item) in chosen_ids or len(selected) >= limit:
            return False
        source = getattr(item, "source_key", "") or ""
        if respect_source_cap and per_source.get(source, 0) >= MAX_ITEMS_PER_SOURCE:
            return False
        selected.append(item)
        chosen_ids.add(id(item))
        per_source[source] = per_source.get(source, 0) + 1
        return True

    # Fase 1 — quote per tema.
    for topic, quota in TOPIC_QUOTAS.items():
        taken = 0
        for entry in scored:
            if taken >= quota or len(selected) >= limit:
                break
            if entry[1] != topic:
                continue
            if _take(entry, respect_source_cap=True):
                taken += 1

    # Fase 2 — posti liberi ai migliori rimanenti.
    for entry in scored:
        if len(selected) >= limit:
            break
        _take(entry, respect_source_cap=True)

    # Fase 3 — giorni magri: si completa per recenza, tetto per fonte a parte.
    if len(selected) < limit:
        for entry in scored:
            if len(selected) >= limit:
                break
            _take(entry, respect_source_cap=False)

    selected.sort(
        key=lambda item: getattr(item, "published_at", None) or datetime.min, reverse=True
    )
    return selected
