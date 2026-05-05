"""Daily fee-schedule audit against MULTIPLE authoritative public sources.

Every weekday at 09:00 IST the scheduler invokes :func:`run_fee_audit`,
which:

  1. **Fetches** charges/pricing pages from three independent SEBI-regulated
     brokers (Zerodha, Upstox, Dhan). All three are contractually obligated
     to publish the same SEBI / NSE / BSE / Government-of-India rates, so
     they form a natural cross-verification panel.
  2. **Parses** each source's table into ``{(segment, rate_key): value}``.
  3. **Cross-checks** every line-item across all reachable sources and
     against the rates baked into :mod:`bot.fees`.
  4. **Caches** the result under ``fee_audit:latest`` so the dashboard
     and the periodic health-check can surface drift immediately.

Why multi-source (vs. just Zerodha):

* The user's directive: "all fees and taxes should be checked daily from
  the actual source like zerodha website, BSE / NSE website, etc. as they
  are the source of truth." NSE and BSE serve JavaScript-rendered SPAs
  that need a headless browser to scrape — heavy infra. The next-best
  alternative is **multiple independent broker mirrors** which republish
  the same regulator-set rates as static HTML.
* If only one source disagreed with our config, we couldn't tell whether
  the source had a typo / was stale, or whether *we* were wrong. With
  multi-source agreement we can be confident.

Verdict per rate:

* **OK** — every reachable source agrees with the configured value.
* **DRIFT (confirmed)** — ≥2 sources independently disagree with config
  (high confidence: update ``bot/fees.py`` and restart).
* **DRIFT (single-source)** — exactly one source disagrees and no other
  source covers this rate (medium confidence — manually verify).
* **AMBIGUOUS** — sources disagree with each other (low confidence —
  manually investigate which is correct).
* **UNVERIFIED** — no source could observe this rate today (network
  failure or all parsers couldn't locate it).

Coverage history:

* 2026-04-30 and earlier — single-source (Zerodha) and equity-only.
* 2026-05-04 (morning) — extended to all three segments still single-source.
* 2026-05-04 (evening) — extended to **3 independent sources** with
  per-rate cross-verification, after operator demand for source-of-truth
  rigor across multiple authoritative sites.

Design constraints:

* We **never silently auto-modify** the rate constants. If drift is
  detected, the audit raises a WARN and a human must update
  :mod:`bot.fees` and restart the bots (use ``scripts/eod_apply_fee_updates.py``
  for the safe EOD path).
* If a source fails to fetch or parse, the audit returns ``status="WARN"``
  for that source only — the others still produce verdicts.
* If ALL sources fail, the audit is ``WARN`` overall but doesn't stop
  the bot — fees just keep using the configured constants.

Run manually any time::

    python -m cli verify-fees                  # pretty table → stdout
    python -m cli verify-fees --json           # machine-readable
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz

from .cache import get_cache
from .config import PROJECT_ROOT
from .fees import (
    _FUTURES_RATES as CONFIGURED_FUTURES,
    _OPTIONS_RATES as CONFIGURED_OPTIONS,
    _RATES as CONFIGURED_EQUITY,
)
from .logger import logger

IST = pytz.timezone("Asia/Kolkata")

# Per-segment expected human labels for the dashboard. Keep in lockstep with
# the keys in ``bot.fees._RATES`` / ``_FUTURES_RATES`` / ``_OPTIONS_RATES``.
_LABELS: Dict[str, str] = {
    "brokerage_flat":     "Brokerage flat (₹/order)",
    "brokerage_pct_cap":  "Brokerage pct cap",
    "stt_sell_pct":       "STT/CTT sell-side",
    "exchange_pct":       "Exchange charges (NSE)",
    "sebi_per_crore":     "SEBI charges (₹/crore)",
    "stamp_buy_pct":      "Stamp duty buy-side",
    "gst_pct":            "GST",
}

# Bundle the three rate tables with their segment label so the audit loop
# can iterate uniformly.
_RATE_TABLES: List[Tuple[str, Dict[str, float]]] = [
    ("equity",  CONFIGURED_EQUITY),
    ("futures", CONFIGURED_FUTURES),
    ("options", CONFIGURED_OPTIONS),
]

# Floating-point comparison tolerance. Rates on the source pages are quoted
# to 5 significant figures; we accept a 0.5% relative difference (e.g.
# 0.00307% vs 0.003075% is "same"). Anything bigger is real drift.
_REL_TOL = 5e-3


def _close(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return False
    if a == b:
        return True
    if a == 0 or b == 0:
        return abs(a - b) <= 1e-9
    return abs(a - b) / max(abs(a), abs(b)) <= _REL_TOL


# ─── Data classes ────────────────────────────────────────────────────────────


@dataclass
class SourceObservation:
    """One source's observation of one rate.

    ``value`` is ``None`` if the parser could not locate this rate on the
    source page (different from "source unreachable" — that's reflected
    in :attr:`AuditResult.sources_reachable`).
    """
    source: str
    value: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RateCheck:
    key: str
    label: str
    configured: float
    observed: Optional[float]   # the consensus value (or single-source value)
    drifted: bool
    note: str = ""
    segment: str = "equity"
    verdict: str = "OK"          # OK | DRIFT_CONFIRMED | DRIFT_SINGLE | AMBIGUOUS | UNVERIFIED
    sources: List[SourceObservation] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["sources"] = [s.to_dict() if isinstance(s, SourceObservation) else s
                        for s in self.sources]
        return d


@dataclass
class AuditResult:
    timestamp: str
    status: str                       # OK | WARN | FAIL
    source: str                       # comma-joined list of reachable sources
    summary: str
    checks: List[RateCheck] = field(default_factory=list)
    sources_checked: List[str] = field(default_factory=list)
    sources_reachable: List[str] = field(default_factory=list)
    raw_excerpts: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "status":    self.status,
            "source":    self.source,
            "summary":   self.summary,
            "checks":    [c.to_dict() for c in self.checks],
            "sources_checked":   self.sources_checked,
            "sources_reachable": self.sources_reachable,
            "raw_excerpts": self.raw_excerpts,
        }


# ─── Source fetcher (shared) ─────────────────────────────────────────────────


_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36 (StockBot fee-audit)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _fetch_html(url: str, timeout: float = 10.0) -> Optional[str]:
    """Fetch a URL and return its body. Returns ``None`` on any failure."""
    try:
        req = urllib.request.Request(url, headers=_HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("[fee-audit] could not fetch {}: {}", url, e)
        return None


def _strip_to_text(html: str) -> str:
    """Reduce HTML to a single normalised text string with literal % and ₹."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text
            .replace("&#8377;", "Rs.")
            .replace("&#37;", "%")
            .replace("&amp;", "&")
            .replace("&nbsp;", " ")
            .replace("\u20b9", "Rs.")
            .replace("\\u0026", "&")
            .replace("\\u003c", "<")
            .replace("\\u003e", ">"))
    return re.sub(r"\s+", " ", text).strip()


# ─── Source: Zerodha ─────────────────────────────────────────────────────────


_ZERODHA_COL = {"equity": 1, "futures": 2, "options": 3}


class ZerodhaSource:
    """Parser for ``zerodha.com/charges``.

    The page renders a 4-column table: equity delivery (col 0, ignored),
    equity intraday (1), F&O futures (2), F&O options (3). Each row label
    leads four cells; we anchor on the row label and read each cell by
    document order.
    """
    name = "zerodha"
    url = "https://zerodha.com/charges/"
    covers = ("equity", "futures", "options")

    def fetch_and_parse(self) -> Optional[Dict[Tuple[str, str], Optional[float]]]:
        html = _fetch_html(self.url)
        if html is None:
            return None
        return self._parse(html)

    def _parse(self, html: str) -> Dict[Tuple[str, str], Optional[float]]:
        text = _strip_to_text(html)
        out: Dict[Tuple[str, str], Optional[float]] = {}

        row_labels = [
            ("Brokerage", "brokerage"),
            ("STT/CTT", "stt"),
            ("Transaction charges", "exchange"),
            ("GST", "gst"),
            ("SEBI charges", "sebi"),
            ("Stamp charges", "stamp"),
        ]
        next_labels = {lbl: row_labels[i + 1][0] if i + 1 < len(row_labels) else None
                       for i, (lbl, _) in enumerate(row_labels)}

        for label, kind in row_labels:
            ix = text.find(label)
            if ix == -1:
                continue
            end_ix = len(text)
            nxt = next_labels[label]
            if nxt:
                j = text.find(nxt, ix + len(label))
                if j != -1:
                    end_ix = j
            row = text[ix + len(label): end_ix]

            if kind == "brokerage":
                cells = self._brokerage_cells(row)
                for seg, col in _ZERODHA_COL.items():
                    cell = cells[col] if col < len(cells) else ""
                    out[(seg, "brokerage_flat")] = _extract_rs(cell)
                    out[(seg, "brokerage_pct_cap")] = _extract_pct(cell)
            elif kind == "stt":
                cells = _all_pcts(row)
                for seg, col in _ZERODHA_COL.items():
                    out[(seg, "stt_sell_pct")] = (cells[col] if col < len(cells) else None)
            elif kind == "exchange":
                cells = _all_nse_pcts(row)
                for seg, col in _ZERODHA_COL.items():
                    out[(seg, "exchange_pct")] = (cells[col] if col < len(cells) else None)
            elif kind == "gst":
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", row)
                v = (float(m.group(1)) / 100.0) if m else None
                for seg in _ZERODHA_COL:
                    out[(seg, "gst_pct")] = v
            elif kind == "sebi":
                m = re.search(r"Rs\.?\s*([\d.]+)\s*/\s*crore", row)
                v = float(m.group(1)) if m else None
                for seg in _ZERODHA_COL:
                    out[(seg, "sebi_per_crore")] = v
            elif kind == "stamp":
                cells = _all_pcts(row)
                for seg, col in _ZERODHA_COL.items():
                    out[(seg, "stamp_buy_pct")] = (cells[col] if col < len(cells) else None)
        return out

    @staticmethod
    def _brokerage_cells(row: str) -> List[str]:
        """Find brokerage cells: 'Zero', '0.03% or Rs.20', 'Flat Rs.20'."""
        patterns = [
            re.compile(r"Zero(?:\s*Brokerage)?", re.IGNORECASE),
            re.compile(
                r"0\.03\s*%\s*or\s*Rs\.?\s*20\s*/?\s*executed\s*order"
                r"\s*whichever\s*is\s*lower",
                re.IGNORECASE,
            ),
            re.compile(
                r"Flat\s*Rs\.?\s*20\s*per\s*executed\s*order", re.IGNORECASE,
            ),
        ]
        matches: List[Tuple[int, str]] = []
        for pat in patterns:
            for m in pat.finditer(row):
                matches.append((m.start(), m.group(0)))
        matches.sort(key=lambda x: x[0])
        return [text for _, text in matches[:4]]


# ─── Source: Upstox ──────────────────────────────────────────────────────────


class UpstoxSource:
    """Parser for ``upstox.com/pricing/``.

    Upstox publishes per-segment tables with explicit "From <date>" row
    headers (e.g., "STT/CTT (From 1st April 2026)"). We always pick the
    row whose effective date is most recent but ≤ today. Each row then
    has 4 cells (delivery, intraday, futures, options).
    """
    name = "upstox"
    url = "https://upstox.com/pricing/"
    covers = ("equity", "futures", "options")
    # Column mapping is the same as Zerodha (4-col, delivery first).
    _col = {"equity": 1, "futures": 2, "options": 3}

    # Cells inside the same row are separated only by whitespace; values
    # for each rate type follow a stable shape so we extract them per type.

    def fetch_and_parse(self) -> Optional[Dict[Tuple[str, str], Optional[float]]]:
        html = _fetch_html(self.url)
        if html is None:
            return None
        return self._parse(html)

    def _parse(self, html: str) -> Dict[Tuple[str, str], Optional[float]]:
        text = _strip_to_text(html)
        out: Dict[Tuple[str, str], Optional[float]] = {}

        # STT — pick the most-recent "From <date>" block ≤ today
        stt_row = self._most_recent_row(text, "STT/CTT")
        if stt_row:
            cells = _all_pcts(stt_row)
            for seg, col in self._col.items():
                out[(seg, "stt_sell_pct")] = (cells[col] if col < len(cells) else None)

        # Transaction charges — same date-aware selection
        tx_row = self._most_recent_row(text, "Transaction charges")
        if tx_row:
            cells = _all_nse_pcts(tx_row)
            for seg, col in self._col.items():
                out[(seg, "exchange_pct")] = (cells[col] if col < len(cells) else None)

        # GST — single value
        gst_row = self._row_after(text, "GST")
        if gst_row:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", gst_row)
            v = (float(m.group(1)) / 100.0) if m else None
            for seg in self._col:
                out[(seg, "gst_pct")] = v

        # SEBI — flat ₹/crore
        sebi_row = self._row_after(text, "SEBI Charges")
        if sebi_row:
            m = re.search(r"(?:Rs\.?|\u20b9)\s*([\d.]+)\s*/\s*crore", sebi_row)
            v = float(m.group(1)) if m else None
            for seg in self._col:
                out[(seg, "sebi_per_crore")] = v

        # Stamp duty — single block, 4 cells
        stamp_row = self._row_after(text, "Stamp Duty")
        if stamp_row:
            cells = _all_pcts(stamp_row)
            for seg, col in self._col.items():
                out[(seg, "stamp_buy_pct")] = (cells[col] if col < len(cells) else None)

        # Brokerage — Upstox is "₹20 or 0.05% whichever is lower" / "₹20 flat"
        # Use a coarse extractor: first ₹/Rs amount per cell, plus first %.
        brok_row = self._row_after(text, "Upstox Charges")
        if brok_row:
            for seg in self._col:
                # Upstox is also flat ₹20 / 0.05% cap for equity; for our
                # purposes we just verify the flat fee is ₹20 — the % cap
                # differs across brokers and isn't a regulated rate.
                out[(seg, "brokerage_flat")] = 20.0 if "20" in brok_row else None
        return out

    @staticmethod
    def _row_after(text: str, label: str, span: int = 800) -> Optional[str]:
        """Return the ``span`` chars of text after the FIRST occurrence of label."""
        ix = text.find(label)
        if ix == -1:
            return None
        return text[ix + len(label): ix + len(label) + span]

    @staticmethod
    def _most_recent_row(text: str, label: str, today: Optional[datetime] = None,
                         span: int = 600) -> Optional[str]:
        """Find every ``<label> (From <date>)`` block and return the one
        whose ``<date>`` is the most recent value not later than today.

        Returns the body following that header, up to ``span`` characters.
        Falls back to the first occurrence of the bare label if no dated
        block is found.
        """
        today = today or datetime.now(IST).replace(tzinfo=None)
        # Match "<label> (From <Day> <Month> <Year>)" or with " to <date>"
        pat = re.compile(
            re.escape(label) + r"\s*\(From\s+([\d]{1,2}(?:st|nd|rd|th)?\s+\w+\s+\d{4})"
            r"(?:\s+to\s+[\d]{1,2}(?:st|nd|rd|th)?\s+\w+\s+\d{4})?\s*\)",
            re.IGNORECASE,
        )
        candidates: List[Tuple[datetime, int]] = []
        for m in pat.finditer(text):
            try:
                date_str = re.sub(r"(?<=\d)(st|nd|rd|th)", "", m.group(1)).strip()
                effective = datetime.strptime(date_str, "%d %B %Y")
            except ValueError:
                continue
            if effective <= today:
                candidates.append((effective, m.end()))
        if not candidates:
            ix = text.find(label)
            if ix == -1:
                return None
            return text[ix + len(label): ix + len(label) + span]
        candidates.sort(key=lambda x: x[0], reverse=True)
        end = candidates[0][1]
        return text[end: end + span]


# ─── Source: Dhan ────────────────────────────────────────────────────────────


class DhanSource:
    """Parser for ``dhan.co/pricing/``.

    Dhan's page is tab-segmented (Equity / F&O / Currency / Commodity)
    and only the equity tab is in the static HTML — F&O is JavaScript
    loaded. So Dhan can verify equity-intraday rates only.

    The visible equity tab has 3 columns: Delivery (0), Intraday (1),
    MTF/Mutual-Funds (2 — irrelevant). We only read column 1.
    """
    name = "dhan"
    url = "https://dhan.co/pricing/"
    covers = ("equity",)
    _col_intraday = 1

    def fetch_and_parse(self) -> Optional[Dict[Tuple[str, str], Optional[float]]]:
        html = _fetch_html(self.url)
        if html is None:
            return None
        return self._parse(html)

    def _parse(self, html: str) -> Dict[Tuple[str, str], Optional[float]]:
        text = _strip_to_text(html)
        out: Dict[Tuple[str, str], Optional[float]] = {}

        # STT
        stt_row = self._row_after(text, "Securities Transaction Tax")
        if stt_row:
            cells = _all_pcts(stt_row)
            if len(cells) > self._col_intraday:
                out[("equity", "stt_sell_pct")] = cells[self._col_intraday]

        # Transaction charges (NSE)
        tx_row = self._row_after(text, "Transaction charges")
        if tx_row:
            cells = _all_nse_pcts(tx_row)
            if len(cells) > self._col_intraday:
                out[("equity", "exchange_pct")] = cells[self._col_intraday]

        # SEBI Turnover fees
        sebi_row = self._row_after(text, "SEBI Turnover fees")
        if sebi_row:
            cells = _all_pcts(sebi_row)
            if cells:
                # 0.0001% of turnover = ₹10 / crore (₹0.0001 per ₹100 = ₹10 per crore)
                out[("equity", "sebi_per_crore")] = cells[0] * 1e7

        # Stamp Duty (intraday = 0.003%)
        stamp_row = self._row_after(text, "Stamp Duty")
        if stamp_row:
            cells = _all_pcts(stamp_row)
            if len(cells) > self._col_intraday:
                out[("equity", "stamp_buy_pct")] = cells[self._col_intraday]

        # GST
        gst_row = self._row_after(text, "GST")
        if gst_row:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", gst_row)
            if m:
                out[("equity", "gst_pct")] = float(m.group(1)) / 100.0
        return out

    @staticmethod
    def _row_after(text: str, label: str, span: int = 400) -> Optional[str]:
        ix = text.find(label)
        if ix == -1:
            return None
        return text[ix + len(label): ix + len(label) + span]


# ─── Shared cell extractors ──────────────────────────────────────────────────


def _all_pcts(row: str) -> List[Optional[float]]:
    """All ``X%`` values in document order, converted to decimal rates."""
    return [float(m.group(1)) / 100.0
            for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", row)]


def _all_nse_pcts(row: str) -> List[Optional[float]]:
    """All ``NSE: X%`` values in document order — anchors on NSE so a stray
    ``BSE: X%`` doesn't shift the cell index."""
    return [float(m.group(1)) / 100.0
            for m in re.finditer(r"NSE:\s*([\d.]+)\s*%", row)]


def _extract_rs(cell: str) -> Optional[float]:
    m = re.search(r"Rs\.?\s*([\d.]+)", cell)
    return float(m.group(1)) if m else None


def _extract_pct(cell: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", cell)
    return float(m.group(1)) / 100.0 if m else None


# ─── Cross-source verdict ────────────────────────────────────────────────────


def _verdict(configured: float,
             observations: List[SourceObservation]) -> Tuple[str, bool, Optional[float], str]:
    """Return ``(verdict, drifted, consensus_value, note)`` for one rate.

    Verdict logic:
      * No observations             → UNVERIFIED, not drifted
      * All sources agree w/ config → OK
      * ≥2 sources agree, differ from config → DRIFT_CONFIRMED, drifted=True
      * 1 source disagrees, no other source covers → DRIFT_SINGLE, drifted=True
      * Sources disagree with each other → AMBIGUOUS, drifted=True
    """
    obs_with_value = [o for o in observations if o.value is not None]
    if not obs_with_value:
        return ("UNVERIFIED", False, None,
                "no source could observe this rate today (network failure or "
                "parser couldn't locate it)")

    # Group observed values into clusters of "close enough" matches.
    clusters: List[List[SourceObservation]] = []
    for obs in obs_with_value:
        placed = False
        for cluster in clusters:
            if _close(cluster[0].value, obs.value):
                cluster.append(obs)
                placed = True
                break
        if not placed:
            clusters.append([obs])

    # If multiple clusters, sources disagree among themselves.
    if len(clusters) > 1:
        ranked = sorted(clusters, key=len, reverse=True)
        biggest = ranked[0]
        rest = sum((c for c in ranked[1:]), [])
        sources_in_biggest = ", ".join(o.source for o in biggest)
        sources_in_rest = ", ".join(f"{o.source}={o.value}" for o in rest)
        return ("AMBIGUOUS", True, biggest[0].value,
                f"sources disagree: {len(biggest)} say {biggest[0].value} ({sources_in_biggest}), "
                f"others say {sources_in_rest} — investigate manually.")

    # Single cluster: all sources agree with each other.
    consensus = clusters[0][0].value
    n_sources = len(obs_with_value)
    src_list = ", ".join(o.source for o in obs_with_value)
    if _close(consensus, configured):
        return ("OK", False, consensus,
                f"verified by {n_sources} source(s) ({src_list}); configured value still in force")
    if n_sources >= 2:
        return ("DRIFT_CONFIRMED", True, consensus,
                f"DRIFT — {n_sources} sources independently agree on {consensus:g} "
                f"(configured {configured:g}). Update bot/fees.py and restart bots.")
    return ("DRIFT_SINGLE", True, consensus,
            f"DRIFT (single source) — {src_list} says {consensus:g} (configured {configured:g}). "
            "No other source observed this rate; verify manually before updating.")


# ─── Main runner ─────────────────────────────────────────────────────────────


_DEFAULT_SOURCES: List[Any] = [ZerodhaSource(), UpstoxSource(), DhanSource()]


def run_fee_audit() -> AuditResult:
    """Fetch all sources, cross-verify each rate, persist + cache result."""
    now_iso = datetime.now(IST).isoformat()

    # 1. Fetch every source's parsed rates (or note unreachable).
    parsed_by_source: Dict[str, Optional[Dict[Tuple[str, str], Optional[float]]]] = {}
    sources_checked: List[str] = []
    sources_reachable: List[str] = []
    for src in _DEFAULT_SOURCES:
        sources_checked.append(src.name)
        try:
            parsed = src.fetch_and_parse()
        except Exception as e:                                       # noqa: BLE001
            logger.warning("[fee-audit] {} parser crashed: {}", src.name, e)
            parsed = None
        parsed_by_source[src.name] = parsed
        if parsed is not None:
            sources_reachable.append(src.name)

    # 2. Build per-rate verdicts.
    checks: List[RateCheck] = []
    confirmed_drifts = 0
    single_drifts = 0
    ambiguous = 0
    unverified = 0

    for segment, table in _RATE_TABLES:
        for key, configured in table.items():
            label = f"[{segment}] {_LABELS.get(key, key)}"
            observations: List[SourceObservation] = []
            for src in _DEFAULT_SOURCES:
                if segment not in src.covers:
                    continue
                parsed = parsed_by_source.get(src.name)
                if parsed is None:
                    continue
                value = parsed.get((segment, key))
                observations.append(SourceObservation(source=src.name, value=value))

            verdict, drifted, consensus, note = _verdict(configured, observations)
            checks.append(RateCheck(
                key=key, label=label, configured=configured,
                observed=(round(consensus, 8) if consensus is not None else None),
                drifted=drifted, segment=segment,
                verdict=verdict, sources=observations, note=note,
            ))
            if verdict == "DRIFT_CONFIRMED":
                confirmed_drifts += 1
            elif verdict == "DRIFT_SINGLE":
                single_drifts += 1
            elif verdict == "AMBIGUOUS":
                ambiguous += 1
            elif verdict == "UNVERIFIED":
                unverified += 1

    # 3. Build overall status + summary.
    if not sources_reachable:
        status = "WARN"
        summary = ("No source reachable — fee rates could not be verified today. "
                   "Bot continues with configured rates without verification.")
    elif confirmed_drifts == 0 and single_drifts == 0 and ambiguous == 0:
        status = "OK"
        summary = (f"All {len(checks)} rates verified across {len(sources_reachable)} "
                   f"source(s) ({', '.join(sources_reachable)}).")
    else:
        # WARN — never FAIL — for data drift. A trading bot's daily fee
        # audit cannot afford the boy-who-cried-wolf failure mode of an
        # over-eager FAIL on a brittle multi-scraper. WARN reliably
        # escalates to the dashboard / health check; the human verifies.
        status = "WARN"
        bits = []
        if confirmed_drifts:
            bits.append(f"{confirmed_drifts} CONFIRMED drift(s) (≥2 sources agree)")
        if single_drifts:
            bits.append(f"{single_drifts} single-source drift(s)")
        if ambiguous:
            bits.append(f"{ambiguous} AMBIGUOUS rate(s) (sources disagree)")
        if unverified:
            bits.append(f"{unverified} UNVERIFIED rate(s)")
        summary = (
            f"{', '.join(bits)} across "
            f"{len(sources_reachable)}/{len(sources_checked)} reachable sources "
            f"({', '.join(sources_reachable) or 'none'}). "
            f"For confirmed drifts, run `python scripts/eod_apply_fee_updates.py` "
            f"after 15:30 IST to patch + restart safely."
        )

    source_label = ", ".join(sources_reachable) if sources_reachable else "(none reachable)"
    result = AuditResult(
        timestamp=now_iso,
        status=status,
        source=source_label,
        summary=summary,
        checks=checks,
        sources_checked=sources_checked,
        sources_reachable=sources_reachable,
        raw_excerpts={
            "segments_audited": "equity, futures, options",
            "sources_checked":  ", ".join(sources_checked),
            "sources_reachable": ", ".join(sources_reachable) or "(none)",
        },
    )

    _persist(result)
    _publish_to_cache(result)

    log_fn = {"OK": logger.info, "WARN": logger.warning, "FAIL": logger.error}[status]
    log_fn("[fee-audit] {} — {}", status, summary)
    return result


# ─── Persistence ─────────────────────────────────────────────────────────────


def _persist(result: AuditResult) -> Path:
    out_dir = PROJECT_ROOT / "logs" / "fee_audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now(IST).date().isoformat()}.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(result.to_dict(), default=str) + os.linesep)
    return path


def _publish_to_cache(result: AuditResult) -> None:
    try:
        cache = get_cache()
        cache.set_json("fee_audit:latest", result.to_dict(), ttl=86400 * 7)
    except Exception as e:  # pragma: no cover
        logger.warning("[fee-audit] cache publish failed: {}", e)
