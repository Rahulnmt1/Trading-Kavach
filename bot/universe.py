"""NSE stock universe — used by the watchlist updater as the candidate pool."""
from __future__ import annotations

from typing import List

# NIFTY 100 (large-cap, highly liquid). The watchlist updater filters this list
# by liquidity, trend and momentum to produce the daily watchlist.
NIFTY_100: List[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "SBIN",
    "BHARTIARTL", "ITC", "LT", "KOTAKBANK", "AXISBANK", "ASIANPAINT", "MARUTI",
    "TITAN", "BAJFINANCE", "HCLTECH", "ULTRACEMCO", "WIPRO", "SUNPHARMA",
    "POWERGRID", "ONGC", "NTPC", "TATASTEEL", "JSWSTEEL", "M&M", "TECHM",
    "GRASIM", "HINDALCO", "ADANIPORTS", "COALINDIA", "DRREDDY", "CIPLA",
    "BAJAJFINSV", "INDUSINDBK", "DIVISLAB", "EICHERMOT", "BRITANNIA", "HEROMOTOCO",
    "BPCL", "NESTLEIND", "UPL", "TATACONSUM", "APOLLOHOSP",
    # TATAMOTORS — removed: post-Oct-2024 demerger the legacy ticker keeps
    # 404'ing on yfinance; revisit when the new TATAMOTORS / TMLCV settle.
    "BAJAJ-AUTO", "SBILIFE", "HDFCLIFE", "ADANIENT", "LTIM",
    "DMART", "PIDILITIND", "GODREJCP", "ICICIPRULI", "ICICIGI", "DABUR",
    "AMBUJACEM", "SHREECEM", "VEDL", "GAIL", "IOC", "SIEMENS", "DLF",
    "HAVELLS", "BERGEPAINT", "MARICO", "CHOLAFIN", "TRENT", "BAJAJHLDNG",
    "NAUKRI", "PIIND", "MUTHOOTFIN", "PAGEIND", "LICI", "BANDHANBNK",
    "INDIGO", "MOTHERSON", "BOSCHLTD", "AUROPHARMA", "TORNTPHARM", "LUPIN",
    "BIOCON", "ADANIGREEN", "ETERNAL", "POLICYBZR", "PNB", "BANKBARODA",
    # ETERNAL replaces ZOMATO (renamed in 2025).
    "FEDERALBNK", "IDFCFIRSTB", "RECLTD", "PFC", "HAL", "BEL", "CONCOR",
    "MFSL", "GLAND", "ABBOTINDIA", "JUBLFOOD", "VOLTAS", "TVSMOTOR",
    # ADANITRANS — merged into ADANIENERGY in 2024; ADANIENT is already in
    # this list, so we just drop the dead ticker.
]

__all__ = ["NIFTY_100"]
