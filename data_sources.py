"""Two data modes behind one interface: demo (offline JSON bundles) and live (yfinance).

Both modes produce the same normalized "evidence bundle" shape so scoring.py / llm.py
never need to know which mode built the data.
"""
import glob
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_DIR = os.path.join(BASE_DIR, "universes")
DEMO_DIR = os.path.join(BASE_DIR, "demo_data")

# Verified working Indian markets RSS feeds (checked live, not guessed — a couple of
# other commonly-cited "RSS" URLs turned out to redirect to a login page instead).
RSS_FEEDS = [
    ("Economic Times", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("LiveMint", "https://www.livemint.com/rss/markets"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
]

POSITIVE_WORDS = {
    "surge", "surges", "rally", "rallies", "beat", "beats", "upgrade", "upgrades",
    "outperform", "record", "growth", "profit", "profits", "gain", "gains", "strong",
    "bullish", "buy", "positive", "soar", "soars", "jump", "jumps", "high", "wins",
    "win", "expansion", "robust", "rebound", "boost", "boosts", "milestone", "order",
    "orders", "contract", "expand", "expands", "raise", "raises", "optimistic",
}
NEGATIVE_WORDS = {
    "fall", "falls", "plunge", "plunges", "drop", "drops", "downgrade", "downgrades",
    "underperform", "loss", "losses", "decline", "declines", "weak", "bearish",
    "sell", "negative", "crash", "miss", "misses", "low", "cut", "cuts", "concern",
    "concerns", "probe", "fraud", "lawsuit", "slump", "slumps", "layoff", "layoffs",
    "penalty", "fine", "fined", "delay", "delays", "recall", "warns", "warning",
}


def _classify_headline(title):
    if not title:
        return "neutral"
    words = {w.strip(".,:;!?()'\"").lower() for w in title.split()}
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _fmt_ist(dt):
    """Normalize any timezone-aware (or naive-UTC) datetime to a readable IST string,
    so a headline's date reads the same regardless of which source it came from."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


def _parse_rss_date(raw):
    """RSS pubDate is RFC 2822, e.g. 'Fri, 14 Aug 2026 18:33:35 +0530'."""
    if not raw:
        return None
    try:
        return _fmt_ist(parsedate_to_datetime(raw))
    except (TypeError, ValueError):
        return None


def _parse_yf_date(raw):
    """yfinance's news date shows up either as content.pubDate (ISO 8601 string,
    e.g. '2026-08-08T00:01:00Z') on current versions, or providerPublishTime (a Unix
    timestamp) on older ones — handle both rather than assume one."""
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return _fmt_ist(datetime.fromtimestamp(raw, tz=ZoneInfo("UTC")))
        return _fmt_ist(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except (TypeError, ValueError, OSError):
        return None


def _compute_rsi(close, period=14):
    """Standard RSI (Wilder's smoothing) — the textbook 14-period version most
    charting platforms use. Returns the latest value (0-100), or None if there
    isn't enough price history yet to calculate it."""
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    last_gain, last_loss = avg_gain.iloc[-1], avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs = last_gain / last_loss
    return 100 - (100 / (1 + rs))


def _compute_macd(close, fast=12, slow=26, signal=9):
    """Standard MACD (12/26/9 EMA settings). Returns (macd_line, signal_line,
    histogram) as of the latest close, or (None, None, None) if there isn't
    enough price history yet."""
    if len(close) < slow + signal:
        return None, None, None
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


def _compute_bollinger(close, period=20, num_std=2):
    """Standard Bollinger Bands (20-period, 2 standard deviations). Returns
    (upper, lower) as of the latest close, or (None, None) if there isn't
    enough price history yet."""
    if len(close) < period:
        return None, None
    window = close.tail(period)
    mid = window.mean()
    std = window.std()
    return float(mid + num_std * std), float(mid - num_std * std)


def _statement_years(df, row):
    """All valid (non-NaN) values for a row from a yfinance annual statement,
    most-recent-year first — matches yfinance's own column ordering. Empty list
    if the statement, or that row, isn't available."""
    if df is None or df.empty or row not in df.index:
        return []
    return [v for v in df.loc[row].tolist() if v == v]  # v == v is False only for NaN


def _piotroski_score(net_income, revenue, gross_profit, assets, lt_debt, curr_assets, curr_liab, shares, cfo):
    """Standard Piotroski F-Score (Piotroski, 2000) — a well-established 9-point
    checklist of profitability, leverage, and efficiency YEAR-OVER-YEAR trend
    signals, 1 point each. 8-9 = strong fundamentals, 0-2 = weak; these are the
    textbook interpretation bands, not something invented here. Every point is a
    real comparison of two actually-reported numbers — never estimated.

    Returns (score, criteria_computed, breakdown). criteria_computed can be less
    than 9 if some inputs are missing — the score is out of however many criteria
    were actually computable, never padded out to 9 by guessing the rest as fails."""
    score = 0
    computed = 0
    breakdown = []

    def add(label, passed):
        nonlocal score, computed
        if passed is None:
            breakdown.append(f"{label}: data unavailable")
            return
        computed += 1
        if passed:
            score += 1
        breakdown.append(f"{label}: {'Yes' if passed else 'No'}")

    def at(lst, i):
        return lst[i] if len(lst) > i else None

    ni, ni_prior = at(net_income, 0), at(net_income, 1)
    rev, rev_prior = at(revenue, 0), at(revenue, 1)
    gp, gp_prior = at(gross_profit, 0), at(gross_profit, 1)
    ta, ta_prior = at(assets, 0), at(assets, 1)
    ltd, ltd_prior = at(lt_debt, 0), at(lt_debt, 1)
    ca, ca_prior = at(curr_assets, 0), at(curr_assets, 1)
    cl, cl_prior = at(curr_liab, 0), at(curr_liab, 1)
    sh, sh_prior = at(shares, 0), at(shares, 1)
    cfo0 = at(cfo, 0)

    add("Positive net income", ni > 0 if ni is not None else None)
    add("Positive operating cash flow", cfo0 > 0 if cfo0 is not None else None)

    roa = (ni / ta) if (ni is not None and ta) else None
    roa_prior = (ni_prior / ta_prior) if (ni_prior is not None and ta_prior) else None
    add("Return on assets improved YoY", (roa > roa_prior) if (roa is not None and roa_prior is not None) else None)

    add("Operating cash flow exceeds net income", (cfo0 > ni) if (cfo0 is not None and ni is not None) else None)

    ltd_ratio = (ltd / ta) if (ltd is not None and ta) else None
    ltd_ratio_prior = (ltd_prior / ta_prior) if (ltd_prior is not None and ta_prior) else None
    add(
        "Long-term debt ratio improved YoY",
        (ltd_ratio < ltd_ratio_prior) if (ltd_ratio is not None and ltd_ratio_prior is not None) else None,
    )

    cr = (ca / cl) if (ca is not None and cl) else None
    cr_prior = (ca_prior / cl_prior) if (ca_prior is not None and cl_prior) else None
    add("Current ratio improved YoY", (cr > cr_prior) if (cr is not None and cr_prior is not None) else None)

    add(
        "No new shares issued (no dilution)",
        (sh <= sh_prior) if (sh is not None and sh_prior is not None) else None,
    )

    gm = (gp / rev) if (gp is not None and rev) else None
    gm_prior = (gp_prior / rev_prior) if (gp_prior is not None and rev_prior) else None
    add("Gross margin improved YoY", (gm > gm_prior) if (gm is not None and gm_prior is not None) else None)

    atr = (rev / ta) if (rev is not None and ta) else None
    atr_prior = (rev_prior / ta_prior) if (rev_prior is not None and ta_prior) else None
    add("Asset turnover improved YoY", (atr > atr_prior) if (atr is not None and atr_prior is not None) else None)

    return score, computed, breakdown


def compute_fundamentals_trend(income_stmt, balance_sheet, cashflow):
    """Multi-year trend analysis + a standard Piotroski F-Score, computed from
    yfinance's actual annual financial statements (typically ~4 clean years) —
    not a single-quarter snapshot the rest of `fundamentals` is built from. Every
    figure here is a real reported number; nothing is estimated. Returns an
    all-None-ish dict if there isn't enough statement history to say anything
    trustworthy (needs at least 2 years of income statement + revenue data)."""
    result = {
        "years_available": 0,
        "net_margin_trend": None, "revenue_growth_streak": None,
        "cfo_to_net_income_ratio": None, "earnings_quality": None,
        "piotroski_f_score": None, "piotroski_max": None, "piotroski_breakdown": [],
    }

    net_income_years = _statement_years(income_stmt, "Net Income")
    revenue_years = _statement_years(income_stmt, "Total Revenue")
    gross_profit_years = _statement_years(income_stmt, "Gross Profit")
    assets_years = _statement_years(balance_sheet, "Total Assets")
    lt_debt_years = _statement_years(balance_sheet, "Long Term Debt")
    curr_assets_years = _statement_years(balance_sheet, "Current Assets")
    curr_liab_years = _statement_years(balance_sheet, "Current Liabilities")
    shares_years = _statement_years(balance_sheet, "Ordinary Shares Number")
    cfo_years = _statement_years(cashflow, "Operating Cash Flow")

    result["years_available"] = len(net_income_years)
    if len(net_income_years) < 2 or len(revenue_years) < 2:
        return result

    # --- multi-year trend, not a single snapshot ---
    margins_pct = [ni / rev * 100 for ni, rev in zip(net_income_years, revenue_years) if rev]
    if len(margins_pct) >= 2:
        diff = margins_pct[0] - margins_pct[-1]  # latest vs oldest available year
        if diff >= 1:
            result["net_margin_trend"] = "improving"
        elif diff <= -1:
            result["net_margin_trend"] = "declining"
        else:
            result["net_margin_trend"] = "flat"

    grew = sum(1 for i in range(len(revenue_years) - 1) if revenue_years[i] > revenue_years[i + 1])
    result["revenue_growth_streak"] = f"revenue grew in {grew} of {len(revenue_years) - 1} year(s)"

    # --- earnings quality: does reported profit actually show up as cash? ---
    if cfo_years and net_income_years and net_income_years[0]:
        ratio = cfo_years[0] / net_income_years[0]
        result["cfo_to_net_income_ratio"] = round(ratio, 2)
        result["earnings_quality"] = "healthy" if ratio >= 1 else "weak"

    # --- Piotroski F-Score ---
    score, computed, breakdown = _piotroski_score(
        net_income_years, revenue_years, gross_profit_years, assets_years,
        lt_debt_years, curr_assets_years, curr_liab_years, shares_years, cfo_years,
    )
    if computed > 0:
        result["piotroski_f_score"] = score
        result["piotroski_max"] = computed
        result["piotroski_breakdown"] = breakdown

    return result


def fetch_rss_headlines():
    """Fetch and parse all RSS_FEEDS ONCE per run — not once per stock, that would be
    3 HTTP calls x every ticker in the universe. Never raises: a dead or slow feed is
    just skipped, same "never break the run" rule as the rest of this file. Returns a
    flat list of {"title", "source", "published"} across all feeds, unfiltered —
    matching to a specific company happens later in match_rss_headlines_for_company()."""
    items = []
    for source, url in RSS_FEEDS:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item"):
                title = html.unescape((item.findtext("title") or "").strip())
                if title:
                    items.append({
                        "title": title,
                        "source": source,
                        "published": _parse_rss_date(item.findtext("pubDate")),
                    })
        except Exception:
            continue
    return items


_CORP_SUFFIX_RE = re.compile(
    r"\s+(limited|ltd\.?|corporation|corp\.?|company|co\.?|inc\.?)\s*$", re.IGNORECASE
)


def _company_search_name(name):
    """Strip a trailing corporate-entity word ('Limited', 'Ltd', 'Corp'...) so
    'Cochin Shipyard Ltd.' becomes the distinctive part that actually shows up in a
    headline: 'Cochin Shipyard'. Sector words like 'Bank' or 'Industries' are kept —
    they're often needed to tell a company apart from others in the same group."""
    cleaned = _CORP_SUFFIX_RE.sub("", name or "").strip()
    return cleaned or (name or "").strip()


def match_rss_headlines_for_company(rss_items, company_name, limit=5):
    """Filter the pre-fetched RSS pool for headlines that name-check this specific
    company. Matches on the full (suffix-stripped) company name as a substring, not
    a single word — a single word like "Tata" or "Adani" would wrongly match
    headlines about a different company in the same business group. This trades some
    recall (a headline that only says "Reliance" won't match "Reliance Industries")
    for precision — RSS here supplements yfinance's own news, it doesn't replace it."""
    search_name = _company_search_name(company_name).lower()
    if not search_name:
        return []
    matches = []
    for item in rss_items:
        if search_name in item["title"].lower():
            matches.append(item)
            if len(matches) >= limit:
                break
    return matches


def _prettify_label(raw):
    """Title-case each word, except a word that's already ALL CAPS in the filename
    (IT, FMCG, PSU...) — that's a deliberate acronym, not a word to re-case, and
    Python's plain .title() would otherwise turn "IT" into "It"."""
    words = raw.replace("_", " ").replace("-", " ").split()
    return " ".join(w if w.isupper() and len(w) > 1 else w.capitalize() for w in words)


def list_universe_sets():
    """Auto-discover stock universe sets: any {large,mid,small} JSON file dropped
    into universes/ shows up here, id = filename stem, label = prettified id.
    Same drop-a-file-in convention as demo_data/*.json — no code change needed
    to add a set 3, 4, 5..."""
    if not os.path.isdir(UNIVERSE_DIR):
        return []
    sets = []
    for path in sorted(glob.glob(os.path.join(UNIVERSE_DIR, "*.json"))):
        set_id = os.path.splitext(os.path.basename(path))[0]
        sets.append({"id": set_id, "label": _prettify_label(set_id)})
    return sets


def load_universe(set_id=None):
    sets = list_universe_sets()
    if not sets:
        raise FileNotFoundError(f"No universe sets found in {UNIVERSE_DIR}")
    ids = [s["id"] for s in sets]
    if set_id not in ids:
        set_id = ids[0]
    with open(os.path.join(UNIVERSE_DIR, f"{set_id}.json")) as f:
        return json.load(f)


def load_demo_bundles():
    bundles = []
    for path in sorted(glob.glob(os.path.join(DEMO_DIR, "*.json"))):
        with open(path) as f:
            bundles.append(json.load(f))
    return bundles


def _collect_data_gaps(bundle):
    gaps = []

    def walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                path = f"{prefix}.{k}" if prefix else k
                if isinstance(v, (dict,)):
                    walk(v, path)
                elif v is None:
                    gaps.append(path)

    walk({k: v for k, v in bundle.items() if k not in ("news", "data_gaps")})
    return gaps


def fetch_live_bundle(symbol, cap_segment, rss_items=None):
    """Build one normalized evidence bundle for `symbol` via yfinance. Never raises —
    missing fields become null and get listed in data_gaps. rss_items, if given, is
    the pre-fetched pool from fetch_rss_headlines() — headlines that name-check this
    company are folded in alongside yfinance's own news."""
    import yfinance as yf

    bundle = {
        "symbol": symbol,
        "name": symbol,
        "cap_segment": cap_segment,
        "sector": None,
        "price": {
            "live": None, "day_open": None, "day_high": None, "day_low": None,
            "prev_close": None, "day_change_pct": None, "volume": None,
        },
        "range_52w": {"high": None, "low": None, "pct_from_high": None, "position_pct": None},
        "technicals": {
            "rvol": None, "price_vs_sma_pct": None, "window_return_pct": None,
            "swing_high": None, "swing_low": None, "day_range_position_pct": None,
            "trend": None,
            "sma_20": None, "sma_50": None, "sma_200": None, "ma_alignment": None,
            "rsi_14": None, "rsi_signal": None,
            "macd": None, "macd_signal": None, "macd_histogram": None, "macd_trend": None,
            "bb_upper": None, "bb_lower": None, "bb_position_pct": None,
        },
        "analyst": {
            "consensus": None, "num_analysts": None, "buy_pct": None, "hold_pct": None,
            "sell_pct": None, "target_mean": None, "target_low": None, "target_high": None,
            "upside_pct": None,
        },
        "fundamentals": {
            "pe_ttm": None, "pe_forward": None, "price_to_book": None,
            "roe_pct": None, "gross_margin_pct": None, "operating_margin_pct": None, "net_margin_pct": None,
            "revenue_growth_pct": None, "earnings_growth_pct": None,
            "debt_to_equity_pct": None, "current_ratio": None, "dividend_yield_pct": None,
            "promoter_insider_holding_pct": None, "institutional_holding_pct": None,
            "eps_ttm": None, "book_value_per_share": None, "free_cash_flow": None,
            "market_cap": None, "beta": None,
        },
        "fundamentals_trend": {
            "years_available": 0,
            "net_margin_trend": None, "revenue_growth_streak": None,
            "cfo_to_net_income_ratio": None, "earnings_quality": None,
            "piotroski_f_score": None, "piotroski_max": None, "piotroski_breakdown": [],
        },
        "news": {"total": 0, "positive": 0, "negative": 0, "neutral": 0, "recent": []},
        "data_gaps": [],
    }

    try:
        ticker = yf.Ticker(symbol)
    except Exception:
        bundle["data_gaps"] = _collect_data_gaps(bundle) + ["all_data"]
        return bundle

    hist = None
    try:
        # Pull a full year, not just a month — needed for the 200-day average and
        # to give the RSI/MACD/Bollinger formulas enough history to be meaningful.
        hist = ticker.history(period="1y", interval="1d")
    except Exception:
        hist = None

    if hist is not None and not hist.empty and len(hist) >= 2:
        last = hist.iloc[-1]
        prev = hist.iloc[-2]
        last_close = float(last["Close"])
        prev_close = float(prev["Close"])
        day_change_pct = (last_close - prev_close) / prev_close * 100 if prev_close else None
        close = hist["Close"]

        bundle["price"]["live"] = round(last_close, 2)
        bundle["price"]["day_open"] = round(float(last["Open"]), 2)
        bundle["price"]["day_high"] = round(float(last["High"]), 2)
        bundle["price"]["day_low"] = round(float(last["Low"]), 2)
        bundle["price"]["prev_close"] = round(prev_close, 2)
        bundle["price"]["day_change_pct"] = round(day_change_pct, 2) if day_change_pct is not None else None
        bundle["price"]["volume"] = int(last["Volume"]) if last["Volume"] == last["Volume"] else None

        # RVOL is meant to be "today vs a recent baseline" — keep that baseline at the
        # last ~20 trading days (~1 month) even though we now pull a full year of
        # history for other indicators, so this doesn't quietly turn into "vs the
        # whole year" and throw off the BUY confirmation check that depends on it.
        prior = hist.iloc[:-1].tail(20)
        if not prior.empty and prior["Volume"].mean():
            avg_prior_vol = float(prior["Volume"].mean())
            if avg_prior_vol > 0 and bundle["price"]["volume"] is not None:
                bundle["technicals"]["rvol"] = round(bundle["price"]["volume"] / avg_prior_vol, 2)

        # 20-day SMA replaces the old ad-hoc "up to 10 days" window now that we have
        # real history for a proper, standard short-term reference.
        if len(close) >= 20:
            sma_20 = float(close.tail(20).mean())
            bundle["technicals"]["sma_20"] = round(sma_20, 2)
            if sma_20:
                bundle["technicals"]["price_vs_sma_pct"] = round((last_close - sma_20) / sma_20 * 100, 2)

        if len(close) >= 50:
            bundle["technicals"]["sma_50"] = round(float(close.tail(50).mean()), 2)

        if len(close) >= 200:
            bundle["technicals"]["sma_200"] = round(float(close.tail(200).mean()), 2)

        sma_50, sma_200 = bundle["technicals"]["sma_50"], bundle["technicals"]["sma_200"]
        if sma_50 is not None and sma_200 is not None:
            bundle["technicals"]["ma_alignment"] = "bullish" if sma_50 > sma_200 else "bearish"

        rsi = _compute_rsi(close)
        if rsi is not None:
            bundle["technicals"]["rsi_14"] = round(rsi, 2)
            if rsi >= 70:
                bundle["technicals"]["rsi_signal"] = "overbought"
            elif rsi <= 30:
                bundle["technicals"]["rsi_signal"] = "oversold"
            else:
                bundle["technicals"]["rsi_signal"] = "neutral"

        macd_line, macd_signal, macd_hist = _compute_macd(close)
        if macd_line is not None:
            bundle["technicals"]["macd"] = round(macd_line, 2)
            bundle["technicals"]["macd_signal"] = round(macd_signal, 2)
            bundle["technicals"]["macd_histogram"] = round(macd_hist, 2)
            bundle["technicals"]["macd_trend"] = "bullish" if macd_line > macd_signal else "bearish"

        bb_upper, bb_lower = _compute_bollinger(close)
        if bb_upper is not None:
            bundle["technicals"]["bb_upper"] = round(bb_upper, 2)
            bundle["technicals"]["bb_lower"] = round(bb_lower, 2)
            if bb_upper != bb_lower:
                bundle["technicals"]["bb_position_pct"] = round(
                    max(0, min(100, (last_close - bb_lower) / (bb_upper - bb_lower) * 100)), 1
                )

        # window_return_pct keeps its original meaning too: trailing ~1 month, not
        # the full year now pulled for the indicators above.
        month_window = close.tail(21)
        first_of_month = float(month_window.iloc[0]) if len(month_window) >= 2 else None
        if first_of_month:
            bundle["technicals"]["window_return_pct"] = round(
                (last_close - first_of_month) / first_of_month * 100, 2
            )

        bundle["technicals"]["swing_high"] = round(float(hist["High"].max()), 2)
        bundle["technicals"]["swing_low"] = round(float(hist["Low"].min()), 2)

        day_high, day_low = bundle["price"]["day_high"], bundle["price"]["day_low"]
        if day_high is not None and day_low is not None and day_high != day_low:
            bundle["technicals"]["day_range_position_pct"] = round(
                (last_close - day_low) / (day_high - day_low) * 100, 1
            )

        vs_sma = bundle["technicals"]["price_vs_sma_pct"]
        if vs_sma is not None:
            if vs_sma > 2:
                bundle["technicals"]["trend"] = "up"
            elif vs_sma < -2:
                bundle["technicals"]["trend"] = "down"
            else:
                bundle["technicals"]["trend"] = "sideways"

    info = {}
    try:
        info = ticker.get_info() if hasattr(ticker, "get_info") else ticker.info
    except Exception:
        info = {}

    if info:
        bundle["name"] = info.get("shortName") or info.get("longName") or symbol
        bundle["sector"] = info.get("sector")

        high52 = info.get("fiftyTwoWeekHigh")
        low52 = info.get("fiftyTwoWeekLow")
        live_price = bundle["price"]["live"] or info.get("currentPrice") or info.get("regularMarketPrice")
        if high52 and low52 and live_price:
            bundle["range_52w"]["high"] = round(float(high52), 2)
            bundle["range_52w"]["low"] = round(float(low52), 2)
            bundle["range_52w"]["pct_from_high"] = round((live_price - high52) / high52 * 100, 2)
            if high52 != low52:
                bundle["range_52w"]["position_pct"] = round(
                    max(0, min(100, (live_price - low52) / (high52 - low52) * 100)), 1
                )
        if bundle["price"]["live"] is None and live_price:
            bundle["price"]["live"] = round(float(live_price), 2)

        target_mean = info.get("targetMeanPrice")
        target_low = info.get("targetLowPrice")
        target_high = info.get("targetHighPrice")
        bundle["analyst"]["target_mean"] = round(float(target_mean), 2) if target_mean else None
        bundle["analyst"]["target_low"] = round(float(target_low), 2) if target_low else None
        bundle["analyst"]["target_high"] = round(float(target_high), 2) if target_high else None
        bundle["analyst"]["consensus"] = info.get("recommendationKey")
        bundle["analyst"]["num_analysts"] = info.get("numberOfAnalystOpinions")
        if target_mean and live_price:
            bundle["analyst"]["upside_pct"] = round((target_mean - live_price) / live_price * 100, 2)

        # Deeper fundamentals from yfinance's info dict. Unit conventions below were verified
        # empirically against a live call, not assumed — yfinance/Yahoo mix decimal fractions
        # and already-scaled percentages depending on the field, which is a well-known source
        # of silent bugs if you guess instead of checking:
        #   - profitMargins/operatingMargins/grossMargins/revenueGrowth/earningsGrowth/
        #     returnOnEquity/heldPercentInsiders/heldPercentInstitutions -> decimal fraction, x100
        #   - debtToEquity/dividendYield -> already percent-scale on this yfinance version, no x100
        #   - trailingPE/forwardPE/priceToBook/currentRatio/beta/trailingEps/bookValue/marketCap
        #     -> raw values, no conversion
        fnd = bundle["fundamentals"]
        pe_ttm = info.get("trailingPE")
        pe_forward = info.get("forwardPE")
        price_to_book = info.get("priceToBook")
        roe = info.get("returnOnEquity")
        gross_margin = info.get("grossMargins")
        op_margin = info.get("operatingMargins")
        net_margin = info.get("profitMargins")
        rev_growth = info.get("revenueGrowth")
        earn_growth = info.get("earningsGrowth")
        debt_to_equity = info.get("debtToEquity")
        current_ratio = info.get("currentRatio")
        dividend_yield = info.get("dividendYield")
        insider_held = info.get("heldPercentInsiders")
        inst_held = info.get("heldPercentInstitutions")
        eps_ttm = info.get("trailingEps")
        book_value = info.get("bookValue")
        fcf = info.get("freeCashflow")
        market_cap = info.get("marketCap")
        beta = info.get("beta")

        fnd["pe_ttm"] = round(float(pe_ttm), 2) if pe_ttm is not None else None
        fnd["pe_forward"] = round(float(pe_forward), 2) if pe_forward is not None else None
        fnd["price_to_book"] = round(float(price_to_book), 2) if price_to_book is not None else None
        fnd["roe_pct"] = round(float(roe) * 100, 2) if roe is not None else None
        fnd["gross_margin_pct"] = round(float(gross_margin) * 100, 2) if gross_margin is not None else None
        fnd["operating_margin_pct"] = round(float(op_margin) * 100, 2) if op_margin is not None else None
        fnd["net_margin_pct"] = round(float(net_margin) * 100, 2) if net_margin is not None else None
        fnd["revenue_growth_pct"] = round(float(rev_growth) * 100, 2) if rev_growth is not None else None
        fnd["earnings_growth_pct"] = round(float(earn_growth) * 100, 2) if earn_growth is not None else None
        fnd["debt_to_equity_pct"] = round(float(debt_to_equity), 2) if debt_to_equity is not None else None
        fnd["current_ratio"] = round(float(current_ratio), 2) if current_ratio is not None else None
        fnd["dividend_yield_pct"] = round(float(dividend_yield), 2) if dividend_yield is not None else None
        fnd["promoter_insider_holding_pct"] = round(float(insider_held) * 100, 2) if insider_held is not None else None
        fnd["institutional_holding_pct"] = round(float(inst_held) * 100, 2) if inst_held is not None else None
        fnd["eps_ttm"] = round(float(eps_ttm), 2) if eps_ttm is not None else None
        fnd["book_value_per_share"] = round(float(book_value), 2) if book_value is not None else None
        fnd["free_cash_flow"] = int(fcf) if fcf is not None else None
        fnd["market_cap"] = int(market_cap) if market_cap is not None else None
        fnd["beta"] = round(float(beta), 2) if beta is not None else None

    try:
        rec = ticker.recommendations
        if rec is not None and not rec.empty:
            latest = rec.iloc[-1]
            strong_buy = int(latest.get("strongBuy", 0) or 0)
            buy = int(latest.get("buy", 0) or 0)
            hold = int(latest.get("hold", 0) or 0)
            sell = int(latest.get("sell", 0) or 0)
            strong_sell = int(latest.get("strongSell", 0) or 0)
            total = strong_buy + buy + hold + sell + strong_sell
            if total:
                bundle["analyst"]["buy_pct"] = round((strong_buy + buy) / total * 100, 1)
                bundle["analyst"]["hold_pct"] = round(hold / total * 100, 1)
                bundle["analyst"]["sell_pct"] = round((sell + strong_sell) / total * 100, 1)
    except Exception:
        pass

    income_stmt = balance_sheet = cashflow = None
    try:
        income_stmt = ticker.income_stmt
    except Exception:
        pass
    try:
        balance_sheet = ticker.balance_sheet
    except Exception:
        pass
    try:
        cashflow = ticker.cashflow
    except Exception:
        pass
    bundle["fundamentals_trend"] = compute_fundamentals_trend(income_stmt, balance_sheet, cashflow)

    try:
        raw_news = ticker.news or []
    except Exception:
        raw_news = []

    headlines = []  # list of (title, source, published), de-duplicated by title
    seen_titles = set()
    for item in raw_news[:8]:
        content = item.get("content") if isinstance(item.get("content"), dict) else None
        title = item.get("title") or (content.get("title") if content else None)
        published_raw = item.get("providerPublishTime") or (content.get("pubDate") if content else None)
        if title and title not in seen_titles:
            headlines.append((title, "Yahoo Finance", _parse_yf_date(published_raw)))
            seen_titles.add(title)

    if rss_items:
        for match in match_rss_headlines_for_company(rss_items, bundle.get("name") or symbol):
            if match["title"] not in seen_titles and len(headlines) < 8:
                headlines.append((match["title"], match["source"], match.get("published")))
                seen_titles.add(match["title"])

    pos = neg = neu = 0
    recent = []
    for title, source, published in headlines:
        tone = _classify_headline(title)
        if tone == "positive":
            pos += 1
        elif tone == "negative":
            neg += 1
        else:
            neu += 1
        if len(recent) < 5:
            recent.append({"title": title, "tone": tone, "source": source, "published": published})

    bundle["news"] = {"total": len(headlines), "positive": pos, "negative": neg, "neutral": neu, "recent": recent}

    bundle["data_gaps"] = _collect_data_gaps(bundle)
    return bundle


def build_universe_evidence(mode, universe_set=None, progress_cb=None):
    """Returns evidence bundles for the whole scanned universe (demo bundles, or every
    ticker in the chosen universes/<universe_set>.json set for live). universe_set is
    ignored in demo mode. progress_cb(symbol, index, total) fires per stock."""
    if mode == "demo":
        bundles = load_demo_bundles()
        for i, b in enumerate(bundles):
            if progress_cb:
                progress_cb(b.get("symbol", "?"), i + 1, len(bundles))
        return bundles

    universe = load_universe(universe_set)
    all_symbols = [(sym, seg) for seg, syms in universe.items() for sym in syms]
    rss_items = fetch_rss_headlines()  # fetched once for the whole run, not per stock
    bundles = []
    for i, (sym, seg) in enumerate(all_symbols):
        bundle = fetch_live_bundle(sym, seg, rss_items=rss_items)
        bundles.append(bundle)
        if progress_cb:
            progress_cb(sym, i + 1, len(all_symbols))
    return bundles


def shortlist(bundles, percent):
    """Take the top `percent`% biggest gainers from each cap-segment bucket
    (large/mid/small), rounded — e.g. 40% of 10 = 4, 40% of 12 = 5. A bucket that
    has any stocks at all still gets at least 1 shortlisted, so a small bucket
    (e.g. 1-2 stocks) can't round down to zero and quietly disappear from every run."""
    groups = defaultdict(list)
    for b in bundles:
        groups[b.get("cap_segment", "unknown")].append(b)

    result = []
    for seg in sorted(groups.keys()):
        items = groups[seg]
        items_sorted = sorted(
            items,
            key=lambda x: (x["price"].get("day_change_pct") is None, -(x["price"].get("day_change_pct") or -999)),
        )
        n = round(len(items) * percent / 100)
        if items:
            n = max(n, 1)
        result.extend(items_sorted[:n])
    return result
