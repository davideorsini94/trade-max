"""Offline tests for ``NewsService`` persistence and selection logic.

No network access: ``_persist``/`_latest_from_db`` are exercised directly with
hand-built entry dicts, exactly the shape ``_parse_feed`` would produce.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.data.news import NewsService


def _entry(title: str, url: str, published_at: datetime | None) -> dict:
    return {"title": title, "url": url, "summary": "", "published_at": published_at}


def test_persist_falls_back_to_now_when_feed_has_no_pubdate(
    db_session_factory: sessionmaker[Session],
) -> None:
    """A feed item with no pubDate/updated (e.g. ESMA) must not get published_at=NULL.

    NULL would be excluded forever by _latest_from_db's "isnot(None)" freshness
    filter, silently dropping the source from every future prompt.
    """
    db = db_session_factory()
    service = NewsService()
    entries = [_entry("ESMA statement", "https://esma.example/1", None)]

    service._persist(db, source_key="esma_press", category="MACRO", ticker=None, entries=entries)
    items = service._latest_from_db(db, category="MACRO", ticker=None)

    assert len(items) == 1
    assert items[0]["title"] == "ESMA statement"
    db.close()


def test_latest_from_db_never_fully_crowds_out_a_low_frequency_source(
    db_session_factory: sessionmaker[Session],
) -> None:
    """A single low-frequency item must survive a flood from one chatty source.

    20 fresh cnbc_top items vs. 1 fed_press item: without the per-source cap, a
    plain "most recent 15" would never include the lone official item. The cap
    guarantees it a slot even though the overall window still backfills up to
    15 from the only other source available (there's nothing else to fill with).
    """
    db = db_session_factory()
    service = NewsService()
    now = datetime.utcnow()

    chatty = [
        _entry(f"CNBC headline {i}", f"https://cnbc.example/{i}", now - timedelta(minutes=i))
        for i in range(20)
    ]
    service._persist(db, source_key="cnbc_top", category="MACRO", ticker=None, entries=chatty)
    official = [_entry("Fed statement", "https://fed.example/1", now - timedelta(hours=1))]
    service._persist(db, source_key="fed_press", category="MACRO", ticker=None, entries=official)

    items = service._latest_from_db(db, category="MACRO", ticker=None)

    assert len(items) == 15
    sources = {item["source"] for item in items}
    assert "Federal Reserve" in sources
    db.close()


def test_latest_from_db_caps_hold_when_supply_is_diverse_enough(
    db_session_factory: sessionmaker[Session],
) -> None:
    """With enough distinct sources, no single one exceeds the per-source cap.

    6 sources x 5 fresh items each (30 total, well above the 15-item window) means
    the cap alone already yields >=15 candidates, so the backfill path (which
    would legitimately let a dominant source exceed the cap when diversity is
    thin) never triggers here.
    """
    db = db_session_factory()
    service = NewsService()
    now = datetime.utcnow()

    for source_idx in range(6):
        entries = [
            _entry(
                f"source {source_idx} item {i}",
                f"https://source{source_idx}.example/{i}",
                now - timedelta(minutes=source_idx * 100 + i),
            )
            for i in range(5)
        ]
        service._persist(
            db, source_key=f"src{source_idx}", category="MACRO", ticker=None, entries=entries
        )

    items = service._latest_from_db(db, category="MACRO", ticker=None)
    counts: dict[str, int] = {}
    for item in items:
        counts[item["source"]] = counts.get(item["source"], 0) + 1

    assert len(items) == 15
    assert max(counts.values()) <= 3
    db.close()


def test_latest_from_db_backfills_past_cap_when_few_sources_active(
    db_session_factory: sessionmaker[Session],
) -> None:
    """The per-source cap must not shrink the result below 15 when only 1-2 sources exist."""
    db = db_session_factory()
    service = NewsService()
    now = datetime.utcnow()

    entries = [
        _entry(f"CNBC headline {i}", f"https://cnbc.example/only/{i}", now - timedelta(minutes=i))
        for i in range(20)
    ]
    service._persist(db, source_key="cnbc_top", category="MACRO", ticker=None, entries=entries)

    items = service._latest_from_db(db, category="MACRO", ticker=None)

    assert len(items) == 15
    db.close()
