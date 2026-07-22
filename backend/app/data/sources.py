"""Curated whitelist of external data sources (blueprint section 5.6).

These are the ONLY feeds the application ever reads news from. No URL is ever
constructed from LLM output or user input beyond substituting a validated
ticker into the ``CORPORATE_SOURCES`` templates below. Keeping this list in one
module makes the whitelist auditable and easy to review.
"""

from __future__ import annotations

from typing import Final, TypedDict


class MacroSource(TypedDict):
    """Static metadata for a single macro news RSS/Atom feed."""

    name: str
    url: str
    lang: str


# Macro/geopolitical/central-bank feeds, read as-is (no per-ticker templating).
MACRO_SOURCES: Final[dict[str, MacroSource]] = {
    "fed_press": {
        "name": "Federal Reserve",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "lang": "en",
    },
    "ecb_press": {
        "name": "BCE",
        "url": "https://www.ecb.europa.eu/rss/press.html",
        "lang": "en",
    },
    "cnbc_top": {
        "name": "CNBC Top News",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "lang": "en",
    },
    "cnbc_econ": {
        "name": "CNBC Economy",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
        "lang": "en",
    },
    "mw_top": {
        "name": "MarketWatch",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "lang": "en",
    },
    "sole24_fin": {
        "name": "Il Sole 24 Ore Finanza",
        "url": "https://www.ilsole24ore.com/rss/finanza--mercati.xml",
        "lang": "it",
    },
    "boe_press": {
        "name": "Bank of England",
        "url": "https://www.bankofengland.co.uk/rss/news",
        "lang": "en",
    },
    "bls_latest": {
        "name": "US Bureau of Labor Statistics",
        "url": "https://www.bls.gov/feed/bls_latest.rss",
        "lang": "en",
    },
    "esma_press": {
        "name": "ESMA",
        "url": "https://www.esma.europa.eu/rss.xml",
        "lang": "en",
    },
}

# Per-ticker feed URL templates. ``{ticker}`` is substituted with the symbol's
# validated ticker string (already upper-cased / stripped) before fetching.
CORPORATE_SOURCES: Final[dict[str, str]] = {
    "yahoo_sym": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
    "sec_edgar": (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}"
        "&type=8-K&dateb=&owner=include&count=10&output=atom"
    ),
    "cnbc_biz": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
}

# SEC EDGAR requires a descriptive User-Agent identifying the requester
# (https://www.sec.gov/os/webmaster-faq#developers). Used by NewsService for
# the "sec_edgar" corporate source specifically.
SEC_EDGAR_USER_AGENT: Final[str] = "trade-max/1.0 davide.orsini@promedital.it"
