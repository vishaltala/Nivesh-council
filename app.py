"""Flask server + background state machine + SQLite audit + Telegram delivery.

Run: python app.py, then open http://127.0.0.1:5000 and click Start agents.
"""
import copy
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, send_from_directory

import config
import data_sources
import llm
import sector_auto

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
DB_PATH = __import__("os").path.join(BASE_DIR, "agent_dashboard.db")
BUY_LOG_DIR = os.path.join(BASE_DIR, "buy_logs")

app = Flask(__name__)

AGENTS = [
    {"id": "scout", "name": "Scout", "icon": "🔭", "role": "Screens the universe for movers",
     "stat1_label": "Scanned", "stat2_label": "Shortlisted"},
    {"id": "technician", "name": "Technician", "icon": "📈", "role": "Reads price action, RVOL & trend",
     "stat1_label": "Analyzed", "stat2_label": "Avg RVOL"},
    {"id": "fundamentalist", "name": "Fundamentalist", "icon": "📊", "role": "Weighs valuation & analyst targets",
     "stat1_label": "Covered", "stat2_label": "Avg upside"},
    {"id": "newsdesk", "name": "Newsdesk", "icon": "📰", "role": "Pulls live news & scores sentiment",
     "stat1_label": "Headlines", "stat2_label": "Net tone"},
    {"id": "bull", "name": "Bull", "icon": "🐂", "role": "Argues the case to buy",
     "stat1_label": "Cases", "stat2_label": "Avg score"},
    {"id": "bear", "name": "Bear", "icon": "🐻", "role": "Argues the case against",
     "stat1_label": "Cases", "stat2_label": "Avg score"},
    {"id": "judge", "name": "Judge", "icon": "⚖️", "role": "Weighs the debate, issues verdict + confidence",
     "stat1_label": "Verdicts", "stat2_label": "Buy"},
    {"id": "messenger", "name": "Messenger", "icon": "📨", "role": "Sends signals to Telegram",
     "stat1_label": "Sent", "stat2_label": "Engine"},
]
AGENT_IDS = [a["id"] for a in AGENTS]

STATE_LOCK = threading.Lock()


def _fresh_state():
    return {
        "run_id": None,
        "mode": None,
        "universe_set": None,
        "auto_pick": None,
        "engine": None,
        "running": False,
        "started_at": None,
        "finished_at": None,
        "kpi": {"universe": 0, "in_debate": 0, "buy_signals": 0, "top_pick": None},
        "agents": {aid: {"status": "offline", "stat1": 0, "stat2": 0} for aid in AGENT_IDS},
        "verdicts": [],
        "footer": {"stocks_count": 0, "data_timestamp": None, "engine": None, "universe_set": None},
        "notice": None,
    }


STATE = _fresh_state()


def set_agent(agent_id, status=None, stat1=None, stat2=None):
    with STATE_LOCK:
        a = STATE["agents"][agent_id]
        if status is not None:
            a["status"] = status
        if stat1 is not None:
            a["stat1"] = stat1
        if stat2 is not None:
            a["stat2"] = stat2


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT, finished_at TEXT, mode TEXT, engine TEXT,
            universe_count INTEGER, shortlist_count INTEGER, buy_count INTEGER,
            top_pick_symbol TEXT, top_pick_confidence INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER, symbol TEXT, name TEXT, cap_segment TEXT, sector TEXT,
            verdict TEXT, confidence INTEGER, winner TEXT, rationale TEXT,
            key_catalyst TEXT, price REAL, day_change_pct REAL,
            bull_score INTEGER, bear_score INTEGER, engine TEXT, created_at TEXT
        )
    """)
    for stmt in (
        "ALTER TABLE runs ADD COLUMN universe_set TEXT",
        "ALTER TABLE verdicts ADD COLUMN net_score INTEGER",
        "ALTER TABLE verdicts ADD COLUMN technical_confirmation INTEGER",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists on a pre-existing db
    conn.commit()
    conn.close()


def save_run(run):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO runs (started_at, finished_at, mode, engine, universe_count, shortlist_count, "
        "buy_count, top_pick_symbol, top_pick_confidence, universe_set) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            run["started_at"], run["finished_at"], run["mode"], run["engine"],
            run["kpi"]["universe"], run["kpi"]["in_debate"], run["kpi"]["buy_signals"],
            (run["kpi"]["top_pick"] or {}).get("symbol"), (run["kpi"]["top_pick"] or {}).get("confidence"),
            run.get("universe_set"),
        ),
    )
    run_db_id = cur.lastrowid
    for v in run["verdicts"]:
        conn.execute(
            "INSERT INTO verdicts (run_id, symbol, name, cap_segment, sector, verdict, confidence, "
            "winner, rationale, key_catalyst, price, day_change_pct, bull_score, bear_score, engine, "
            "created_at, net_score, technical_confirmation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_db_id, v["symbol"], v["name"], v["cap_segment"], v["sector"], v["verdict"],
                v["confidence"], v["winner"], v["rationale"], v["key_catalyst"], v["price"],
                v["day_change_pct"], v["bull_score"], v["bear_score"], v["engine"], v["created_at"],
                v.get("net"), int(v["technical_confirmation"]) if v.get("technical_confirmation") is not None else None,
            ),
        )
    conn.commit()
    conn.close()


def now_ist_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def send_telegram(text):
    """Never log the token or the request URL — only the outcome."""
    token, chat_id = config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False, "not configured"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        ok = resp.status_code == 200 and resp.json().get("ok", False)
        return ok, None if ok else "telegram api rejected the message"
    except requests.RequestException:
        return False, "network error contacting telegram"


def build_buy_message(v):
    cap = (v["cap_segment"] or "unknown").capitalize()
    price = f"₹{v['price']:.2f}" if v["price"] is not None else "n/a"
    change = f"{v['day_change_pct']:+.2f}%" if v["day_change_pct"] is not None else "n/a"

    lines = [
        f"<b>BUY SIGNAL — {v['symbol']} ({cap} cap)</b>\n",
    ]
    if v.get("universe_set_label"):
        lines.append(f"Sector: {v['universe_set_label']}\n")
    lines += [
        f"Verdict: BUY | Confidence: {v['confidence']}/10",
        f"Winner: {v['winner']}\n",
        f"Why: {v['rationale']}\n",
        f"Key catalyst: {v['key_catalyst']}\n",
        f"Live price: {price} | Day change: {change}",
    ]

    entry, sl, tp = v.get("entry"), v.get("stop_loss"), v.get("take_profit")
    if entry is not None and sl is not None and tp is not None:
        sl_pct = v.get("sl_pct")
        rr_pct = v.get("risk_reward_pct")
        rr_ratio = v.get("rr_ratio")
        lines.append(f"Entry: ₹{entry:.2f}")
        lines.append(f"Stop-loss: ₹{sl:.2f} (-{sl_pct:.2f}%)" if sl_pct is not None else f"Stop-loss: ₹{sl:.2f}")
        tp_line = f"Take-profit: ₹{tp:.2f}"
        if rr_pct is not None:
            tp_line += f" (+{rr_pct:.2f}%)"
        if rr_ratio is not None:
            tp_line += f" [RR 1:{rr_ratio:g}]"
        lines.append(tp_line)
        if v.get("analyst_target_note"):
            lines.append(v["analyst_target_note"])

    #lines.append("— Analysis only. No trade was placed. Not investment advice.")
    return "\n".join(lines)


def _safe_filename(symbol):
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", symbol or "").strip("_")
    return cleaned or "UNKNOWN"


def _fmt_money(val):
    return f"₹{val:,.2f}" if val is not None else "n/a"


def _fmt_signed_pct(val, decimals=2):
    return f"{val:+.{decimals}f}%" if val is not None else "n/a"


def _fmt_pct(val, decimals=2):
    return f"{val:.{decimals}f}%" if val is not None else "n/a"


def _fmt_ratio(val, suffix="x"):
    return f"{val:.2f}{suffix}" if val is not None else "n/a"


def build_buy_log(evidence, result, v, created_at):
    """Full audit dump for one fired BUY signal: technicals, fundamentals, news
    headlines, every seat's reasoning (Bull/Bear/Technician/Fundamentalist/
    Newsdesk), and the Judge's verdict. Every figure here is either read straight
    from the evidence bundle or is the stated entry/SL/TP formula — same grounding
    rule as everywhere else in this app."""
    price = evidence.get("price") or {}
    range_52w = evidence.get("range_52w") or {}
    tech = evidence.get("technicals") or {}
    analyst = evidence.get("analyst") or {}
    fnd = evidence.get("fundamentals") or {}
    fnd_trend = evidence.get("fundamentals_trend") or {}
    news = evidence.get("news") or {}
    scores = result.get("scores") or {}

    lines = []
    lines.append("=" * 70)
    lines.append(f"BUY SIGNAL — {evidence.get('symbol')} ({evidence.get('name') or evidence.get('symbol')})")
    lines.append(f"Generated: {created_at} | Engine: {result.get('engine')} | Verified: {result.get('verified')}")
    lines.append("=" * 70)

    lines.append("")
    lines.append("VERDICT")
    lines.append(f"  Verdict: {v.get('verdict')} | Confidence: {v.get('confidence')}/10 | Winner: {v.get('winner')}")
    bull_s, bear_s, net = v.get("bull_score"), v.get("bear_score"), v.get("net")
    if net is not None:
        lines.append(f"  Net score: {net:+d}  (Bull {bull_s} minus Bear {bear_s})")
    confirmed = v.get("technical_confirmation")
    if confirmed is not None:
        conf_text = "Yes" if confirmed else "No"
        lines.append(f"  Technical confirmation: {conf_text}  (near 52-week high, or unusually high volume today)")
    lines.append(f"  Key catalyst: {v.get('key_catalyst')}")
    lines.append(f"  Rationale: {v.get('rationale')}")

    lines.append("")
    lines.append("TRADE LEVELS")
    if v.get("entry") is not None:
        lines.append(f"  Entry: {_fmt_money(v.get('entry'))}")
        lines.append(f"  Stop-loss: {_fmt_money(v.get('stop_loss'))} (-{_fmt_pct(v.get('sl_pct'))})")
        rr = f" [RR 1:{v['rr_ratio']:g}]" if v.get("rr_ratio") is not None else ""
        lines.append(f"  Take-profit: {_fmt_money(v.get('take_profit'))} (+{_fmt_pct(v.get('risk_reward_pct'))}){rr}")
        if v.get("analyst_target_note"):
            lines.append(f"  {v['analyst_target_note']}")
    else:
        lines.append("  n/a — no live price in evidence")

    lines.append("")
    lines.append("PRICE")
    lines.append(f"  Live: {_fmt_money(price.get('live'))} | Day change: {_fmt_signed_pct(price.get('day_change_pct'))}")
    lines.append(
        f"  Day range: {_fmt_money(price.get('day_low'))} - {_fmt_money(price.get('day_high'))} "
        f"| Prev close: {_fmt_money(price.get('prev_close'))}"
    )
    vol = price.get("volume")
    lines.append(f"  Volume: {vol:,}" if vol is not None else "  Volume: n/a")

    lines.append("")
    lines.append("52-WEEK RANGE")
    lines.append(
        f"  High: {_fmt_money(range_52w.get('high'))} | Low: {_fmt_money(range_52w.get('low'))} "
        f"| Position: {_fmt_pct(range_52w.get('position_pct'))} | From high: {_fmt_signed_pct(range_52w.get('pct_from_high'))}"
    )

    lines.append("")
    lines.append("TECHNICALS")
    rvol = tech.get("rvol")
    rvol_str = f"{rvol:.2f}x" if rvol is not None else "n/a"
    lines.append(f"  Trend: {tech.get('trend') or 'n/a'} | RVOL: {rvol_str} | vs SMA(20): {_fmt_signed_pct(tech.get('price_vs_sma_pct'))}")
    lines.append(
        f"  Day range position: {_fmt_pct(tech.get('day_range_position_pct'))} "
        f"| Window return (1mo): {_fmt_signed_pct(tech.get('window_return_pct'))}"
    )
    lines.append(
        f"  SMA 20/50/200: {_fmt_money(tech.get('sma_20'))} / {_fmt_money(tech.get('sma_50'))} / {_fmt_money(tech.get('sma_200'))} "
        f"| 50 vs 200: {tech.get('ma_alignment') or 'n/a'}"
    )
    rsi = tech.get("rsi_14")
    rsi_str = f"{rsi:.0f} ({tech.get('rsi_signal') or 'n/a'})" if rsi is not None else "n/a"
    lines.append(
        f"  RSI(14): {rsi_str} | MACD: {tech.get('macd_trend') or 'n/a'} "
        f"(line {_fmt_ratio(tech.get('macd'), suffix='')}, signal {_fmt_ratio(tech.get('macd_signal'), suffix='')})"
    )
    lines.append(
        f"  Bollinger Bands (20,2): {_fmt_money(tech.get('bb_lower'))} - {_fmt_money(tech.get('bb_upper'))} "
        f"| Position: {_fmt_pct(tech.get('bb_position_pct'))}"
    )
    lines.append(f"  52-week swing high/low: {_fmt_money(tech.get('swing_high'))} / {_fmt_money(tech.get('swing_low'))}")

    lines.append("")
    lines.append("FUNDAMENTALS / ANALYST")
    num_analysts = analyst.get("num_analysts")
    lines.append(f"  Consensus: {analyst.get('consensus') or 'n/a'} | Analysts covering: {num_analysts if num_analysts is not None else 'n/a'}")
    lines.append(
        f"  Buy/Hold/Sell: {_fmt_pct(analyst.get('buy_pct'))} / "
        f"{_fmt_pct(analyst.get('hold_pct'))} / {_fmt_pct(analyst.get('sell_pct'))}"
    )
    lines.append(
        f"  Target mean/low/high: {_fmt_money(analyst.get('target_mean'))} / "
        f"{_fmt_money(analyst.get('target_low'))} / {_fmt_money(analyst.get('target_high'))}"
    )
    lines.append(f"  Upside to target: {_fmt_signed_pct(analyst.get('upside_pct'))}")

    lines.append("")
    lines.append("FUNDAMENTAL RATIOS (yfinance)")
    lines.append(
        f"  P/E (TTM / Forward): {_fmt_ratio(fnd.get('pe_ttm'))} / {_fmt_ratio(fnd.get('pe_forward'))} "
        f"| P/B: {_fmt_ratio(fnd.get('price_to_book'))}"
    )
    lines.append(
        f"  ROE: {_fmt_signed_pct(fnd.get('roe_pct'))} | Gross margin: {_fmt_signed_pct(fnd.get('gross_margin_pct'))} "
        f"| Operating margin: {_fmt_signed_pct(fnd.get('operating_margin_pct'))} | Net margin: {_fmt_signed_pct(fnd.get('net_margin_pct'))}"
    )
    lines.append(
        f"  Revenue growth YoY: {_fmt_signed_pct(fnd.get('revenue_growth_pct'))} "
        f"| Earnings growth YoY: {_fmt_signed_pct(fnd.get('earnings_growth_pct'))}"
    )
    lines.append(
        f"  Debt/Equity: {_fmt_pct(fnd.get('debt_to_equity_pct'))} | Current ratio: {_fmt_ratio(fnd.get('current_ratio'), suffix='')} "
        f"| Dividend yield: {_fmt_pct(fnd.get('dividend_yield_pct'))}"
    )
    lines.append(
        f"  Promoter/insider holding: {_fmt_pct(fnd.get('promoter_insider_holding_pct'))} "
        f"| Institutional holding: {_fmt_pct(fnd.get('institutional_holding_pct'))}"
    )
    lines.append(
        f"  EPS (TTM): {_fmt_money(fnd.get('eps_ttm'))} | Book value/share: {_fmt_money(fnd.get('book_value_per_share'))}"
    )
    lines.append(
        f"  Free cash flow: {_fmt_money(fnd.get('free_cash_flow'))} | Market cap: {_fmt_money(fnd.get('market_cap'))} "
        f"| Beta: {_fmt_ratio(fnd.get('beta'), suffix='')}"
    )

    lines.append("")
    years = fnd_trend.get("years_available") or 0
    lines.append(f"MULTI-YEAR TREND & QUALITY ({years} year(s) of filings)")
    if years >= 2:
        lines.append(
            f"  Net margin trend: {fnd_trend.get('net_margin_trend') or 'n/a'} "
            f"| {fnd_trend.get('revenue_growth_streak') or 'n/a'}"
        )
        cfo_ratio = fnd_trend.get("cfo_to_net_income_ratio")
        cfo_str = f"{cfo_ratio:.2f}x" if cfo_ratio is not None else "n/a"
        lines.append(
            f"  Earnings quality: {fnd_trend.get('earnings_quality') or 'n/a'} "
            f"(operating cash flow is {cfo_str} reported net income)"
        )
        piotroski = fnd_trend.get("piotroski_f_score")
        piotroski_max = fnd_trend.get("piotroski_max")
        if piotroski is not None:
            lines.append(f"  Piotroski F-Score: {piotroski}/{piotroski_max}")
            for line in fnd_trend.get("piotroski_breakdown") or []:
                lines.append(f"    - {line}")
    else:
        lines.append("  n/a — not enough years of filings available")

    lines.append("")
    total = news.get("total", 0)
    lines.append(
        f"NEWS HEADLINES — simple word-count tags ({news.get('positive', 0)} positive / "
        f"{news.get('negative', 0)} negative / {news.get('neutral', 0)} neutral of {total} total)"
    )
    lines.append("  (see NEWSDESK below for the AI's own read of these same headlines)")
    recent = news.get("recent") or []
    if recent:
        for item in recent:
            published = item.get("published") or "date n/a"
            source = item.get("source") or "n/a"
            lines.append(f"  [{item.get('tone')}] {published} ({source}) {item.get('title')}")
    else:
        lines.append("  no headlines found")

    lines.append("")
    for key, label in (
        ("bull", "BULL CASE"), ("bear", "BEAR CASE"), ("technician", "TECHNICIAN"),
        ("fundamentalist", "FUNDAMENTALIST"), ("newsdesk", "NEWSDESK"),
    ):
        seat = scores.get(key) or {}
        lines.append(f"{label} — score {seat.get('score', 'n/a')}/100")
        lines.append(f"  {seat.get('reasons') or 'n/a'}")
        lines.append("")

    lines.append("GROUNDING")
    lines.append(f"  Verified: {result.get('verified')}")
    claims = result.get("unverified_claims") or []
    lines.append(f"  Unverified claims: {', '.join(claims) if claims else 'none'}")

    if evidence.get("data_gaps"):
        lines.append("")
        lines.append(f"DATA GAPS: {', '.join(evidence['data_gaps'])}")

    lines.append("")
    lines.append("=" * 70)
    lines.append("Analysis only. No trade was placed. Not investment advice.")

    return "\n".join(lines)


def write_buy_log(evidence, result, v, created_at):
    """Write the full BUY audit dump to buy_logs/<SYMBOL>_<timestamp>.txt.
    Never raises — a logging failure must not break the analysis pipeline."""
    try:
        os.makedirs(BUY_LOG_DIR, exist_ok=True)
        ts = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
        symbol = _safe_filename(evidence.get("symbol", "UNKNOWN"))
        filename = f"{symbol}_{ts}.txt"
        path = os.path.join(BUY_LOG_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_buy_log(evidence, result, v, created_at))
        return filename
    except Exception:
        return None


def cleanup_old_buy_logs():
    """Delete buy_logs/*.txt older than BUY_LOG_RETENTION_DAYS. Never raises —
    a cleanup failure must not break the analysis pipeline or startup."""
    try:
        if not os.path.isdir(BUY_LOG_DIR):
            return
        cutoff = time.time() - config.BUY_LOG_RETENTION_DAYS * 86400
        for name in os.listdir(BUY_LOG_DIR):
            if not name.endswith(".txt"):
                continue
            path = os.path.join(BUY_LOG_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                continue
    except Exception:
        pass


def build_summary_message(mode, engine, fired, universe_set_label):
    ts = now_ist_str()
    
    if universe_set_label:
        lines = [f"<b>{config.BRAND} — daily summary</b>", f"Mode: {mode} | Engine: {engine} | {ts} | Sector: {universe_set_label.upper()}"]
    else:
        lines = [f"<b>{config.BRAND} — daily summary</b>", f"Mode: {mode} | Engine: {engine} | {ts}"]
    
    if not fired:
        lines.append("\nNo BUY signals fired this run.")
    else:
        lines.append(f"\n{len(fired)} BUY signal(s) fired:")
        for v in fired:
            lines.append(f"• {v['symbol']} — {v['confidence']}/10 confidence")
    
    return "\n".join(lines)


def run_cycle(mode, universe_set=None):
    cleanup_old_buy_logs()

    with STATE_LOCK:
        global STATE
        STATE = _fresh_state()
        STATE["running"] = True
        STATE["mode"] = mode
        STATE["universe_set"] = universe_set if mode in ("live", "auto") else None
        STATE["started_at"] = now_ist_str()

    try:
        # --- AUTO MODE: pick today's biggest-moving NSE sector automatically ---
        if mode == "auto":
            available_ids = [s["id"] for s in data_sources.list_universe_sets()]
            auto_result = sector_auto.pick_auto_universe_set(available_ids)
            if auto_result is None:
                raise RuntimeError(
                    "Auto mode couldn't reach NSE to find today's top-moving sector "
                    "— try Live mode with a manual sector instead."
                )
            if auto_result["chosen"] is None:
                raise RuntimeError(
                    "Auto mode reached NSE, but none of today's sectoral movers match "
                    "a universes/*.json file you have — try Live mode with a manual sector."
                )
            universe_set = auto_result["chosen"]["universe_set"]
            with STATE_LOCK:
                STATE["universe_set"] = universe_set
                STATE["auto_pick"] = auto_result["chosen"]

        # --- SCOUT: scan the universe, build shortlist ---
        set_agent("scout", status="working")

        def scout_progress(symbol, i, total):
            set_agent("scout", stat1=i)
            time.sleep(min(config.AGENT_DELAY, 0.15))

        bundles = data_sources.build_universe_evidence(mode, universe_set=universe_set, progress_cb=scout_progress)
        shortlisted = data_sources.shortlist(bundles, config.SHORTLIST_PERCENT)

        with STATE_LOCK:
            STATE["kpi"]["universe"] = len(bundles)
            STATE["kpi"]["in_debate"] = len(shortlisted)
        set_agent("scout", status="done", stat1=len(bundles), stat2=len(shortlisted))

        # --- TECHNICIAN: pass over shortlist, average RVOL ---
        set_agent("technician", status="working")
        rvol_sum, rvol_n = 0.0, 0
        for i, b in enumerate(shortlisted):
            rvol = (b.get("technicals") or {}).get("rvol")
            if rvol is not None:
                rvol_sum += rvol
                rvol_n += 1
            avg_rvol = round(rvol_sum / rvol_n, 2) if rvol_n else 0
            set_agent("technician", stat1=i + 1, stat2=avg_rvol)
            time.sleep(config.AGENT_DELAY)
        set_agent("technician", status="done")

        # --- FUNDAMENTALIST: pass over shortlist, average upside ---
        set_agent("fundamentalist", status="working")
        up_sum, up_n = 0.0, 0
        for i, b in enumerate(shortlisted):
            upside = (b.get("analyst") or {}).get("upside_pct")
            if upside is not None:
                up_sum += upside
                up_n += 1
            avg_up = round(up_sum / up_n, 1) if up_n else 0
            set_agent("fundamentalist", stat1=i + 1, stat2=avg_up)
            time.sleep(config.AGENT_DELAY)
        set_agent("fundamentalist", status="done")

        # --- NEWSDESK: pass over shortlist, tally headlines & tone ---
        set_agent("newsdesk", status="working")
        headline_total, tone_sum, tone_n = 0, 0.0, 0
        for i, b in enumerate(shortlisted):
            news = b.get("news") or {}
            headline_total += news.get("total", 0)
            if news.get("total"):
                tone_sum += (news.get("positive", 0) - news.get("negative", 0)) / news["total"] * 100
                tone_n += 1
            avg_tone = round(tone_sum / tone_n, 0) if tone_n else 0
            set_agent("newsdesk", stat1=headline_total, stat2=avg_tone)
            time.sleep(config.AGENT_DELAY)
        set_agent("newsdesk", status="done")

        # --- DEBATE: Bull, Bear, Judge — one combined evaluate() call per stock ---
        set_agent("bull", status="working")
        set_agent("bear", status="working")
        set_agent("judge", status="working")

        bull_sum = bear_sum = 0.0
        buy_count = 0
        top_pick = None
        engines_used = []
        created_at = now_ist_str()

        universe_set_label = None
        if mode in ("live", "auto") and universe_set:
            universe_set_label = next(
                (s["label"] for s in data_sources.list_universe_sets() if s["id"] == universe_set),
                universe_set,
            )

        for i, evidence in enumerate(shortlisted):
            result = llm.evaluate(evidence)
            engines_used.append(result["engine"])
            scores = result["scores"]
            v = result["verdict"]

            bull_sum += scores["bull"]["score"]
            bear_sum += scores["bear"]["score"]
            log_file = None
            if v["verdict"] == "BUY" and v["confidence"] >= config.CONFIDENCE_THRESHOLD:
                buy_count += 1
                log_file = write_buy_log(evidence, result, v, created_at)

            row = {
                "symbol": evidence["symbol"],
                "name": evidence.get("name") or evidence["symbol"],
                "cap_segment": evidence.get("cap_segment"),
                "sector": evidence.get("sector"),
                "verdict": v["verdict"],
                "confidence": v["confidence"],
                "winner": v["winner"],
                "rationale": v["rationale"],
                "key_catalyst": v["key_catalyst"],
                "why": v["rationale"],
                "price": (evidence.get("price") or {}).get("live"),
                "day_change_pct": (evidence.get("price") or {}).get("day_change_pct"),
                "entry": v.get("entry"),
                "stop_loss": v.get("stop_loss"),
                "take_profit": v.get("take_profit"),
                "sl_pct": v.get("sl_pct"),
                "risk_reward_pct": v.get("risk_reward_pct"),
                "rr_ratio": v.get("rr_ratio"),
                "analyst_target_note": v.get("analyst_target_note"),
                "bull_score": scores["bull"]["score"],
                "bear_score": scores["bear"]["score"],
                "net": v.get("net"),
                "technical_confirmation": v.get("technical_confirmation"),
                "engine": result["engine"],
                "verified": result.get("verified", True),
                "created_at": created_at,
                "log_file": log_file,
                "universe_set_label": universe_set_label,
            }
            with STATE_LOCK:
                STATE["verdicts"].insert(0, row)
                STATE["kpi"]["buy_signals"] = buy_count
                if v["verdict"] == "BUY" and (top_pick is None or v["confidence"] > top_pick["confidence"]):
                    top_pick = {"symbol": evidence["symbol"], "confidence": v["confidence"]}
                STATE["kpi"]["top_pick"] = top_pick

            n = i + 1
            set_agent("bull", stat1=n, stat2=round(bull_sum / n))
            set_agent("bear", stat1=n, stat2=round(bear_sum / n))
            set_agent("judge", stat1=n, stat2=buy_count)
            time.sleep(config.AGENT_DELAY)

        set_agent("bull", status="done")
        set_agent("bear", status="done")
        set_agent("judge", status="done")

        run_engine = max(set(engines_used), key=engines_used.count) if engines_used else "deterministic"
        with STATE_LOCK:
            STATE["engine"] = run_engine

        # --- MESSENGER: send Telegram BUY alerts + daily summary ---
        set_agent("messenger", status="working")
        with STATE_LOCK:
            fired = [v for v in STATE["verdicts"] if v["verdict"] == "BUY" and v["confidence"] >= config.CONFIDENCE_THRESHOLD]

        sent = 0
        telegram_notice = None
        for v in fired:
            ok, err = send_telegram(build_buy_message(v))
            if ok:
                sent += 1
            elif telegram_notice is None:
                telegram_notice = err
            set_agent("messenger", stat1=sent, stat2=run_engine)
            time.sleep(config.AGENT_DELAY)

        ok, err = send_telegram(build_summary_message(mode, run_engine, fired, universe_set))
        if not ok and telegram_notice is None:
            telegram_notice = err

        set_agent("messenger", status="done", stat1=sent, stat2=run_engine)

        with STATE_LOCK:
            STATE["finished_at"] = now_ist_str()
            STATE["running"] = False
            STATE["footer"] = {
                "stocks_count": STATE["kpi"]["universe"],
                "data_timestamp": STATE["finished_at"],
                "engine": run_engine,
                "universe_set": STATE["universe_set"],
                "auto_pick": STATE["auto_pick"],
            }
            if telegram_notice:
                STATE["notice"] = f"Telegram: {telegram_notice}"
            snapshot = copy.deepcopy(STATE)

        save_run(snapshot)

    except Exception as exc:  # the pipeline must never crash the server
        with STATE_LOCK:
            STATE["running"] = False
            STATE["finished_at"] = now_ist_str()
            STATE["notice"] = f"Run failed: {exc}"
            for aid in AGENT_IDS:
                if STATE["agents"][aid]["status"] == "working":
                    STATE["agents"][aid]["status"] = "offline"


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "dashboard.html")


@app.route("/config")
def get_config():
    return jsonify({
        "brand": config.BRAND,
        "agents": AGENTS,
        "confidence_threshold": config.CONFIDENCE_THRESHOLD,
        "shortlist_percent": config.SHORTLIST_PERCENT,
        "modes": ["demo", "live", "auto"],
        "universe_sets": data_sources.list_universe_sets(),
    })


@app.route("/status")
def status():
    with STATE_LOCK:
        return jsonify(copy.deepcopy(STATE))


@app.route("/start", methods=["POST"])
def start():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "demo")
    if mode not in ("demo", "live", "auto"):
        mode = "demo"

    universe_set = None
    if mode == "live":
        # Auto mode picks its own set once the run starts (sector_auto.py) — no
        # need to default one here, it'd just get overwritten immediately.
        universe_set = body.get("universe_set")
        available_ids = [s["id"] for s in data_sources.list_universe_sets()]
        if universe_set not in available_ids:
            universe_set = available_ids[0] if available_ids else None

    with STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "a run is already in progress"}), 409
    threading.Thread(target=run_cycle, args=(mode, universe_set), daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    cleanup_old_buy_logs()
    app.run(host="127.0.0.1", port=config.PORT, debug=False, threaded=True)