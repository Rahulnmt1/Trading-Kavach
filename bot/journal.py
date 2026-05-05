"""Trade journal — per-day live log of every order, paired round-trip trade, and EOD P&L.

Layout under `logs/` (per-segment to keep equity and F&O fully isolated):

    logs/
      trades/
        equity/
          2026-04-27.jsonl   ← append-only, one JSON event per line (live tail-able)
          2026-04-27.csv     ← human-friendly spreadsheet of round-trip trades
        fno/
          2026-04-27.jsonl
          2026-04-27.csv
      eod/
        equity/
          2026-04-27.txt     ← formatted End-of-Day P&L statement
        fno/
          2026-04-27.txt

The segment scoping ensures one bot's trades can never accidentally
appear in the other's P&L statement. The historical (pre-segments)
flat layout used ``logs/trades/2026-04-27.jsonl`` directly and is now
considered the equity segment's layout — those files were copied or
moved into ``logs/trades/equity/`` if you want to preserve history;
otherwise they remain alongside the per-segment dirs and are ignored.

Event schema (`*.jsonl`):
  {"ts": "...", "type": "FILL"        | "TRADE_OPEN" | "TRADE_CLOSED",
   "symbol": "...", "side": "BUY"|"SELL"|"LONG"|"SHORT", ...}

Round-trip pairing: a TRADE_CLOSED event is emitted whenever the executor
takes a position from non-zero back to zero (or flips it). Realized P&L is
computed in INR, gross of slippage but net of all charges (brokerage + STT +
exchange + GST + SEBI + stamp).
"""
from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz

from .broker.base import Order, OrderSide, OrderStatus, Position
from .config import PROJECT_ROOT, load_config
from .logger import logger
from .segment import Segment, cfg_capital, journal_subdir

IST = pytz.timezone("Asia/Kolkata")


# ============================================================================
# storage paths
# ============================================================================

def _logs_root(segment: Segment = Segment.EQUITY) -> Path:
    """Return the logs root. Creates the per-segment trades/eod dirs."""
    p = PROJECT_ROOT / load_config().logging.dir
    sub = journal_subdir(segment)
    (p / "trades" / sub).mkdir(parents=True, exist_ok=True)
    (p / "eod" / sub).mkdir(parents=True, exist_ok=True)
    return p


def trades_jsonl(day: Optional[date] = None, segment: Segment = Segment.EQUITY) -> Path:
    return _logs_root(segment) / "trades" / journal_subdir(segment) / f"{(day or date.today()).isoformat()}.jsonl"


def trades_csv(day: Optional[date] = None, segment: Segment = Segment.EQUITY) -> Path:
    return _logs_root(segment) / "trades" / journal_subdir(segment) / f"{(day or date.today()).isoformat()}.csv"


def eod_report(day: Optional[date] = None, segment: Segment = Segment.EQUITY) -> Path:
    return _logs_root(segment) / "eod" / journal_subdir(segment) / f"{(day or date.today()).isoformat()}.txt"


def _hhmmss(iso_ts: Optional[str]) -> str:
    """Pull HH:MM:SS out of an ISO timestamp (with or without TZ offset)."""
    if not iso_ts:
        return "--:--:--"
    try:
        return datetime.fromisoformat(iso_ts).strftime("%H:%M:%S")
    except Exception:
        return iso_ts[-8:]


# ============================================================================
# in-memory open-trade tracker
# ============================================================================

@dataclass
class OpenTrade:
    """Tracks an open round-trip trade; used to compute P&L at close time."""
    symbol: str
    side: str             # "LONG" | "SHORT"
    qty: int
    entry_time: datetime
    entry_price: float
    entry_fees: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strategy: str = ""
    reason: str = ""


# ============================================================================
# journal
# ============================================================================

class TradeJournal:
    """Append-only daily trade log. Thread-safe.

    One TradeJournal exists per segment. ``get_journal(segment)`` is the
    canonical accessor — it returns the segment's journal singleton.
    """

    def __init__(self, segment: Segment = Segment.EQUITY) -> None:
        self.segment = segment
        self._lock = threading.Lock()
        self._open: Dict[str, OpenTrade] = {}

    # ------------------------------------------------------------------ writes
    def _append_event(self, event: Dict[str, Any]) -> None:
        event.setdefault("ts", datetime.now(IST).isoformat(timespec="seconds"))
        event.setdefault("segment", self.segment.value)
        try:
            with self._lock, trades_jsonl(segment=self.segment).open("a") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except Exception as e:
            logger.error("[journal:{}] write failed: {}", self.segment.value, e)

    def _append_csv(self, trade: Dict[str, Any]) -> None:
        path = trades_csv(segment=self.segment)
        new = not path.exists()
        try:
            with self._lock, path.open("a", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(trade.keys()))
                if new:
                    writer.writeheader()
                writer.writerow(trade)
        except Exception as e:
            logger.error("[journal:{}] csv write failed: {}", self.segment.value, e)

    # ------------------------------------------------------------------- main
    def record_fill(
        self,
        order: Order,
        position_before: Optional[Position],
        position_after: Optional[Position],
        signal_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Called by the executor after each filled order.

        Determines whether the fill OPENS a new trade, ADDS to an existing one,
        CLOSES it, or FLIPS direction, and writes the appropriate event(s).
        """
        if order.status != OrderStatus.FILLED:
            return
        meta = signal_meta or {}
        fill_price = float(order.fill_price or 0.0)

        self._append_event({
            "type": "FILL",
            "order_id": order.id,
            "symbol": order.symbol,
            "side": order.side.value,
            "qty": int(order.qty),
            "price": fill_price,
            "fees": float(order.fees),
            "stop_loss": order.stop_loss,
            "take_profit": order.take_profit,
            "strategy": meta.get("strategy", ""),
            "reason": meta.get("reason", ""),
        })

        before_qty = position_before.qty if position_before else 0
        after_qty = position_after.qty if position_after else 0

        # Case 1: new position opened (0 -> non-zero).
        if before_qty == 0 and after_qty != 0:
            self._open[order.symbol] = OpenTrade(
                symbol=order.symbol,
                side="LONG" if order.side == OrderSide.BUY else "SHORT",
                qty=abs(after_qty),
                entry_time=datetime.now(IST),
                entry_price=fill_price,
                entry_fees=float(order.fees),
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                strategy=meta.get("strategy", ""),
                reason=meta.get("reason", ""),
            )
            self._append_event({
                "type": "TRADE_OPEN", "symbol": order.symbol,
                "side": "LONG" if order.side == OrderSide.BUY else "SHORT",
                "qty": int(abs(after_qty)), "entry_price": fill_price,
                "stop_loss": order.stop_loss, "take_profit": order.take_profit,
                "strategy": meta.get("strategy", ""), "reason": meta.get("reason", ""),
            })
            return

        # Case 2: full close (non-zero -> 0) or flip.
        if before_qty != 0 and (after_qty == 0 or before_qty * after_qty < 0):
            ot = self._open.pop(order.symbol, None)
            if ot is None:
                # We don't have a prior OPEN (process restart?); reconstruct minimally.
                ot = OpenTrade(
                    symbol=order.symbol,
                    side="LONG" if before_qty > 0 else "SHORT",
                    qty=abs(before_qty),
                    entry_time=datetime.now(IST),
                    entry_price=position_before.avg_price if position_before else fill_price,
                    entry_fees=0.0,
                )
            self._emit_close(ot, fill_price, float(order.fees), exit_reason=meta.get("exit_reason", ""))

            # If it FLIPPED, the new opposite position is also open now.
            if after_qty != 0:
                self._open[order.symbol] = OpenTrade(
                    symbol=order.symbol,
                    side="LONG" if after_qty > 0 else "SHORT",
                    qty=abs(after_qty),
                    entry_time=datetime.now(IST),
                    entry_price=fill_price,
                    entry_fees=0.0,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    strategy=meta.get("strategy", ""),
                    reason="flipped from " + ot.side,
                )
            return

        # Case 3: adding to or partially reducing an existing position — for our
        # rule-based intraday bot this rarely happens; we just log the FILL and
        # update the open record's avg/qty.
        if order.symbol in self._open:
            ot = self._open[order.symbol]
            if order.side.value == ("BUY" if ot.side == "LONG" else "SELL"):
                total = ot.qty + order.qty
                ot.entry_price = (ot.entry_price * ot.qty + fill_price * order.qty) / total
                ot.qty = total
                ot.entry_fees += float(order.fees)
            else:
                ot.qty = max(0, ot.qty - order.qty)

    def _emit_close(self, ot: OpenTrade, exit_price: float, exit_fees: float, exit_reason: str = "") -> None:
        if ot.side == "LONG":
            gross = (exit_price - ot.entry_price) * ot.qty
        else:
            gross = (ot.entry_price - exit_price) * ot.qty
        total_fees = ot.entry_fees + exit_fees
        net = gross - total_fees
        duration_min = max(0, int((datetime.now(IST) - ot.entry_time).total_seconds() // 60))

        if not exit_reason:
            if ot.stop_loss and ((ot.side == "LONG" and exit_price <= ot.stop_loss)
                                 or (ot.side == "SHORT" and exit_price >= ot.stop_loss)):
                exit_reason = "stop_loss"
            elif ot.take_profit and ((ot.side == "LONG" and exit_price >= ot.take_profit)
                                     or (ot.side == "SHORT" and exit_price <= ot.take_profit)):
                exit_reason = "take_profit"
            else:
                exit_reason = "manual"

        record = {
            "ts": datetime.now(IST).isoformat(timespec="seconds"),
            "symbol": ot.symbol,
            "side": ot.side,
            "qty": int(ot.qty),
            "entry_time": ot.entry_time.isoformat(timespec="seconds"),
            "entry_price": round(ot.entry_price, 2),
            "exit_price": round(exit_price, 2),
            "duration_min": duration_min,
            "gross_pnl": round(gross, 2),
            "fees": round(total_fees, 2),
            "net_pnl": round(net, 2),
            "stop_loss": ot.stop_loss,
            "take_profit": ot.take_profit,
            "strategy": ot.strategy,
            "exit_reason": exit_reason,
        }
        self._append_event({"type": "TRADE_CLOSED", **record})
        self._append_csv(record)
        sign = "+" if net >= 0 else ""
        logger.info("[journal] CLOSED {} {} {}@{:.2f} → {:.2f}  net ₹{}{:.2f} ({})",
                    ot.side, ot.symbol, ot.qty, ot.entry_price, exit_price,
                    sign, net, exit_reason)


# ============================================================================
# read-side helpers
# ============================================================================

def _load_events(day: Optional[date] = None,
                 segment: Segment = Segment.EQUITY) -> List[Dict[str, Any]]:
    path = trades_jsonl(day, segment=segment)
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def closed_trades(day: Optional[date] = None,
                  segment: Segment = Segment.EQUITY) -> List[Dict[str, Any]]:
    return [e for e in _load_events(day, segment=segment) if e.get("type") == "TRADE_CLOSED"]


def daily_summary(day: Optional[date] = None,
                  segment: Segment = Segment.EQUITY) -> Dict[str, Any]:
    """Aggregate metrics for the given day. Empty dict if no trades."""
    trades = closed_trades(day, segment=segment)
    if not trades:
        return {"date": (day or date.today()).isoformat(), "trades": 0, "segment": segment.value}

    pnls = [t["net_pnl"] for t in trades]
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gross = sum(t["gross_pnl"] for t in trades)
    fees = sum(t["fees"] for t in trades)
    net = sum(pnls)

    avg_win = sum(t["net_pnl"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["net_pnl"] for t in losses) / len(losses) if losses else 0.0
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    by_strategy: Dict[str, Dict[str, float]] = {}
    by_symbol: Dict[str, Dict[str, float]] = {}
    for t in trades:
        for key, bucket in ((t.get("strategy") or "unknown", by_strategy),
                            (t["symbol"], by_symbol)):
            slot = bucket.setdefault(key, {"trades": 0, "wins": 0, "net_pnl": 0.0})
            slot["trades"] += 1
            slot["wins"] += 1 if t["net_pnl"] > 0 else 0
            slot["net_pnl"] += t["net_pnl"]

    cfg = load_config()
    # Use the SEGMENT's capital, not always the equity top-level.
    capital_total = cfg_capital(cfg, segment).total
    win_rate = len(wins) / len(trades)
    # Expectancy per trade in INR — the single most informative number for
    # whether the strategies have edge. Positive expectancy + N trades/day
    # = expected daily P&L. We surface this on the dashboard.
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
    return {
        "date": (day or date.today()).isoformat(),
        "segment": segment.value,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "gross_pnl": round(gross, 2),
        "fees": round(fees, 2),
        "net_pnl": round(net, 2),
        "return_pct": round(net / capital_total * 100, 3) if capital_total else 0.0,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "payoff_ratio": round(abs(avg_win / avg_loss), 2) if avg_loss else 0.0,
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
        "biggest_win": max(trades, key=lambda t: t["net_pnl"]) if wins else None,
        "biggest_loss": min(trades, key=lambda t: t["net_pnl"]) if losses else None,
        "by_strategy": by_strategy,
        "by_symbol": by_symbol,
        "trades_list": trades,
    }


# ============================================================================
# EOD report writer (pretty text)
# ============================================================================

def write_eod_report(day: Optional[date] = None,
                     segment: Segment = Segment.EQUITY) -> Path:
    s = daily_summary(day, segment=segment)
    path = eod_report(day, segment=segment)
    if s["trades"] == 0:
        path.write_text(f"No trades on {s['date']} ({segment.label}).\n")
        return path

    cfg = load_config()
    cap = cfg_capital(cfg, segment).total
    end_cap = cap + s["net_pnl"]
    lines: List[str] = []
    sep = "=" * 64
    lines += [sep,
              f"     END OF DAY P&L STATEMENT  -  {s['date']}  [{segment.label}]",
              sep, ""]
    lines.append(f"Capital   :  ₹{cap:>10,.2f}  →  ₹{end_cap:>10,.2f}   ({s['return_pct']:+.3f}%)")
    lines.append("")
    lines.append(f"Trades    :  {s['trades']:>3}  total")
    lines.append(f"             {s['wins']:>3}  wins ({s['win_rate']*100:.1f}%)")
    lines.append(f"             {s['losses']:>3}  losses")
    lines.append("")
    lines.append(f"P&L       :  gross   ₹{s['gross_pnl']:>+10,.2f}")
    lines.append(f"             fees    ₹{-s['fees']:>+10,.2f}")
    lines.append(f"             net     ₹{s['net_pnl']:>+10,.2f}")
    lines.append("")
    lines.append("Stats     :")
    lines.append(f"   avg win        ₹{s['avg_win']:>+10,.2f}")
    lines.append(f"   avg loss       ₹{s['avg_loss']:>+10,.2f}")
    lines.append(f"   expectancy     ₹{s['expectancy']:>+10,.2f}  per trade")
    lines.append(f"   payoff ratio   {s['payoff_ratio']}")
    lines.append(f"   profit factor  {s['profit_factor']}")
    if s["biggest_win"]:
        bw = s["biggest_win"]
        lines.append(f"   biggest win    ₹{bw['net_pnl']:>+10,.2f}  ({bw['symbol']})")
    if s["biggest_loss"]:
        bl = s["biggest_loss"]
        lines.append(f"   biggest loss   ₹{bl['net_pnl']:>+10,.2f}  ({bl['symbol']})")

    lines += ["", "Per strategy:"]
    for k, v in sorted(s["by_strategy"].items(), key=lambda x: -x[1]["net_pnl"]):
        wr = v["wins"] / v["trades"] * 100 if v["trades"] else 0
        lines.append(f"   {k:<25}  {v['trades']:>3} trades   net ₹{v['net_pnl']:>+10,.2f}   win {wr:.0f}%")

    lines += ["", "Per symbol:"]
    for k, v in sorted(s["by_symbol"].items(), key=lambda x: -x[1]["net_pnl"]):
        lines.append(f"   {k:<12}  {v['trades']:>3} trades   net ₹{v['net_pnl']:>+10,.2f}")

    lines += ["", "Trade-by-trade:"]
    for t in s["trades_list"]:
        et = _hhmmss(t.get("entry_time"))
        xt = _hhmmss(t.get("ts"))
        lines.append(
            f"   {et}→{xt}  {t['side']:<5} {t['symbol']:<10}  "
            f"{t['qty']:>3}@{t['entry_price']:>8.2f} → {t['exit_price']:>8.2f}   "
            f"gross ₹{t['gross_pnl']:>+9,.2f}  fees ₹{-t['fees']:>+8,.2f}  "
            f"net ₹{t['net_pnl']:>+9,.2f}  ({t['exit_reason']})"
        )

    lines += ["", sep]
    text = "\n".join(lines) + "\n"
    path.write_text(text)
    logger.info("[journal] EOD report written → {}", path)
    return path


# ============================================================================
# singleton
# ============================================================================

_journals: Dict[Segment, TradeJournal] = {}


def get_journal(segment: Segment = Segment.EQUITY) -> TradeJournal:
    """Return the trade-journal singleton for ``segment``.

    One journal exists per segment per process. The equity bot calls
    ``get_journal()`` (defaulting to :attr:`Segment.EQUITY`) and the
    F&O bot calls ``get_journal(Segment.FNO)`` from a different
    process — they each see only their own ``self._open`` round-trip
    tracker and write to their own files.
    """
    if segment not in _journals:
        _journals[segment] = TradeJournal(segment=segment)
    return _journals[segment]
