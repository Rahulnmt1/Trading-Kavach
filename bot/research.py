"""Pre-market research agent.

For each symbol in the watchlist:
  1. Fetch overnight gap (today's open vs yesterday's close)
  2. Compute simple technical posture (above/below 9, 21 EMAs)
  3. Pull recent news headlines from the configured RSS feeds
  4. Optionally call an LLM to score and rank, with a structured rationale

If no OPENAI_API_KEY is set, falls back to a pure heuristic ranker.
Output is cached in Redis under `research:YYYY-MM-DD`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from .cache import get_cache
from .config import env, load_config
from .data import daily_history, history, previous_close, todays_open
from .indicators import ema, rsi
from .logger import logger


@dataclass
class StockBrief:
    symbol: str
    last_close: float
    gap_pct: float
    above_ema9: bool
    above_ema21: bool
    rsi14: float
    headlines: List[str] = field(default_factory=list)


@dataclass
class ResearchPick:
    symbol: str
    bias: str          # "long" | "short" | "neutral"
    score: float       # 0..1
    rationale: str


def _technical_brief(symbol: str) -> Optional[StockBrief]:
    df = history(symbol, days=5, interval="15m")
    daily = daily_history(symbol, days=30)
    if df.empty or daily.empty:
        return None
    close = float(df["close"].iloc[-1])
    pc = previous_close(symbol) or float(daily["close"].iloc[-2])
    o = todays_open(symbol) or close
    gap = (o - pc) / pc * 100.0 if pc else 0.0
    e9 = float(ema(df["close"], 9).iloc[-1])
    e21 = float(ema(df["close"], 21).iloc[-1])
    r = float(rsi(df["close"], 14).iloc[-1])
    return StockBrief(
        symbol=symbol, last_close=close, gap_pct=gap,
        above_ema9=close > e9, above_ema21=close > e21, rsi14=r,
    )


def _fetch_headlines(symbol: str, max_items: int = 5) -> List[str]:
    feeds = env().news_feed_list
    if not feeds:
        return []
    try:
        import feedparser  # type: ignore
    except ImportError:
        return []
    out: List[str] = []
    for url in feeds:
        try:
            d = feedparser.parse(url)
            for entry in d.entries[:30]:
                title = getattr(entry, "title", "")
                if symbol.lower() in title.lower():
                    out.append(title)
                if len(out) >= max_items:
                    return out
        except Exception as e:
            logger.warning("feed {} failed: {}", url, e)
    return out


def _heuristic_rank(briefs: List[StockBrief]) -> List[ResearchPick]:
    picks: List[ResearchPick] = []
    for b in briefs:
        score = 0.0
        bias = "neutral"
        reasons: List[str] = []
        if b.above_ema9 and b.above_ema21 and b.gap_pct > 0:
            bias, score = "long", 0.6
            reasons.append("price above 9 & 21 EMAs with positive gap")
        elif (not b.above_ema9) and (not b.above_ema21) and b.gap_pct < 0:
            bias, score = "short", 0.6
            reasons.append("price below 9 & 21 EMAs with negative gap")

        if abs(b.gap_pct) > 1.5:
            score += 0.1
            reasons.append(f"strong gap {b.gap_pct:+.2f}%")
        if 30 < b.rsi14 < 70:
            score += 0.05
            reasons.append(f"RSI healthy at {b.rsi14:.1f}")
        elif b.rsi14 >= 75 and bias == "long":
            score -= 0.15
            reasons.append("RSI overbought")
        elif b.rsi14 <= 25 and bias == "short":
            score -= 0.15
            reasons.append("RSI oversold")

        if b.headlines:
            score += 0.05
            reasons.append(f"{len(b.headlines)} fresh headlines")

        picks.append(ResearchPick(
            symbol=b.symbol, bias=bias, score=max(0.0, min(score, 1.0)),
            rationale="; ".join(reasons) or "no edge detected",
        ))
    picks.sort(key=lambda p: p.score, reverse=True)
    return picks


def _llm_rank(briefs: List[StockBrief]) -> Optional[List[ResearchPick]]:
    e_ = env()
    if not e_.OPENAI_API_KEY:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=e_.OPENAI_API_KEY)
    except Exception as ex:
        logger.warning("OpenAI unavailable: {}", ex)
        return None

    payload = [
        {
            "symbol": b.symbol,
            "last_close": round(b.last_close, 2),
            "gap_pct": round(b.gap_pct, 3),
            "above_ema9": b.above_ema9,
            "above_ema21": b.above_ema21,
            "rsi14": round(b.rsi14, 1),
            "headlines": b.headlines,
        }
        for b in briefs
    ]

    sys = (
        "You are a senior intraday equity analyst for the NSE. "
        "Given pre-market technical and news data on each stock, return a JSON object "
        "with key `picks` containing an array sorted by trade quality DESCENDING. "
        "Each pick has: symbol (string), bias (one of: long, short, neutral), "
        "score (float 0..1), rationale (1-2 short sentences). "
        "Be conservative — give score > 0.6 only for clear setups. Return ONLY JSON."
    )

    try:
        resp = client.chat.completions.create(
            model=e_.OPENAI_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": json.dumps(payload)},
            ],
            temperature=0.3,
        )
        data = json.loads(resp.choices[0].message.content)
        return [
            ResearchPick(
                symbol=p["symbol"], bias=p.get("bias", "neutral"),
                score=float(p.get("score", 0)), rationale=p.get("rationale", ""),
            )
            for p in data.get("picks", [])
        ]
    except Exception as e:
        logger.warning("LLM rank failed, falling back to heuristic: {}", e)
        return None


def run_research() -> List[ResearchPick]:
    cfg = load_config()
    briefs: List[StockBrief] = []
    for sym in cfg.symbols:
        b = _technical_brief(sym)
        if b is None:
            continue
        b.headlines = _fetch_headlines(sym)
        briefs.append(b)

    picks = _llm_rank(briefs) or _heuristic_rank(briefs)
    picks = picks[: cfg.research.top_n]

    cache = get_cache()
    cache.set_json(
        f"research:{date.today().isoformat()}",
        [p.__dict__ for p in picks],
        ttl=86400,
    )
    logger.info("Research complete — top {} picks: {}",
                len(picks), [(p.symbol, p.bias, round(p.score, 2)) for p in picks])
    return picks


def todays_picks() -> List[ResearchPick]:
    cache = get_cache()
    raw = cache.get_json(f"research:{date.today().isoformat()}")
    if not raw:
        return []
    return [ResearchPick(**p) for p in raw]
