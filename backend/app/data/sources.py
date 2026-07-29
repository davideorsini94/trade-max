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
    "bls_cpi": {
        "name": "US BLS — Consumer Price Index",
        "url": "https://www.bls.gov/feed/cpi.rss",
        "lang": "en",
    },
    "bls_empsit": {
        "name": "US BLS — Employment Situation",
        "url": "https://www.bls.gov/feed/empsit.rss",
        "lang": "en",
    },
    "esma_press": {
        "name": "ESMA",
        "url": "https://www.esma.europa.eu/rss.xml",
        "lang": "en",
    },
    # --- Geopolitica, conflitti e sicurezza energetica ---------------------- #
    # Aggiunte perché la whitelist non aveva UNA fonte dedicata alla geopolitica:
    # le notizie di guerra arrivavano solo di rimbalzo da CNBC/MarketWatch, e la
    # selezione per sola recenza le faceva sparire dal payload (vedi
    # ``app.data.news_select``). Ogni URL qui sotto è stato verificato prima di
    # essere aggiunto: si scarica, feedparser lo analizza, almeno 3 voci, almeno
    # l'80% con data e la più recente sotto i 30 giorni. Il controllo
    # sull'anzianità è quello che ha bocciato in passato ``bls_latest.rss``, un
    # singolo elemento perpetuo che non si aggiornava mai.
    # Bocciati in verifica e volutamente NON inclusi: Reuters (RSS pubblico
    # dismesso), NATO e IEA (404), OPEC, Consiglio UE e IMF (403), Banca Mondiale
    # (nessuna voce). Scartati per scelta editoriale: Al Jazeera (sbilancerebbe
    # il payload verso il commento sui conflitti) e CNBC World (darebbe a un solo
    # editore tre feed).
    "bbc_world": {
        "name": "BBC News — Mondo",
        "url": "http://feeds.bbci.co.uk/news/world/rss.xml",
        "lang": "en",
    },
    "un_news": {
        "name": "Nazioni Unite",
        "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
        "lang": "en",
    },
    "sole24_mondo": {
        "name": "Il Sole 24 Ore Mondo",
        "url": "https://www.ilsole24ore.com/rss/mondo.xml",
        "lang": "it",
    },
    # Il canale di trasmissione principale da un conflitto ai mercati: l'energia.
    "cnbc_energy": {
        "name": "CNBC Energia",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19836768",
        "lang": "en",
    },
    "eia_today": {
        "name": "US EIA — Today in Energy",
        "url": "https://www.eia.gov/rss/todayinenergy.xml",
        "lang": "en",
    },
    # Le sanzioni UE si annunciano qui: il canale geopolitico più materiale per
    # un titolo quotato in Europa.
    "ec_daily": {
        "name": "Commissione Europea",
        "url": "https://ec.europa.eu/commission/presscorner/api/rss?language=en",
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
