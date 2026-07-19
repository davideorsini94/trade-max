"""Curated stock universe ("Universo titoli") + market-stats refresh.

Backs the Mercato page: a hand-picked list of well-known stocks (US mega/large
caps, top European names and the FTSE MIB constituents) enriched with real
market data pulled in bulk from yfinance and turned into a handful of
deterministic ranking scores (fame / positive trend / value / reliability and a
weighted composite).

Design notes
------------
* ``UNIVERSE`` is *static curated metadata*. ``market_cap_bn`` in particular is
  an **indicative baseline** (approximate 2025/2026 order-of-magnitude values in
  billions USD) used only to rank by "value" and size — it is deliberately NOT
  live data and is never refreshed from the network.
* ``compute_reliability`` / ``compute_composite`` are pure, deterministic
  functions (fully unit-tested) so the ranking is reproducible.
* ``refresh_universe`` is a **synchronous** function (yfinance is blocking):
  callers on the event loop wrap it in ``asyncio.to_thread`` (see
  ``app.api.universe`` and ``app.scheduler``). A module-level ``threading.Lock``
  makes concurrent refreshes no-ops (the second caller returns 0 immediately).
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime

import pandas as pd
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.indicators import volatility
from app.data.market import MarketDataService
from app.models import UniverseStat

logger = logging.getLogger(__name__)

# yfinance ``download`` is issued once per chunk of tickers (one HTTP request per
# chunk) to stay well under provider limits while keeping the total number of
# requests small.
_CHUNK_SIZE = 50

# Number of trading days used for the ~1-month (30 calendar day) trend and for
# the long-term SMA200 healthy-trend check.
_TRADING_DAYS_1M = 21
_SMA_LONG_WINDOW = 200

# Guards ``refresh_universe`` against concurrent execution (scheduler job vs a
# manual POST /api/universe/refresh, or two manual calls). Acquired non-blocking.
_refresh_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Curated universe (static metadata)
# --------------------------------------------------------------------------- #
#
# fame_rank: 5 = global icon, 4 = very well known, 3 = known, 2 = investor-known,
#            1 = niche.
# market_cap_bn: indicative baseline in billions USD (approx 2025/2026), used as
#                a *ranking* input only — NOT live data.
UNIVERSE: list[dict] = [
    # --- US technology / Nasdaq-100 core -------------------------------------
    {"ticker": "AAPL", "name": "Apple", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 5, "market_cap_bn": 3400.0},
    {"ticker": "MSFT", "name": "Microsoft", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 5, "market_cap_bn": 3200.0},
    {"ticker": "NVDA", "name": "NVIDIA", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 5, "market_cap_bn": 3300.0},
    {"ticker": "GOOGL", "name": "Alphabet", "exchange": "NASDAQ", "country": "US", "sector": "Communication Services", "currency": "USD", "fame_rank": 5, "market_cap_bn": 2200.0},
    {"ticker": "AMZN", "name": "Amazon", "exchange": "NASDAQ", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 5, "market_cap_bn": 2300.0},
    {"ticker": "META", "name": "Meta Platforms", "exchange": "NASDAQ", "country": "US", "sector": "Communication Services", "currency": "USD", "fame_rank": 5, "market_cap_bn": 1500.0},
    {"ticker": "TSLA", "name": "Tesla", "exchange": "NASDAQ", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 5, "market_cap_bn": 1000.0},
    {"ticker": "AVGO", "name": "Broadcom", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 1100.0},
    {"ticker": "NFLX", "name": "Netflix", "exchange": "NASDAQ", "country": "US", "sector": "Communication Services", "currency": "USD", "fame_rank": 5, "market_cap_bn": 400.0},
    {"ticker": "AMD", "name": "Advanced Micro Devices", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 250.0},
    {"ticker": "INTC", "name": "Intel", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 120.0},
    {"ticker": "QCOM", "name": "Qualcomm", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 180.0},
    {"ticker": "TXN", "name": "Texas Instruments", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 180.0},
    {"ticker": "ADBE", "name": "Adobe", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 230.0},
    {"ticker": "CRM", "name": "Salesforce", "exchange": "NYSE", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 320.0},
    {"ticker": "ORCL", "name": "Oracle", "exchange": "NYSE", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 500.0},
    {"ticker": "CSCO", "name": "Cisco Systems", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 240.0},
    {"ticker": "IBM", "name": "IBM", "exchange": "NYSE", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 200.0},
    {"ticker": "PLTR", "name": "Palantir Technologies", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 200.0},
    {"ticker": "UBER", "name": "Uber Technologies", "exchange": "NYSE", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 150.0},
    {"ticker": "ABNB", "name": "Airbnb", "exchange": "NASDAQ", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 4, "market_cap_bn": 90.0},
    {"ticker": "PYPL", "name": "PayPal Holdings", "exchange": "NASDAQ", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 80.0},
    {"ticker": "SHOP", "name": "Shopify", "exchange": "NYSE", "country": "CA", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 130.0},
    {"ticker": "COIN", "name": "Coinbase Global", "exchange": "NASDAQ", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 60.0},
    {"ticker": "MU", "name": "Micron Technology", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 110.0},
    {"ticker": "AMAT", "name": "Applied Materials", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 150.0},
    {"ticker": "LRCX", "name": "Lam Research", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 2, "market_cap_bn": 100.0},
    {"ticker": "ADI", "name": "Analog Devices", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 2, "market_cap_bn": 110.0},
    {"ticker": "INTU", "name": "Intuit", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 180.0},
    {"ticker": "NOW", "name": "ServiceNow", "exchange": "NYSE", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 200.0},
    {"ticker": "SNOW", "name": "Snowflake", "exchange": "NYSE", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 55.0},
    {"ticker": "MRVL", "name": "Marvell Technology", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 2, "market_cap_bn": 90.0},
    {"ticker": "PANW", "name": "Palo Alto Networks", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 120.0},
    {"ticker": "CRWD", "name": "CrowdStrike Holdings", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 90.0},
    {"ticker": "MSTR", "name": "Strategy (MicroStrategy)", "exchange": "NASDAQ", "country": "US", "sector": "Technology", "currency": "USD", "fame_rank": 3, "market_cap_bn": 90.0},
    # --- US communication / media --------------------------------------------
    {"ticker": "DIS", "name": "Walt Disney", "exchange": "NYSE", "country": "US", "sector": "Communication Services", "currency": "USD", "fame_rank": 5, "market_cap_bn": 200.0},
    {"ticker": "CMCSA", "name": "Comcast", "exchange": "NASDAQ", "country": "US", "sector": "Communication Services", "currency": "USD", "fame_rank": 3, "market_cap_bn": 150.0},
    {"ticker": "T", "name": "AT&T", "exchange": "NYSE", "country": "US", "sector": "Communication Services", "currency": "USD", "fame_rank": 4, "market_cap_bn": 160.0},
    {"ticker": "VZ", "name": "Verizon Communications", "exchange": "NYSE", "country": "US", "sector": "Communication Services", "currency": "USD", "fame_rank": 4, "market_cap_bn": 180.0},
    {"ticker": "TMUS", "name": "T-Mobile US", "exchange": "NASDAQ", "country": "US", "sector": "Communication Services", "currency": "USD", "fame_rank": 3, "market_cap_bn": 250.0},
    # --- US consumer ----------------------------------------------------------
    {"ticker": "WMT", "name": "Walmart", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 5, "market_cap_bn": 750.0},
    {"ticker": "COST", "name": "Costco Wholesale", "exchange": "NASDAQ", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 4, "market_cap_bn": 400.0},
    {"ticker": "PG", "name": "Procter & Gamble", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 4, "market_cap_bn": 400.0},
    {"ticker": "KO", "name": "Coca-Cola", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 5, "market_cap_bn": 280.0},
    {"ticker": "PEP", "name": "PepsiCo", "exchange": "NASDAQ", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 4, "market_cap_bn": 230.0},
    {"ticker": "MCD", "name": "McDonald's", "exchange": "NYSE", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 5, "market_cap_bn": 210.0},
    {"ticker": "SBUX", "name": "Starbucks", "exchange": "NASDAQ", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 4, "market_cap_bn": 110.0},
    {"ticker": "NKE", "name": "Nike", "exchange": "NYSE", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 5, "market_cap_bn": 110.0},
    {"ticker": "HD", "name": "Home Depot", "exchange": "NYSE", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 4, "market_cap_bn": 400.0},
    {"ticker": "LOW", "name": "Lowe's", "exchange": "NYSE", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 3, "market_cap_bn": 140.0},
    {"ticker": "TGT", "name": "Target", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 3, "market_cap_bn": 65.0},
    {"ticker": "MDLZ", "name": "Mondelez International", "exchange": "NASDAQ", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 3, "market_cap_bn": 90.0},
    {"ticker": "CL", "name": "Colgate-Palmolive", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 3, "market_cap_bn": 75.0},
    {"ticker": "PM", "name": "Philip Morris International", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 3, "market_cap_bn": 180.0},
    {"ticker": "MO", "name": "Altria Group", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 3, "market_cap_bn": 90.0},
    {"ticker": "KHC", "name": "Kraft Heinz", "exchange": "NASDAQ", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 3, "market_cap_bn": 40.0},
    {"ticker": "MNST", "name": "Monster Beverage", "exchange": "NASDAQ", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 2, "market_cap_bn": 55.0},
    {"ticker": "KMB", "name": "Kimberly-Clark", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 2, "market_cap_bn": 45.0},
    {"ticker": "EL", "name": "Estée Lauder", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 3, "market_cap_bn": 30.0},
    {"ticker": "GIS", "name": "General Mills", "exchange": "NYSE", "country": "US", "sector": "Consumer Staples", "currency": "USD", "fame_rank": 2, "market_cap_bn": 40.0},
    # --- US financials --------------------------------------------------------
    {"ticker": "BRK-B", "name": "Berkshire Hathaway", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 5, "market_cap_bn": 1000.0},
    {"ticker": "JPM", "name": "JPMorgan Chase", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 5, "market_cap_bn": 700.0},
    {"ticker": "BAC", "name": "Bank of America", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 350.0},
    {"ticker": "WFC", "name": "Wells Fargo", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 240.0},
    {"ticker": "GS", "name": "Goldman Sachs", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 180.0},
    {"ticker": "MS", "name": "Morgan Stanley", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 190.0},
    {"ticker": "C", "name": "Citigroup", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 130.0},
    {"ticker": "V", "name": "Visa", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 5, "market_cap_bn": 600.0},
    {"ticker": "MA", "name": "Mastercard", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 5, "market_cap_bn": 480.0},
    {"ticker": "AXP", "name": "American Express", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 210.0},
    {"ticker": "BLK", "name": "BlackRock", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 160.0},
    {"ticker": "SCHW", "name": "Charles Schwab", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 140.0},
    {"ticker": "SPGI", "name": "S&P Global", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 160.0},
    {"ticker": "BX", "name": "Blackstone", "exchange": "NYSE", "country": "US", "sector": "Financials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 200.0},
    # --- US health care -------------------------------------------------------
    {"ticker": "UNH", "name": "UnitedHealth Group", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 4, "market_cap_bn": 500.0},
    {"ticker": "JNJ", "name": "Johnson & Johnson", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 5, "market_cap_bn": 380.0},
    {"ticker": "LLY", "name": "Eli Lilly", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 5, "market_cap_bn": 800.0},
    {"ticker": "PFE", "name": "Pfizer", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 4, "market_cap_bn": 150.0},
    {"ticker": "MRK", "name": "Merck & Co.", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 4, "market_cap_bn": 250.0},
    {"ticker": "ABBV", "name": "AbbVie", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 4, "market_cap_bn": 330.0},
    {"ticker": "TMO", "name": "Thermo Fisher Scientific", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 3, "market_cap_bn": 200.0},
    {"ticker": "ABT", "name": "Abbott Laboratories", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 3, "market_cap_bn": 200.0},
    {"ticker": "DHR", "name": "Danaher", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 3, "market_cap_bn": 170.0},
    {"ticker": "BMY", "name": "Bristol-Myers Squibb", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 3, "market_cap_bn": 110.0},
    {"ticker": "AMGN", "name": "Amgen", "exchange": "NASDAQ", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 3, "market_cap_bn": 150.0},
    {"ticker": "GILD", "name": "Gilead Sciences", "exchange": "NASDAQ", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 3, "market_cap_bn": 110.0},
    {"ticker": "CVS", "name": "CVS Health", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 3, "market_cap_bn": 70.0},
    {"ticker": "MDT", "name": "Medtronic", "exchange": "NYSE", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 3, "market_cap_bn": 110.0},
    {"ticker": "ISRG", "name": "Intuitive Surgical", "exchange": "NASDAQ", "country": "US", "sector": "Health Care", "currency": "USD", "fame_rank": 3, "market_cap_bn": 180.0},
    # --- US energy ------------------------------------------------------------
    {"ticker": "XOM", "name": "Exxon Mobil", "exchange": "NYSE", "country": "US", "sector": "Energy", "currency": "USD", "fame_rank": 5, "market_cap_bn": 500.0},
    {"ticker": "CVX", "name": "Chevron", "exchange": "NYSE", "country": "US", "sector": "Energy", "currency": "USD", "fame_rank": 4, "market_cap_bn": 280.0},
    {"ticker": "COP", "name": "ConocoPhillips", "exchange": "NYSE", "country": "US", "sector": "Energy", "currency": "USD", "fame_rank": 3, "market_cap_bn": 130.0},
    {"ticker": "SLB", "name": "Schlumberger", "exchange": "NYSE", "country": "US", "sector": "Energy", "currency": "USD", "fame_rank": 3, "market_cap_bn": 60.0},
    {"ticker": "EOG", "name": "EOG Resources", "exchange": "NYSE", "country": "US", "sector": "Energy", "currency": "USD", "fame_rank": 2, "market_cap_bn": 70.0},
    {"ticker": "OXY", "name": "Occidental Petroleum", "exchange": "NYSE", "country": "US", "sector": "Energy", "currency": "USD", "fame_rank": 3, "market_cap_bn": 50.0},
    {"ticker": "PSX", "name": "Phillips 66", "exchange": "NYSE", "country": "US", "sector": "Energy", "currency": "USD", "fame_rank": 2, "market_cap_bn": 55.0},
    {"ticker": "MPC", "name": "Marathon Petroleum", "exchange": "NYSE", "country": "US", "sector": "Energy", "currency": "USD", "fame_rank": 2, "market_cap_bn": 55.0},
    {"ticker": "KMI", "name": "Kinder Morgan", "exchange": "NYSE", "country": "US", "sector": "Energy", "currency": "USD", "fame_rank": 2, "market_cap_bn": 55.0},
    # --- US industrials -------------------------------------------------------
    {"ticker": "BA", "name": "Boeing", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 5, "market_cap_bn": 110.0},
    {"ticker": "CAT", "name": "Caterpillar", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 180.0},
    {"ticker": "GE", "name": "GE Aerospace", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 200.0},
    {"ticker": "HON", "name": "Honeywell International", "exchange": "NASDAQ", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 140.0},
    {"ticker": "UPS", "name": "United Parcel Service", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 110.0},
    {"ticker": "FDX", "name": "FedEx", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 70.0},
    {"ticker": "LMT", "name": "Lockheed Martin", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 4, "market_cap_bn": 110.0},
    {"ticker": "RTX", "name": "RTX (Raytheon)", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 150.0},
    {"ticker": "DE", "name": "Deere & Company", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 120.0},
    {"ticker": "MMM", "name": "3M", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 70.0},
    {"ticker": "UNP", "name": "Union Pacific", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 140.0},
    {"ticker": "GD", "name": "General Dynamics", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 75.0},
    {"ticker": "NOC", "name": "Northrop Grumman", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 70.0},
    {"ticker": "EMR", "name": "Emerson Electric", "exchange": "NYSE", "country": "US", "sector": "Industrials", "currency": "USD", "fame_rank": 2, "market_cap_bn": 65.0},
    # --- US materials ---------------------------------------------------------
    {"ticker": "LIN", "name": "Linde", "exchange": "NASDAQ", "country": "US", "sector": "Materials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 210.0},
    {"ticker": "SHW", "name": "Sherwin-Williams", "exchange": "NYSE", "country": "US", "sector": "Materials", "currency": "USD", "fame_rank": 3, "market_cap_bn": 90.0},
    {"ticker": "FCX", "name": "Freeport-McMoRan", "exchange": "NYSE", "country": "US", "sector": "Materials", "currency": "USD", "fame_rank": 2, "market_cap_bn": 60.0},
    {"ticker": "NEM", "name": "Newmont", "exchange": "NYSE", "country": "US", "sector": "Materials", "currency": "USD", "fame_rank": 2, "market_cap_bn": 50.0},
    # --- US utilities / real estate ------------------------------------------
    {"ticker": "NEE", "name": "NextEra Energy", "exchange": "NYSE", "country": "US", "sector": "Utilities", "currency": "USD", "fame_rank": 3, "market_cap_bn": 150.0},
    {"ticker": "DUK", "name": "Duke Energy", "exchange": "NYSE", "country": "US", "sector": "Utilities", "currency": "USD", "fame_rank": 2, "market_cap_bn": 85.0},
    {"ticker": "SO", "name": "Southern Company", "exchange": "NYSE", "country": "US", "sector": "Utilities", "currency": "USD", "fame_rank": 2, "market_cap_bn": 95.0},
    {"ticker": "AMT", "name": "American Tower", "exchange": "NYSE", "country": "US", "sector": "Real Estate", "currency": "USD", "fame_rank": 2, "market_cap_bn": 90.0},
    {"ticker": "PLD", "name": "Prologis", "exchange": "NYSE", "country": "US", "sector": "Real Estate", "currency": "USD", "fame_rank": 2, "market_cap_bn": 110.0},
    # --- US autos -------------------------------------------------------------
    {"ticker": "F", "name": "Ford Motor", "exchange": "NYSE", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 4, "market_cap_bn": 45.0},
    {"ticker": "GM", "name": "General Motors", "exchange": "NYSE", "country": "US", "sector": "Consumer Discretionary", "currency": "USD", "fame_rank": 4, "market_cap_bn": 55.0},
    # --- Top European names (correct Yahoo suffixes) -------------------------
    {"ticker": "ASML", "name": "ASML Holding", "exchange": "NASDAQ", "country": "NL", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 350.0},
    {"ticker": "SAP", "name": "SAP", "exchange": "NYSE", "country": "DE", "sector": "Technology", "currency": "USD", "fame_rank": 4, "market_cap_bn": 230.0},
    {"ticker": "MC.PA", "name": "LVMH Moët Hennessy Louis Vuitton", "exchange": "Paris", "country": "FR", "sector": "Consumer Discretionary", "currency": "EUR", "fame_rank": 5, "market_cap_bn": 350.0},
    {"ticker": "OR.PA", "name": "L'Oréal", "exchange": "Paris", "country": "FR", "sector": "Consumer Staples", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 230.0},
    {"ticker": "TTE.PA", "name": "TotalEnergies", "exchange": "Paris", "country": "FR", "sector": "Energy", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 140.0},
    {"ticker": "AIR.PA", "name": "Airbus", "exchange": "Paris", "country": "FR", "sector": "Industrials", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 140.0},
    {"ticker": "SAN.PA", "name": "Sanofi", "exchange": "Paris", "country": "FR", "sector": "Health Care", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 120.0},
    {"ticker": "SIE.DE", "name": "Siemens", "exchange": "XETRA", "country": "DE", "sector": "Industrials", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 160.0},
    {"ticker": "ALV.DE", "name": "Allianz", "exchange": "XETRA", "country": "DE", "sector": "Financials", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 120.0},
    {"ticker": "BMW.DE", "name": "BMW", "exchange": "XETRA", "country": "DE", "sector": "Consumer Discretionary", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 55.0},
    {"ticker": "MBG.DE", "name": "Mercedes-Benz Group", "exchange": "XETRA", "country": "DE", "sector": "Consumer Discretionary", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 60.0},
    {"ticker": "VOW3.DE", "name": "Volkswagen", "exchange": "XETRA", "country": "DE", "sector": "Consumer Discretionary", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 55.0},
    {"ticker": "BAS.DE", "name": "BASF", "exchange": "XETRA", "country": "DE", "sector": "Materials", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 45.0},
    {"ticker": "DTE.DE", "name": "Deutsche Telekom", "exchange": "XETRA", "country": "DE", "sector": "Communication Services", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 140.0},
    {"ticker": "NESN.SW", "name": "Nestlé", "exchange": "SIX", "country": "CH", "sector": "Consumer Staples", "currency": "CHF", "fame_rank": 5, "market_cap_bn": 240.0},
    {"ticker": "NOVN.SW", "name": "Novartis", "exchange": "SIX", "country": "CH", "sector": "Health Care", "currency": "CHF", "fame_rank": 4, "market_cap_bn": 210.0},
    {"ticker": "ROG.SW", "name": "Roche Holding", "exchange": "SIX", "country": "CH", "sector": "Health Care", "currency": "CHF", "fame_rank": 4, "market_cap_bn": 230.0},
    {"ticker": "AZN.L", "name": "AstraZeneca", "exchange": "LSE", "country": "GB", "sector": "Health Care", "currency": "GBp", "fame_rank": 4, "market_cap_bn": 200.0},
    {"ticker": "SHEL.L", "name": "Shell", "exchange": "LSE", "country": "GB", "sector": "Energy", "currency": "GBp", "fame_rank": 4, "market_cap_bn": 180.0},
    {"ticker": "HSBA.L", "name": "HSBC Holdings", "exchange": "LSE", "country": "GB", "sector": "Financials", "currency": "GBp", "fame_rank": 4, "market_cap_bn": 150.0},
    {"ticker": "ULVR.L", "name": "Unilever", "exchange": "LSE", "country": "GB", "sector": "Consumer Staples", "currency": "GBp", "fame_rank": 4, "market_cap_bn": 130.0},
    {"ticker": "BP.L", "name": "BP", "exchange": "LSE", "country": "GB", "sector": "Energy", "currency": "GBp", "fame_rank": 4, "market_cap_bn": 90.0},
    {"ticker": "RIO.L", "name": "Rio Tinto", "exchange": "LSE", "country": "GB", "sector": "Materials", "currency": "GBp", "fame_rank": 3, "market_cap_bn": 90.0},
    {"ticker": "VOD.L", "name": "Vodafone Group", "exchange": "LSE", "country": "GB", "sector": "Communication Services", "currency": "GBp", "fame_rank": 3, "market_cap_bn": 25.0},
    {"ticker": "NOVO-B.CO", "name": "Novo Nordisk", "exchange": "Copenhagen", "country": "DK", "sector": "Health Care", "currency": "DKK", "fame_rank": 5, "market_cap_bn": 400.0},
    {"ticker": "INGA.AS", "name": "ING Groep", "exchange": "Amsterdam", "country": "NL", "sector": "Financials", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 55.0},
    {"ticker": "SAN.MC", "name": "Banco Santander", "exchange": "BME", "country": "ES", "sector": "Financials", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 70.0},
    {"ticker": "IBE.MC", "name": "Iberdrola", "exchange": "BME", "country": "ES", "sector": "Utilities", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 90.0},
    {"ticker": "ITX.MC", "name": "Inditex (Zara)", "exchange": "BME", "country": "ES", "sector": "Consumer Discretionary", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 150.0},
    # --- FTSE MIB (Italy, .MI suffix) ----------------------------------------
    {"ticker": "ENI.MI", "name": "Eni", "exchange": "Milan", "country": "IT", "sector": "Energy", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 45.0},
    {"ticker": "ENEL.MI", "name": "Enel", "exchange": "Milan", "country": "IT", "sector": "Utilities", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 70.0},
    {"ticker": "ISP.MI", "name": "Intesa Sanpaolo", "exchange": "Milan", "country": "IT", "sector": "Financials", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 75.0},
    {"ticker": "UCG.MI", "name": "UniCredit", "exchange": "Milan", "country": "IT", "sector": "Financials", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 65.0},
    {"ticker": "STLAM.MI", "name": "Stellantis", "exchange": "Milan", "country": "IT", "sector": "Consumer Discretionary", "currency": "EUR", "fame_rank": 5, "market_cap_bn": 40.0},
    {"ticker": "RACE.MI", "name": "Ferrari", "exchange": "Milan", "country": "IT", "sector": "Consumer Discretionary", "currency": "EUR", "fame_rank": 5, "market_cap_bn": 75.0},
    {"ticker": "G.MI", "name": "Assicurazioni Generali", "exchange": "Milan", "country": "IT", "sector": "Financials", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 40.0},
    {"ticker": "TIT.MI", "name": "Telecom Italia (TIM)", "exchange": "Milan", "country": "IT", "sector": "Communication Services", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 7.0},
    {"ticker": "PST.MI", "name": "Poste Italiane", "exchange": "Milan", "country": "IT", "sector": "Financials", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 17.0},
    {"ticker": "LDO.MI", "name": "Leonardo", "exchange": "Milan", "country": "IT", "sector": "Industrials", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 25.0},
    {"ticker": "MB.MI", "name": "Mediobanca", "exchange": "Milan", "country": "IT", "sector": "Financials", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 13.0},
    {"ticker": "TRN.MI", "name": "Terna", "exchange": "Milan", "country": "IT", "sector": "Utilities", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 16.0},
    {"ticker": "SRG.MI", "name": "Snam", "exchange": "Milan", "country": "IT", "sector": "Utilities", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 16.0},
    {"ticker": "A2A.MI", "name": "A2A", "exchange": "Milan", "country": "IT", "sector": "Utilities", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 7.0},
    {"ticker": "MONC.MI", "name": "Moncler", "exchange": "Milan", "country": "IT", "sector": "Consumer Discretionary", "currency": "EUR", "fame_rank": 4, "market_cap_bn": 15.0},
    {"ticker": "REC.MI", "name": "Recordati", "exchange": "Milan", "country": "IT", "sector": "Health Care", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 11.0},
    {"ticker": "AMP.MI", "name": "Amplifon", "exchange": "Milan", "country": "IT", "sector": "Health Care", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 6.0},
    {"ticker": "BAMI.MI", "name": "Banco BPM", "exchange": "Milan", "country": "IT", "sector": "Financials", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 13.0},
    {"ticker": "BPE.MI", "name": "BPER Banca", "exchange": "Milan", "country": "IT", "sector": "Financials", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 9.0},
    {"ticker": "PRY.MI", "name": "Prysmian", "exchange": "Milan", "country": "IT", "sector": "Industrials", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 18.0},
    {"ticker": "CPR.MI", "name": "Davide Campari-Milano", "exchange": "Milan", "country": "IT", "sector": "Consumer Staples", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 7.0},
    {"ticker": "DIA.MI", "name": "DiaSorin", "exchange": "Milan", "country": "IT", "sector": "Health Care", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 6.0},
    {"ticker": "IG.MI", "name": "Italgas", "exchange": "Milan", "country": "IT", "sector": "Utilities", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 5.0},
    {"ticker": "HER.MI", "name": "Hera", "exchange": "Milan", "country": "IT", "sector": "Utilities", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 6.0},
    {"ticker": "BMED.MI", "name": "Banca Mediolanum", "exchange": "Milan", "country": "IT", "sector": "Financials", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 9.0},
    {"ticker": "FBK.MI", "name": "FinecoBank", "exchange": "Milan", "country": "IT", "sector": "Financials", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 11.0},
    {"ticker": "TEN.MI", "name": "Tenaris", "exchange": "Milan", "country": "IT", "sector": "Energy", "currency": "EUR", "fame_rank": 3, "market_cap_bn": 18.0},
    {"ticker": "SPM.MI", "name": "Saipem", "exchange": "Milan", "country": "IT", "sector": "Energy", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 7.0},
    {"ticker": "INW.MI", "name": "Infrastrutture Wireless Italiane (Inwit)", "exchange": "Milan", "country": "IT", "sector": "Communication Services", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 10.0},
    {"ticker": "ERG.MI", "name": "ERG", "exchange": "Milan", "country": "IT", "sector": "Utilities", "currency": "EUR", "fame_rank": 2, "market_cap_bn": 4.0},
]

# Fast lookup by ticker (built once at import).
_UNIVERSE_BY_TICKER: dict[str, dict] = {item["ticker"]: item for item in UNIVERSE}


# --------------------------------------------------------------------------- #
# Deterministic ranking scores (pure functions)
# --------------------------------------------------------------------------- #


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def compute_reliability(
    volatility_30d_pct: float | None,
    above_sma200: bool | None,
    market_cap_bn: float | None,
) -> float | None:
    """Deterministic 0..100 reliability score from stability, trend and size.

    Components (each contributes 0 when its input is missing):
    * stability from volatility — lower annualized vol is better:
      ``clamp(100 - vol, 0, 100) * 0.5`` (max 50).
    * +20 when the price is above its 200-day moving average (healthy long-term
      trend).
    * up to 30 from size: ``min(market_cap_bn, 1000) / 1000 * 30``.

    Returns ``None`` only when *all* inputs are missing; otherwise a value in
    ``[0, 100]``.
    """
    if volatility_30d_pct is None and above_sma200 is None and market_cap_bn is None:
        return None

    score = 0.0
    if volatility_30d_pct is not None:
        score += _clamp(100.0 - volatility_30d_pct, 0.0, 100.0) * 0.5
    if above_sma200:
        score += 20.0
    score += min(market_cap_bn or 0.0, 1000.0) / 1000.0 * 30.0
    return _clamp(score, 0.0, 100.0)


def compute_composite(
    fame_rank: int | None,
    change_pct_30d: float | None,
    market_cap_bn: float | None,
    reliability_score: float | None,
) -> float:
    """Deterministic 0..100 weighted composite ranking score.

    Weights: fame 35%, trend 25%, value 20%, reliability 20%. Each normalized
    to 0..100 before weighting; **missing pieces contribute 0**:
    * fame: ``fame_rank / 5 * 100`` (fame_rank is 1..5).
    * trend: ``clamp((change_pct_30d + 20) / 40, 0, 1) * 100`` (squashes the
      monthly change so roughly -20%..+20% maps to 0..100).
    * value: ``min(market_cap_bn, 1000) / 1000 * 100``.
    * reliability: the 0..100 ``reliability_score`` as-is.

    Always returns a value in ``[0, 100]`` (never ``None``).
    """
    fame_norm = _clamp((fame_rank or 0) / 5.0, 0.0, 1.0) * 100.0
    if change_pct_30d is None:
        trend_norm = 0.0
    else:
        trend_norm = _clamp((change_pct_30d + 20.0) / 40.0, 0.0, 1.0) * 100.0
    value_norm = min(market_cap_bn or 0.0, 1000.0) / 1000.0 * 100.0
    reliability_norm = reliability_score if reliability_score is not None else 0.0

    composite = (
        fame_norm * 0.35
        + trend_norm * 0.25
        + value_norm * 0.20
        + reliability_norm * 0.20
    )
    return _clamp(composite, 0.0, 100.0)


# --------------------------------------------------------------------------- #
# Concurrency helpers
# --------------------------------------------------------------------------- #


def is_refreshing() -> bool:
    """Whether a ``refresh_universe`` is currently in progress."""
    return _refresh_lock.locked()


# --------------------------------------------------------------------------- #
# Static seeding (no network)
# --------------------------------------------------------------------------- #


def seed_universe(db: Session) -> int:
    """Insert any missing ``UNIVERSE`` rows using static metadata only (no network).

    Prices and market-derived fields are left ``None``; a size-only reliability
    and a fame+value composite are computed so the table is immediately
    rankable before the first real refresh. Returns the number of rows inserted.
    """
    existing = {ticker for (ticker,) in db.execute(select(UniverseStat.ticker)).all()}
    added = 0
    for item in UNIVERSE:
        ticker = item["ticker"]
        if ticker in existing:
            continue
        cap = item.get("market_cap_bn")
        reliability = compute_reliability(None, None, cap)
        composite = compute_composite(item.get("fame_rank"), None, cap, reliability)
        db.add(
            UniverseStat(
                ticker=ticker,
                name=item.get("name", "") or "",
                exchange=item.get("exchange"),
                country=item.get("country", "US") or "US",
                sector=item.get("sector", "") or "",
                currency=item.get("currency", "USD") or "USD",
                fame_rank=int(item.get("fame_rank", 3) or 3),
                market_cap_bn=cap,
                reliability_score=reliability,
                composite_score=composite,
                updated_at=None,
            )
        )
        added += 1
    db.commit()
    return added


# --------------------------------------------------------------------------- #
# Market-stats refresh (network -> DB)
# --------------------------------------------------------------------------- #


def _chunked(items: list[str], size: int) -> list[list[str]]:
    """Split ``items`` into consecutive chunks of at most ``size`` elements."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _finite(value: object) -> float | None:
    """Best-effort conversion to a finite float, else None (NaN/inf -> None)."""
    if value is None:
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _extract_closes(data: pd.DataFrame | None, ticker: str) -> pd.Series:
    """Pull a ticker's (NaN-dropped) close series out of a batch download frame.

    Handles both the multi-ticker (``group_by="ticker"`` -> MultiIndex columns)
    and the degenerate single-ticker (flat OHLCV columns) shapes. Returns an
    empty Series if the ticker/column is absent or the frame is empty.
    """
    empty = pd.Series(dtype=float)
    if data is None or len(data) == 0:
        return empty
    try:
        columns = data.columns
        if isinstance(columns, pd.MultiIndex):
            if ticker not in columns.get_level_values(0):
                return empty
            sub = data[ticker]
            if "Close" not in sub.columns:
                return empty
            closes = sub["Close"]
        else:
            if "Close" not in columns:
                return empty
            closes = data["Close"]
    except Exception:
        return empty
    return closes.dropna()


def _compute_ticker_stats(closes: pd.Series, meta: dict) -> dict:
    """Compute market-derived stats + ranking scores for one ticker.

    Missing/insufficient data leaves the affected field ``None``; the composite
    is still computed from whatever is available (fame/value always present).
    """
    last_price: float | None = None
    change_pct_1d: float | None = None
    change_pct_30d: float | None = None
    volatility_30d_pct: float | None = None
    above_sma200: bool | None = None

    n = len(closes)
    if n >= 1:
        last_price = _finite(closes.iloc[-1])
    if n >= 2:
        prev = _finite(closes.iloc[-2])
        if last_price is not None and prev:
            change_pct_1d = (last_price - prev) / prev * 100.0
    if n >= _TRADING_DAYS_1M + 1:
        past = _finite(closes.iloc[-1 - _TRADING_DAYS_1M])
        current = _finite(closes.iloc[-1])
        if current is not None and past:
            change_pct_30d = (current - past) / past * 100.0
    if n >= 31:
        # Reuse the shared indicator implementation (annualized %, window=30).
        vol_series = volatility(closes, 30)
        if len(vol_series):
            volatility_30d_pct = _finite(vol_series.iloc[-1])
    if n >= _SMA_LONG_WINDOW:
        sma200 = _finite(closes.iloc[-_SMA_LONG_WINDOW:].mean())
        current = _finite(closes.iloc[-1])
        if sma200 is not None and current is not None:
            above_sma200 = bool(current > sma200)

    market_cap_bn = meta.get("market_cap_bn")
    reliability = compute_reliability(volatility_30d_pct, above_sma200, market_cap_bn)
    composite = compute_composite(
        meta.get("fame_rank"), change_pct_30d, market_cap_bn, reliability
    )
    return {
        "last_price": last_price,
        "change_pct_1d": change_pct_1d,
        "change_pct_30d": change_pct_30d,
        "volatility_30d_pct": volatility_30d_pct,
        "above_sma200": above_sma200,
        "reliability_score": reliability,
        "composite_score": composite,
    }


def _upsert_stat(db: Session, ticker: str, meta: dict, stats: dict, now: datetime) -> None:
    """Insert or update the ``universe_stats`` row for ``ticker``."""
    row = db.execute(
        select(UniverseStat).where(UniverseStat.ticker == ticker)
    ).scalar_one_or_none()
    if row is None:
        row = UniverseStat(
            ticker=ticker,
            name=meta.get("name", "") or "",
            exchange=meta.get("exchange"),
            country=meta.get("country", "US") or "US",
            sector=meta.get("sector", "") or "",
            currency=meta.get("currency", "USD") or "USD",
            fame_rank=int(meta.get("fame_rank", 3) or 3),
            market_cap_bn=meta.get("market_cap_bn"),
        )
        db.add(row)
    row.last_price = stats["last_price"]
    row.change_pct_1d = stats["change_pct_1d"]
    row.change_pct_30d = stats["change_pct_30d"]
    row.volatility_30d_pct = stats["volatility_30d_pct"]
    row.above_sma200 = stats["above_sma200"]
    row.reliability_score = stats["reliability_score"]
    row.composite_score = stats["composite_score"]
    row.updated_at = now


def refresh_universe(db: Session) -> int:
    """Refresh market stats for the whole universe via batched yfinance downloads.

    **Synchronous** (yfinance is blocking): callers on the event loop wrap this
    in ``asyncio.to_thread``. Tickers are downloaded in chunks of ~50 (one HTTP
    request per chunk), each chunk isolated by try/except so one failing chunk
    never aborts the batch; the session is committed per chunk. A single bad
    ticker never raises — its market fields stay ``None`` and the composite is
    still computed from fame/value.

    Concurrency is guarded by a module-level lock: if a refresh is already in
    progress this returns ``0`` immediately. Returns the number of tickers
    upserted.
    """
    if not _refresh_lock.acquire(blocking=False):
        logger.info("refresh_universe skipped: another refresh is already in progress")
        return 0
    try:
        tickers = [item["ticker"] for item in UNIVERSE]
        now = datetime.utcnow()
        updated = 0
        for chunk in _chunked(tickers, _CHUNK_SIZE):
            try:
                data = yf.download(
                    chunk,
                    period="1y",
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=False,
                    progress=False,
                    threads=True,
                )
            except Exception:
                logger.warning(
                    "refresh_universe: batch download failed for chunk %s",
                    chunk,
                    exc_info=True,
                )
                data = None

            for ticker in chunk:
                try:
                    meta = _UNIVERSE_BY_TICKER.get(ticker, {"ticker": ticker})
                    closes = _extract_closes(data, ticker)
                    stats = _compute_ticker_stats(closes, meta)
                    _upsert_stat(db, ticker, meta, stats, now)
                    updated += 1
                except Exception:
                    logger.warning(
                        "refresh_universe: failed to update %s", ticker, exc_info=True
                    )
                    continue
            try:
                db.commit()
            except Exception:
                logger.warning("refresh_universe: commit failed for a chunk", exc_info=True)
                db.rollback()
        return updated
    finally:
        _refresh_lock.release()


# --------------------------------------------------------------------------- #
# Single-ticker refresh (ad-hoc, e.g. resolved from an ISIN lookup)
# --------------------------------------------------------------------------- #


def _download_single_history(ticker: str) -> pd.Series:
    """Download ~1y of daily closes for one ticker; empty Series on any failure.

    Mirrors ``refresh_universe``'s batched download shape (``group_by="ticker"``)
    so the shared ``_extract_closes`` helper handles the result unchanged.
    """
    try:
        data = yf.download(
            [ticker],
            period="1y",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception:
        logger.warning(
            "refresh_single_ticker: history download failed for %s", ticker, exc_info=True
        )
        return pd.Series(dtype=float)
    return _extract_closes(data, ticker)


def refresh_single_ticker(db: Session, ticker: str) -> UniverseStat | None:
    """Resolve, compute stats for and upsert a *single* ticker's universe row.

    Unlike :func:`refresh_universe` this works for a ticker that is **not**
    necessarily in the curated ``UNIVERSE`` (e.g. one resolved from an ISIN
    lookup). It:

    * validates the ticker via ``MarketDataService.validate_and_profile`` and
      returns ``None`` (writing nothing) if it cannot be resolved to real
      market data;
    * fills ``name``/``exchange``/``currency`` from that profile and
      ``market_cap_bn`` from ``get_fundamentals`` (market cap / 1e9, an
      *indicative* value) when available;
    * computes the same market stats + ranking scores as the batch refresh from
      ~1y of daily closes;
    * upserts (and commits) the ``universe_stats`` row and returns it.

    ``fame_rank`` keeps the existing row's value, falls back to the curated
    ``UNIVERSE`` rank when the ticker is curated, and otherwise defaults to 2
    for a brand-new row.

    **Synchronous** (yfinance/httpx are blocking): callers on the event loop
    wrap this in ``asyncio.to_thread``. Defensive: any network failure on the
    *stats* part (history/fundamentals) still leaves a valid row carrying the
    validated metadata with ``None`` market stats; only a ticker that cannot be
    validated yields ``None``.
    """
    normalized = (ticker or "").strip().upper()
    if not normalized:
        return None

    service = MarketDataService()
    try:
        profile = service.validate_and_profile(normalized)
    except Exception:
        logger.warning(
            "refresh_single_ticker: validation failed for %s", normalized, exc_info=True
        )
        profile = None
    if not profile:
        return None

    existing = db.execute(
        select(UniverseStat).where(UniverseStat.ticker == normalized)
    ).scalar_one_or_none()
    curated = _UNIVERSE_BY_TICKER.get(normalized, {})

    # Indicative live fundamentals (never fatal): market cap -> billions.
    try:
        fundamentals = service.get_fundamentals(normalized)
    except Exception:
        logger.warning(
            "refresh_single_ticker: fundamentals failed for %s", normalized, exc_info=True
        )
        fundamentals = {}
    if not isinstance(fundamentals, dict):
        fundamentals = {}

    # market_cap_bn: live fundamentals -> curated baseline -> existing row.
    market_cap_bn: float | None = None
    cap_value = _finite(fundamentals.get("market_cap"))
    if cap_value is not None and cap_value > 0:
        market_cap_bn = cap_value / 1e9
    elif curated.get("market_cap_bn") is not None:
        market_cap_bn = curated.get("market_cap_bn")
    elif existing is not None:
        market_cap_bn = existing.market_cap_bn

    # fame_rank: existing row -> curated -> default 2 for a brand-new row.
    if existing is not None:
        fame_rank = existing.fame_rank
    elif curated.get("fame_rank") is not None:
        fame_rank = int(curated["fame_rank"])
    else:
        fame_rank = 2

    sector = (
        (fundamentals.get("sector") if isinstance(fundamentals.get("sector"), str) else None)
        or curated.get("sector")
        or (existing.sector if existing is not None else None)
        or ""
    )
    name = profile.get("name") or curated.get("name") or (
        existing.name if existing is not None else None
    ) or normalized
    exchange = profile.get("exchange") or curated.get("exchange") or (
        existing.exchange if existing is not None else None
    )
    country = curated.get("country") or (
        existing.country if existing is not None else None
    ) or "US"
    currency = profile.get("currency") or curated.get("currency") or (
        existing.currency if existing is not None else None
    ) or "USD"

    meta = {
        "ticker": normalized,
        "name": name,
        "exchange": exchange,
        "country": country,
        "sector": sector,
        "currency": currency,
        "fame_rank": fame_rank,
        "market_cap_bn": market_cap_bn,
    }

    closes = _download_single_history(normalized)
    stats = _compute_ticker_stats(closes, meta)

    now = datetime.utcnow()
    if existing is None:
        row = UniverseStat(
            ticker=normalized,
            name=name or "",
            exchange=exchange,
            country=country or "US",
            sector=sector or "",
            currency=currency or "USD",
            fame_rank=int(fame_rank or 2),
            market_cap_bn=market_cap_bn,
        )
        db.add(row)
    else:
        row = existing
        # Refresh metadata from the freshly-validated profile (keep fame_rank).
        row.name = name or row.name
        row.exchange = exchange
        row.country = country or "US"
        row.sector = sector or row.sector
        row.currency = currency or "USD"
        row.market_cap_bn = market_cap_bn

    row.last_price = stats["last_price"]
    row.change_pct_1d = stats["change_pct_1d"]
    row.change_pct_30d = stats["change_pct_30d"]
    row.volatility_30d_pct = stats["volatility_30d_pct"]
    row.above_sma200 = stats["above_sma200"]
    row.reliability_score = stats["reliability_score"]
    row.composite_score = stats["composite_score"]
    row.updated_at = now

    db.commit()
    return row
