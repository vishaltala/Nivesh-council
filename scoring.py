"""Deterministic rule-based scoring engine — the always-works fallback.

Every number an agent cites is read directly from the evidence bundle.
Missing fields (None / listed in data_gaps) are skipped rather than guessed.
"""
import config


def _get(evidence, path, default=None):
    cur = evidence
    for part in path.split("."):
        if not isinstance(cur, dict) or cur.get(part) is None:
            return default
        cur = cur[part]
    return cur


def score_bull(evidence):
    score = 0
    reasons = []

    rvol = _get(evidence, "technicals.rvol")
    if rvol is not None:
        pts = min(round(rvol * 8), 25)
        if pts > 0:
            score += pts
            reasons.append(f"RVOL {rvol:.2f}x confirms volume interest")
    else:
        reasons.append("RVOL data unavailable")

    pos = _get(evidence, "range_52w.position_pct")
    if pos is not None and pos >= 85:
        score += 15
        reasons.append(f"Trading at {pos:.0f}% of its 52w range — near breakout")

    vs_sma = _get(evidence, "technicals.price_vs_sma_pct")
    trend = _get(evidence, "technicals.trend")
    if vs_sma is not None and trend == "up" and vs_sma > 0:
        score += 15
        reasons.append(f"{vs_sma:.1f}% above SMA in an uptrend")

    drp = _get(evidence, "technicals.day_range_position_pct")
    if drp is not None and drp >= 70:
        score += 10
        reasons.append(f"Closed strong at {drp:.0f}% of today's range")

    upside = _get(evidence, "analyst.upside_pct")
    if upside is not None and upside >= 10:
        score += 15
        reasons.append(f"Analyst target implies {upside:.1f}% upside")

    buy_pct = _get(evidence, "analyst.buy_pct")
    if buy_pct is not None and buy_pct >= 80:
        score += 10
        reasons.append(f"{buy_pct:.0f}% of analysts rate it Buy")

    news = evidence.get("news") or {}
    pos_n, neg_n = news.get("positive"), news.get("negative")
    if pos_n is not None and neg_n is not None and pos_n > neg_n:
        score += 10
        reasons.append(f"News tone skews positive ({pos_n} vs {neg_n} negative)")

    wret = _get(evidence, "technicals.window_return_pct")
    if wret is not None and wret > 0:
        score += 10
        reasons.append(f"Up {wret:.1f}% over the trailing window")

    return min(score, 100), reasons


def score_bear(evidence):
    score = 0
    reasons = []

    rvol = _get(evidence, "technicals.rvol")
    if rvol is not None and rvol < 1:
        score += 15
        reasons.append(f"RVOL only {rvol:.2f}x — weak conviction")

    pos = _get(evidence, "range_52w.position_pct")
    if pos is not None and pos < 30:
        score += 15
        reasons.append(f"Only {pos:.0f}% of its 52w range — close to lows")

    vs_sma = _get(evidence, "technicals.price_vs_sma_pct")
    trend = _get(evidence, "technicals.trend")
    if trend == "down" or (vs_sma is not None and vs_sma < 0):
        score += 15
        reasons.append(f"Trend is {trend or 'down'}, {vs_sma if vs_sma is not None else 0:.1f}% vs SMA")

    upside = _get(evidence, "analyst.upside_pct")
    if upside is not None and upside <= 0:
        score += 15
        reasons.append(f"No headroom to target ({upside:.1f}% upside)")

    buy_pct = _get(evidence, "analyst.buy_pct")
    if buy_pct is not None and buy_pct < 55:
        score += 10
        reasons.append(f"Low analyst conviction ({buy_pct:.0f}% buy ratings)")

    pct_from_high = _get(evidence, "range_52w.pct_from_high")
    if pct_from_high is not None and pct_from_high <= -20:
        score += 10
        reasons.append(f"{pct_from_high:.1f}% below its 52w high")

    sell_pct = _get(evidence, "analyst.sell_pct")
    if sell_pct is not None and sell_pct >= 20:
        score += 10
        reasons.append(f"{sell_pct:.0f}% of analysts rate it Sell")

    news = evidence.get("news") or {}
    pos_n, neg_n = news.get("positive"), news.get("negative")
    if pos_n is not None and neg_n is not None and neg_n > pos_n:
        score += 10
        reasons.append(f"News tone skews negative ({neg_n} vs {pos_n} positive)")

    drp = _get(evidence, "technicals.day_range_position_pct")
    if drp is not None and drp <= 30:
        score += 10
        reasons.append(f"Weak close at {drp:.0f}% of today's range")

    return min(score, 100), reasons


def score_technician(evidence):
    trend = _get(evidence, "technicals.trend")
    vs_sma = _get(evidence, "technicals.price_vs_sma_pct")
    rvol = _get(evidence, "technicals.rvol")
    drp = _get(evidence, "technicals.day_range_position_pct")
    ma_alignment = _get(evidence, "technicals.ma_alignment")
    rsi = _get(evidence, "technicals.rsi_14")
    macd_trend = _get(evidence, "technicals.macd_trend")

    score = 50
    parts = []
    if trend is not None:
        score += 15 if trend == "up" else (-15 if trend == "down" else 0)
        parts.append(f"trend {trend}")
    else:
        parts.append("trend unknown")
    if vs_sma is not None:
        score += max(-15, min(15, vs_sma))
        parts.append(f"{vs_sma:+.1f}% vs SMA")
    if rvol is not None:
        score += min(15, rvol * 5)
        parts.append(f"RVOL {rvol:.2f}x")
    if drp is not None:
        score += (drp - 50) * 0.2

    # Deeper technicals: 50/200-day average relationship, RSI, MACD momentum.
    if ma_alignment is not None:
        score += 10 if ma_alignment == "bullish" else -10
        parts.append(f"50/200-day average {ma_alignment}")
    if rsi is not None:
        if rsi >= 70:
            score -= 8
            parts.append(f"RSI {rsi:.0f} (overbought — may be overextended)")
        elif rsi <= 30:
            score -= 8
            parts.append(f"RSI {rsi:.0f} (oversold — weak chart)")
        else:
            parts.append(f"RSI {rsi:.0f}")
    if macd_trend is not None:
        score += 8 if macd_trend == "bullish" else -8
        parts.append(f"MACD {macd_trend}")

    score = max(0, min(100, round(score)))
    return score, ", ".join(parts) if parts else "insufficient technical data"


def score_fundamentalist(evidence):
    upside = _get(evidence, "analyst.upside_pct")
    buy_pct = _get(evidence, "analyst.buy_pct")
    num_analysts = _get(evidence, "analyst.num_analysts")
    roe = _get(evidence, "fundamentals.roe_pct")
    rev_growth = _get(evidence, "fundamentals.revenue_growth_pct")
    earn_growth = _get(evidence, "fundamentals.earnings_growth_pct")
    debt_eq = _get(evidence, "fundamentals.debt_to_equity_pct")
    promoter_hold = _get(evidence, "fundamentals.promoter_insider_holding_pct")
    pe_ttm = _get(evidence, "fundamentals.pe_ttm")
    piotroski = _get(evidence, "fundamentals_trend.piotroski_f_score")
    piotroski_max = _get(evidence, "fundamentals_trend.piotroski_max")
    earnings_quality = _get(evidence, "fundamentals_trend.earnings_quality")
    margin_trend = _get(evidence, "fundamentals_trend.net_margin_trend")

    score = 50
    parts = []
    if upside is not None:
        score += max(-20, min(25, upside))
        parts.append(f"{upside:+.1f}% to target")
    else:
        parts.append("target price unavailable")
    if buy_pct is not None:
        score += (buy_pct - 50) * 0.3
        parts.append(f"{buy_pct:.0f}% buy ratings")
    if num_analysts is not None:
        parts.append(f"{num_analysts} analysts covering")

    # Deeper fundamentals (yfinance): ROE, YoY growth, leverage, and promoter/insider
    # holding — the same ratios screener.in-style fundamental analysis leans on.
    if roe is not None:
        if roe >= 15:
            score += 10
        elif roe < 8:
            score -= 10
        parts.append(f"ROE {roe:.1f}%")
    if rev_growth is not None:
        if rev_growth >= 15:
            score += 10
        elif rev_growth < 0:
            score -= 10
        parts.append(f"revenue growth {rev_growth:+.1f}% YoY")
    if earn_growth is not None:
        if earn_growth >= 15:
            score += 8
        elif earn_growth < 0:
            score -= 8
        parts.append(f"earnings growth {earn_growth:+.1f}% YoY")
    if debt_eq is not None:
        if debt_eq < 50:
            score += 8
        elif debt_eq > 150:
            score -= 12
        parts.append(f"D/E {debt_eq:.0f}%")
    if promoter_hold is not None:
        if promoter_hold >= 50:
            score += 5
        elif promoter_hold < 20:
            score -= 5
        parts.append(f"promoter holding {promoter_hold:.1f}%")
    if pe_ttm is not None:
        # PE alone doesn't score directionally — "expensive" vs "cheap" depends on
        # sector/growth context this app doesn't have — but it's still cited for
        # transparency in the reasoning.
        parts.append(f"P/E {pe_ttm:.1f}x")

    # Multi-year trend + a standard Piotroski F-Score (see data_sources.py) — looks
    # at the last several years of actual filings, not just today's snapshot.
    if piotroski is not None and piotroski_max:
        piotroski_frac = piotroski / piotroski_max
        if piotroski_frac >= 0.78:  # roughly 7/9 or better — the textbook "strong" band
            score += 12
        elif piotroski_frac <= 0.33:  # roughly 3/9 or worse — the textbook "weak" band
            score -= 12
        parts.append(f"Piotroski F-Score {piotroski}/{piotroski_max}")
    if earnings_quality is not None:
        score += 8 if earnings_quality == "healthy" else -8
        parts.append(f"earnings quality {earnings_quality} (cash flow vs reported profit)")
    if margin_trend is not None:
        if margin_trend == "improving":
            score += 8
        elif margin_trend == "declining":
            score -= 8
        parts.append(f"margin trend {margin_trend} (multi-year)")

    score = max(0, min(100, round(score)))
    return score, ", ".join(parts)


def score_newsdesk(evidence):
    news = evidence.get("news") or {}
    pos_n, neg_n, neu_n, total = (
        news.get("positive"),
        news.get("negative"),
        news.get("neutral"),
        news.get("total"),
    )

    score = 50
    if pos_n is not None and neg_n is not None and total:
        net = pos_n - neg_n
        score += max(-30, min(30, net * 10))
        reasons = f"{pos_n} positive / {neg_n} negative / {neu_n or 0} neutral of {total} headlines"
    else:
        reasons = "no recent headlines found"

    score = max(0, min(100, round(score)))
    return score, reasons


def compute_trade_levels(evidence):
    """Formula-derived entry/stop-loss/take-profit for a BUY signal — never invented.

    entry = evidence.price.live
    stop_loss = entry - (entry * SL_PERCENT/100)
    take_profit = entry + (entry - stop_loss) * RISK_REWARD_RATIO

    Returns None if evidence.price.live is missing (no number to derive from).
    Single source of truth shared by both engines (scoring.py, llm.py) so a BUY
    signal's entry/SL/TP are identical no matter which engine produced the verdict.
    """
    raw_entry = _get(evidence, "price.live")
    if raw_entry is None:
        return None

    entry = round(float(raw_entry), 2)
    sl_fraction = config.SL_PERCENT / 100
    stop_loss = round(entry - (entry * sl_fraction), 2)
    take_profit = round(entry + (entry - stop_loss) * config.RISK_REWARD_RATIO, 2)

    sl_pct = round((entry - stop_loss) / entry * 100, 2) if entry else None
    risk_reward_pct = round((take_profit - entry) / entry * 100, 2) if entry else None

    levels = {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "sl_pct": sl_pct,
        "risk_reward_pct": risk_reward_pct,
        "rr_ratio": config.RISK_REWARD_RATIO,
        "analyst_target_note": None,
    }

    target_mean = _get(evidence, "analyst.target_mean")
    if target_mean is not None and target_mean > take_profit:
        levels["analyst_target_note"] = f"Analyst target: ₹{target_mean:.2f}"

    return levels


def check_technical_confirmation(evidence):
    """Real market-interest check: near its 52-week high, or trading on unusually
    high volume today. In the deterministic engine this directly gates a BUY — a
    good net score alone isn't enough, there has to be real interest behind it. In
    the LLM engine it's informational (the LLM forms its own judgment) but is still
    computed and reported the same way for every run, so the audit trail always
    shows whether this signal was present, regardless of which engine decided."""
    pos = _get(evidence, "range_52w.position_pct") or 0
    rvol = _get(evidence, "technicals.rvol") or 0
    return pos >= 60 or rvol >= 3


def judge(evidence, bull_score, bull_reasons, bear_score, bear_reasons):
    net = bull_score - bear_score
    leadership = check_technical_confirmation(evidence)

    if net >= 25 and leadership:
        verdict = "BUY"
    elif net <= -15:
        verdict = "AVOID"
    else:
        verdict = "WATCH"

    confidence = max(1, min(10, round(4 + net / 15)))
    confidence = max(confidence, 7) if verdict == "BUY" else min(confidence, 6)

    winner = "Bull" if bull_score >= bear_score else "Bear"
    top_reasons = bull_reasons if winner == "Bull" else bear_reasons
    key_catalyst = top_reasons[0] if top_reasons else "No standout factor"

    if winner == "Bull":
        rationale = f"Bull case leads {bull_score} vs {bear_score}. {top_reasons[0] if top_reasons else ''}"
    else:
        rationale = f"Bear case leads {bear_score} vs {bull_score}. {top_reasons[0] if top_reasons else ''}"
    rationale = rationale.strip()

    trade_levels = compute_trade_levels(evidence)

    return {
        "winner": winner,
        "verdict": verdict,
        "confidence": confidence,
        "rationale": rationale,
        "key_catalyst": key_catalyst,
        "bull_score": bull_score,
        "bear_score": bear_score,
        "net": net,
        "technical_confirmation": leadership,
        "entry": trade_levels["entry"] if trade_levels else None,
        "stop_loss": trade_levels["stop_loss"] if trade_levels else None,
        "take_profit": trade_levels["take_profit"] if trade_levels else None,
        "sl_pct": trade_levels["sl_pct"] if trade_levels else None,
        "risk_reward_pct": trade_levels["risk_reward_pct"] if trade_levels else None,
        "rr_ratio": trade_levels["rr_ratio"] if trade_levels else None,
        "analyst_target_note": trade_levels["analyst_target_note"] if trade_levels else None,
    }


def evaluate(evidence):
    bull_score, bull_reasons = score_bull(evidence)
    bear_score, bear_reasons = score_bear(evidence)
    tech_score, tech_reasons = score_technician(evidence)
    fund_score, fund_reasons = score_fundamentalist(evidence)
    news_score, news_reasons = score_newsdesk(evidence)
    verdict = judge(evidence, bull_score, bull_reasons, bear_score, bear_reasons)

    return {
        "scores": {
            "bull": {"score": bull_score, "reasons": "; ".join(bull_reasons) if bull_reasons else "No strong bullish factors found"},
            "bear": {"score": bear_score, "reasons": "; ".join(bear_reasons) if bear_reasons else "No strong bearish factors found"},
            "technician": {"score": tech_score, "reasons": tech_reasons},
            "fundamentalist": {"score": fund_score, "reasons": fund_reasons},
            "newsdesk": {"score": news_score, "reasons": news_reasons},
        },
        "verdict": verdict,
        "engine": "deterministic",
        "verified": True,
        "unverified_claims": [],
    }
