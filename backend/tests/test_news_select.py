"""Test della selezione macro per rilevanza (blueprint §5.4 addendum).

Il fallimento che questi test inchiodano è stato misurato su dati reali. Alla
domanda "l'agente delle notizie indaga anche sulle varie guerre?" la risposta era
no, ma non per mancanza di fonti: nel database c'erano *"Iran launches surprise
ballistic missile attack on U.S. forces in the Middle East"* e *"Oil jumps as
U.S.-Iran resume strikes"*. La selezione ordinava per sola recenza con un tetto di
3 per fonte, e CNBC pubblicava 76 elementi su 120: i suoi tre posti andavano
all'ultimo quarto d'ora. I 15 titoli che l'agente vedeva davvero contenevano zero
elementi di conflitto, e al loro posto c'erano un pezzo sulla pianificazione
ereditaria e il "New Q&As available" di ESMA.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.data.news_select import (
    MAX_ITEMS_PER_SOURCE,
    TOPIC_QUOTAS,
    classify,
    score_item,
    select_macro_items,
)


class _Item:
    """Sostituto leggero di ``NewsItem``: la selezione legge solo questi attributi."""

    def __init__(
        self,
        title: str,
        *,
        source_key: str = "cnbc_top",
        summary: str = "",
        published_at: datetime | None = None,
        published_is_estimated: bool = False,
    ) -> None:
        self.title = title
        self.summary = summary
        self.source_key = source_key
        self.published_at = published_at or datetime(2026, 7, 29, 12, 0)
        self.published_is_estimated = published_is_estimated

    def __repr__(self) -> str:  # pragma: no cover - solo per leggere i fallimenti
        return f"<{self.source_key}: {self.title[:40]}>"


# --------------------------------------------------------------------------- #
# Classificazione
# --------------------------------------------------------------------------- #


def test_war_does_not_match_warning_or_warren() -> None:
    """Il falso positivo che ho commesso io contando i titoli di guerra.

    Una ricerca per sottostringa fa corrispondere "war" a "Warren, Schiff urge
    SEC..." e a "Warsh". Il confine di parola è ciò che lo impedisce.
    """
    for benign in (
        "Warren, Schiff urge SEC to probe Trump Media's paid service",
        "Fed's Warsh faces strong dissension",
        "Analysts issue a warning on software valuations",
        "New warehouse automation boosts margins",
    ):
        topic, _score = classify(benign)
        assert topic != "geopolitics_conflict", benign


def test_real_conflict_headlines_are_classified_as_geopolitics() -> None:
    for headline in (
        "Iran launches surprise ballistic missile attack on U.S. forces in the Middle East",
        "Senate to set up key vote on Russia sanctions",
        "Ukraine has found a new pressure point in Russia's wartime economy",
    ):
        topic, score = classify(headline)
        assert topic == "geopolitics_conflict", headline
        assert score > 0


def test_italian_keywords_are_classified() -> None:
    """Il Sole 24 Ore pubblica in italiano: una tassonomia solo inglese lo ignorerebbe."""
    topic, _ = classify("Guerra in Medio Oriente: il petrolio balza sopra i 90 dollari")
    assert topic in {"geopolitics_conflict", "energy_commodities"}
    topic, _ = classify("La Fed lascia i tassi di interesse invariati")
    assert topic == "monetary_policy"


def test_oil_and_energy_are_their_own_topic() -> None:
    topic, _ = classify("Brent oil jumps back above $90 after Trump threatens strikes")
    assert topic in {"energy_commodities", "geopolitics_conflict"}


def test_official_sources_score_higher_than_press_on_the_same_topic() -> None:
    """Un dato ufficiale pesa più del commento: la stessa gerarchia del prompt."""
    official = _Item("CPI for all items falls 0.4% in June", source_key="bls_cpi")
    press = _Item("What the CPI report means for your portfolio", source_key="cnbc_top")
    assert score_item(official)[1] > score_item(press)[1]


def test_untopical_items_get_no_topic() -> None:
    topic, _ = classify("I've seen families destroyed by this common estate-planning mistake")
    assert topic is None


# --------------------------------------------------------------------------- #
# Il fallimento reale, riprodotto
# --------------------------------------------------------------------------- #


def test_the_iran_missile_headline_survives_a_flood_of_fresh_cnbc_fluff() -> None:
    """La riproduzione esatta del bug: 20 elementi CNBC freschissimi e irrilevanti
    contro un attacco missilistico più vecchio. Prima l'attacco non entrava."""
    now = datetime(2026, 7, 29, 18, 0)
    rows = [
        _Item(
            f"Stocks making the biggest moves midday: ticker {i}",
            source_key="cnbc_top",
            published_at=now - timedelta(minutes=i),
        )
        for i in range(20)
    ]
    missile = _Item(
        "Iran launches surprise ballistic missile attack on U.S. forces in the Middle East",
        source_key="cnbc_top",
        published_at=now - timedelta(hours=9),  # molto più vecchio
    )
    rows.append(missile)

    selected = select_macro_items(rows, limit=15)
    assert missile in selected


def test_esma_boilerplate_no_longer_crowds_the_payload() -> None:
    """Quattro elementi ESMA senza data occupavano il payload perché il fallback
    li faceva sembrare appena pubblicati. Una data stimata non è freschezza."""
    now = datetime(2026, 7, 29, 16, 33)
    esma = [
        _Item(
            title,
            source_key="esma_press",
            published_at=now,
            published_is_estimated=True,
        )
        for title in (
            "New Q&As available",
            "ESMA publishes first market capitalisation data for EU Member States",
            "Financial firms keep EU carbon markets moving",
            "ESMA publishes technical standards on CCP admission criteria elements",
        )
    ]
    real = [
        _Item(
            "Iran launches ballistic missile attack on U.S. forces",
            source_key="bbc_world",
            published_at=now - timedelta(hours=4),
        ),
        _Item(
            "Brent oil jumps above $90 after strikes resume",
            source_key="cnbc_energy",
            published_at=now - timedelta(hours=5),
        ),
        _Item(
            "Divided Fed holds interest rates steady",
            source_key="fed_press",
            published_at=now - timedelta(hours=6),
        ),
    ]
    selected = select_macro_items(esma + real, limit=5)
    # Le notizie con data vera e materiali passano davanti al boilerplate.
    for item in real:
        assert item in selected
    assert sum(1 for i in selected if i.source_key == "esma_press") <= 2


# --------------------------------------------------------------------------- #
# Quote e tetti
# --------------------------------------------------------------------------- #


def test_geopolitics_gets_its_reserved_slots() -> None:
    now = datetime(2026, 7, 29, 12, 0)
    monetary = [
        _Item(f"Fed official signals rate cut number {i}", source_key="cnbc_top",
              published_at=now - timedelta(minutes=i))
        for i in range(20)
    ]
    conflict = [
        _Item(f"Missile strike escalation report {i}", source_key="bbc_world",
              published_at=now - timedelta(hours=6 + i))
        for i in range(4)
    ]
    selected = select_macro_items(monetary + conflict, limit=15)
    n_conflict = sum(1 for i in selected if i in conflict)
    assert n_conflict >= min(TOPIC_QUOTAS["geopolitics_conflict"], len(conflict))


def test_no_single_source_exceeds_its_cap_when_enough_sources_are_available() -> None:
    """Il caso che il bug reale rappresentava: un editore ad alto volume che
    monopolizzava il payload mentre c'era abbondanza di alternative."""
    now = datetime(2026, 7, 29, 12, 0)
    hog = [
        _Item(f"Sanctions and missile escalation update {i}", source_key="cnbc_top",
              published_at=now - timedelta(minutes=i))
        for i in range(30)
    ]
    others = [
        _Item(f"Rate decision detail {i}", source_key=f"src{i}",
              published_at=now - timedelta(hours=1 + i))
        for i in range(12)
    ]
    selected = select_macro_items(hog + others, limit=15)
    assert len(selected) == 15
    assert sum(1 for i in selected if i.source_key == "cnbc_top") <= MAX_ITEMS_PER_SOURCE


def test_source_cap_yields_rather_than_shrink_the_payload() -> None:
    """Compromesso dichiarato: con poche fonti attive il tetto è aritmeticamente
    impossibile (2 fonti x 3 = 6 < 15), quindi la terza fase lo lascia cadere
    invece di consegnare un payload mezzo vuoto — come faceva il codice
    precedente. Il tetto serve alla diversità quando la diversità esiste."""
    now = datetime(2026, 7, 29, 12, 0)
    rows = [
        _Item(f"Sanctions escalation update {i}", source_key="cnbc_top",
              published_at=now - timedelta(minutes=i))
        for i in range(10)
    ] + [
        _Item(f"ECB rate decision detail {i}", source_key="ecb_press",
              published_at=now - timedelta(hours=1 + i))
        for i in range(10)
    ]
    selected = select_macro_items(rows, limit=15)
    assert len(selected) == 15
    assert sum(1 for i in selected if i.source_key == "cnbc_top") > MAX_ITEMS_PER_SOURCE


def test_thin_day_still_fills_the_payload_dropping_the_source_cap() -> None:
    """Nei giorni poveri di notizie il comportamento precedente era corretto:
    meglio 15 elementi da una fonte sola che 3 e un payload mezzo vuoto."""
    now = datetime(2026, 7, 29, 12, 0)
    rows = [
        _Item(f"Some market update {i}", source_key="cnbc_top",
              published_at=now - timedelta(minutes=i))
        for i in range(20)
    ]
    selected = select_macro_items(rows, limit=15)
    assert len(selected) == 15


def test_output_is_capped_and_chronological() -> None:
    now = datetime(2026, 7, 29, 12, 0)
    rows = [
        _Item(f"Item {i}", source_key=f"src{i % 7}", published_at=now - timedelta(hours=i))
        for i in range(40)
    ]
    selected = select_macro_items(rows, limit=15)
    assert len(selected) == 15
    dates = [i.published_at for i in selected]
    assert dates == sorted(dates, reverse=True)


def test_no_duplicates_across_phases() -> None:
    now = datetime(2026, 7, 29, 12, 0)
    rows = [
        _Item("Missile strike and sanctions escalate", source_key="bbc_world", published_at=now),
        _Item("Fed cuts rates", source_key="fed_press", published_at=now),
        _Item("Oil surges on OPEC decision", source_key="cnbc_energy", published_at=now),
    ]
    selected = select_macro_items(rows, limit=15)
    assert len(selected) == len(set(id(i) for i in selected)) == 3


def test_empty_and_degenerate_inputs_degrade_cleanly() -> None:
    assert select_macro_items([], limit=15) == []
    assert select_macro_items([_Item("x")], limit=0) == []


def test_selection_is_deterministic_under_ties() -> None:
    """Stesso input, stesso output: nessuna dipendenza dall'ordine di un set."""
    now = datetime(2026, 7, 29, 12, 0)
    rows = [
        _Item(f"Tied headline {i}", source_key=f"src{i % 4}", published_at=now)
        for i in range(30)
    ]
    first = [i.title for i in select_macro_items(rows, limit=15)]
    for _ in range(3):
        assert [i.title for i in select_macro_items(rows, limit=15)] == first


def test_quotas_do_not_waste_slots_on_absent_topics() -> None:
    """Un tema senza candidati non deve lasciare posti vuoti."""
    now = datetime(2026, 7, 29, 12, 0)
    rows = [
        _Item(f"Missile and sanctions escalation {i}", source_key=f"src{i}",
              published_at=now - timedelta(hours=i))
        for i in range(15)
    ]
    selected = select_macro_items(rows, limit=15)
    assert len(selected) == 15


def test_quota_total_leaves_room_for_free_slots() -> None:
    """Invariante di progetto: le quote non devono saturare il payload."""
    assert sum(TOPIC_QUOTAS.values()) < 15
