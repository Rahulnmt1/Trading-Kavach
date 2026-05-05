"""Zerodha KiteTicker WebSocket — sub-second tick stream pushed to Redis.

Each tick arrives as JSON `{"ltp", "volume", "ts"}` under key `tick:<SYMBOL>`.
The executor (and dashboard) read these via `cache.get_json("tick:RELIANCE")`.

Typical usage from another module:
    feed = KiteFeed(["RELIANCE", "INFY", "HDFCBANK"])
    feed.start()
    ...
    feed.stop()

Re-connection is handled by KiteTicker itself; we just log connection events.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import pytz

from ..cache import get_cache
from ..config import env
from ..logger import logger

IST = pytz.timezone("Asia/Kolkata")


class KiteFeed:
    """Streams ticks for a list of NSE equity symbols."""

    def __init__(self, symbols: Iterable[str], mode: str = "ltp") -> None:
        self.symbols = [s.upper() for s in symbols]
        self.mode = mode  # "ltp" / "quote" / "full"
        self.cache = get_cache()
        self._token_to_symbol: Dict[int, str] = {}
        self._symbol_to_token: Dict[str, int] = {}
        self._kite = None
        self._ticker = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ---------- bootstrap ----------

    def _ensure_kite(self) -> None:
        if self._kite is not None:
            return
        from kiteconnect import KiteConnect, KiteTicker
        e_ = env()
        if not (e_.KITE_API_KEY and e_.KITE_ACCESS_TOKEN):
            raise RuntimeError("KITE_API_KEY and KITE_ACCESS_TOKEN required for KiteFeed.")
        self._kite = KiteConnect(api_key=e_.KITE_API_KEY)
        self._kite.set_access_token(e_.KITE_ACCESS_TOKEN)
        self._ticker = KiteTicker(e_.KITE_API_KEY, e_.KITE_ACCESS_TOKEN)

    def _resolve_tokens(self) -> None:
        """Map NSE symbols to numeric instrument_tokens via Kite's instruments dump."""
        cache_key = "kite:instruments:nse"
        cached = self.cache.get_json(cache_key)
        if cached:
            self._symbol_to_token = {s: int(t) for s, t in cached.items()}
        else:
            instruments = self._kite.instruments("NSE")
            mapping = {row["tradingsymbol"]: row["instrument_token"]
                       for row in instruments if row.get("segment") == "NSE"}
            self._symbol_to_token = mapping
            self.cache.set_json(cache_key, mapping, ttl=24 * 3600)

        self._token_to_symbol = {}
        for s in self.symbols:
            tok = self._symbol_to_token.get(s)
            if tok is None:
                logger.warning("[kite_ws] no token for {}, skipping", s)
                continue
            self._token_to_symbol[int(tok)] = s

        logger.info("[kite_ws] resolved {} of {} tokens", len(self._token_to_symbol), len(self.symbols))

    # ---------- callbacks ----------

    def _on_ticks(self, ws, ticks: List[dict]) -> None:
        for t in ticks:
            tok = t.get("instrument_token")
            sym = self._token_to_symbol.get(int(tok))
            if not sym:
                continue
            ltp = float(t.get("last_price") or t.get("ltp") or 0.0)
            volume = float(t.get("volume") or t.get("volume_traded") or 0.0)
            payload = {"ltp": ltp, "volume": volume, "ts": datetime.now(IST).isoformat()}
            self.cache.set_json(f"tick:{sym}", payload, ttl=120)

    def _on_connect(self, ws, response) -> None:
        from kiteconnect import KiteTicker
        tokens = list(self._token_to_symbol.keys())
        logger.info("[kite_ws] connected; subscribing to {} tokens", len(tokens))
        ws.subscribe(tokens)
        mode_map = {"ltp": KiteTicker.MODE_LTP, "quote": KiteTicker.MODE_QUOTE, "full": KiteTicker.MODE_FULL}
        ws.set_mode(mode_map.get(self.mode, KiteTicker.MODE_LTP), tokens)

    def _on_close(self, ws, code, reason) -> None:
        logger.warning("[kite_ws] closed: code={} reason={}", code, reason)

    def _on_error(self, ws, code, reason) -> None:
        logger.error("[kite_ws] error: code={} reason={}", code, reason)

    def _on_reconnect(self, ws, attempts) -> None:
        logger.info("[kite_ws] reconnect attempt #{}", attempts)

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._running:
            return
        self._ensure_kite()
        self._resolve_tokens()
        if not self._token_to_symbol:
            raise RuntimeError("KiteFeed: no instrument tokens resolved.")

        t = self._ticker
        t.on_ticks = self._on_ticks
        t.on_connect = self._on_connect
        t.on_close = self._on_close
        t.on_error = self._on_error
        t.on_reconnect = self._on_reconnect

        self._thread = threading.Thread(target=t.connect, kwargs={"threaded": True}, daemon=True)
        self._thread.start()
        self._running = True
        logger.info("[kite_ws] feed started")

    def stop(self) -> None:
        if not self._running:
            return
        try:
            self._ticker.close()
        except Exception:
            pass
        self._running = False
        logger.info("[kite_ws] feed stopped")

    def latest(self, symbol: str) -> Optional[dict]:
        return self.cache.get_json(f"tick:{symbol.upper()}")
