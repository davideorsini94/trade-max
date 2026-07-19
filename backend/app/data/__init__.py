"""Data layer: market data, technical indicators and curated news feeds.

Sibling packages (``app.agents``, ``app.engine``) consume this package through:

- ``app.data.sources``: static whitelists of external feeds (no network I/O).
- ``app.data.market.MarketDataService``: yfinance-backed quotes, history,
  fundamentals and symbol search/validation. All network/pandas work is
  synchronous by design and must be called via ``asyncio.to_thread`` from
  async call sites (yfinance itself is a blocking library).
- ``app.data.indicators``: pure pandas functions over OHLCV DataFrames
  (no I/O, fully deterministic, unit-testable without network access).
- ``app.data.news.NewsService``: feedparser-backed macro/corporate news
  fetching with DB-backed de-duplication and an in-memory TTL cache. Also
  synchronous; call via ``asyncio.to_thread``.
"""

from __future__ import annotations
