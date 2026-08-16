"""LLM debate engine with provider auto-detection, and a numeric grounding verifier.

Priority: claude_code (local CLI, uses your subscription) -> anthropic (API key) ->
openai (API key) -> deterministic (scoring.py, always works). LLM_PROVIDER in .env
forces one provider ("deterministic" forces the rule-based engine).
"""
import json
import re
import shutil
import subprocess

import requests

import config
import scoring

SYSTEM_PROMPT = """You are a six-seat equity research panel analyzing one Indian (NSE) stock \
for a personal dashboard: Bull, Bear, Fundamentalist, Technician, Newsdesk, and a Judge.

Rules:
- Use ONLY the numbers present in the evidence bundle JSON given to you. Never invent numbers.
- If a value you would need is missing or null, say "data unavailable" for that point instead of guessing.
- A BUY verdict needs genuinely favorable risk/reward WITH confirmation (momentum or volume).
  WATCH = promising but unconfirmed. AVOID = poor risk/reward.
- Each seat's "point" must be 25 words or fewer.
- The Judge's "rationale" must be 2 lines or fewer.
- entry/stop_loss/take_profit/risk_reward_pct are given to you below the evidence bundle as
  FIXED TRADE LEVELS, already computed from the evidence with a fixed formula. Copy those
  values into judge.entry / judge.stop_loss / judge.take_profit / judge.risk_reward_pct
  EXACTLY as given — never recompute, round differently, or invent your own. If FIXED TRADE
  LEVELS says unavailable, set all four of those judge fields to null.
- For the Newsdesk seat: evidence.news.recent lists each headline's title next to a "tone"
  tag. That tag was set by simple word-matching (just checking for words like "profit" or
  "loss" in the title), which is often wrong for headlines with mixed or subtle meaning —
  e.g. "Profit rises but company warns of headwinds" is not simply positive. Read each
  headline yourself and judge what it actually means for the stock. Do not just repeat the
  pre-set tags or counts if your own reading of the headline disagrees with them.
- Interpret the deeper technical/fundamental readings (Piotroski F-Score, RSI, P/E, MA
  alignment, MACD, margin trend, earnings quality) using their real-world bands — do not
  treat "anything less than perfect" as a red flag. Piotroski F-Score 8-9 is strong, 6-7
  is decent, 3-5 is average/mixed (neutral, not necessarily bad), 0-2 is genuinely weak.
  RSI is only a real caution signal when truly extreme (>=80 very overbought, <=20 very
  oversold) — RSI in the 30-75 range is unremarkable. A high P/E alone, without the
  sector/peer comparison this app doesn't have, is weak evidence of overvaluation by
  itself. Don't let one middling secondary reading override an otherwise strong core case
  (growth, momentum, analyst support) — only let genuinely extreme readings, or several
  weak signals together, meaningfully pull the verdict toward caution.

Return STRICT JSON only, no prose outside the JSON, matching exactly this schema:
{
  "bull": {"score": <0-100 int>, "point": "<=25 words"},
  "bear": {"score": <0-100 int>, "point": "<=25 words"},
  "fundamentalist": {"score": <0-100 int>, "point": "<=25 words"},
  "technician": {"score": <0-100 int>, "point": "<=25 words"},
  "newsdesk": {"score": <0-100 int>, "point": "<=25 words"},
  "judge": {
    "winner": "Bull" or "Bear",
    "verdict": "BUY" or "WATCH" or "AVOID",
    "confidence": <1-10 int>,
    "rationale": "<=2 lines",
    "key_catalyst": "short phrase",
    "entry": <number or null — echo FIXED TRADE LEVELS.entry exactly>,
    "stop_loss": <number or null — echo FIXED TRADE LEVELS.stop_loss exactly>,
    "take_profit": <number or null — echo FIXED TRADE LEVELS.take_profit exactly>,
    "risk_reward_pct": <number or null — echo FIXED TRADE LEVELS.risk_reward_pct exactly>
  }
}"""

AGENT_KEYS = ["bull", "bear", "fundamentalist", "technician", "newsdesk"]

NUM_RE = re.compile(r"-?\d+\.?\d*")


def _build_prompt(evidence, trade_levels):
    prompt = SYSTEM_PROMPT + "\n\nEVIDENCE BUNDLE:\n" + json.dumps(evidence, default=str)
    if trade_levels:
        prompt += (
            "\n\nFIXED TRADE LEVELS (pre-computed from evidence.price.live via a fixed formula — "
            "echo these exact values into judge.entry / judge.stop_loss / judge.take_profit / "
            "judge.risk_reward_pct, do not recompute or alter them):\n"
            f"entry: {trade_levels['entry']}\n"
            f"stop_loss: {trade_levels['stop_loss']}\n"
            f"take_profit: {trade_levels['take_profit']}\n"
            f"risk_reward_pct: {trade_levels['risk_reward_pct']}\n"
        )
    else:
        prompt += (
            "\n\nFIXED TRADE LEVELS: unavailable (no live price in evidence.price.live). "
            "Set judge.entry, judge.stop_loss, judge.take_profit, judge.risk_reward_pct to null.\n"
        )
    return prompt


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in LLM output")
    return json.loads(text[start:end + 1])


def _validate_llm_result(result):
    if not isinstance(result, dict):
        return False
    for key in AGENT_KEYS:
        seat = result.get(key)
        if not isinstance(seat, dict) or "score" not in seat:
            return False
        try:
            s = float(seat["score"])
        except (TypeError, ValueError):
            return False
        if not (0 <= s <= 100):
            return False
    judge_out = result.get("judge")
    if not isinstance(judge_out, dict):
        return False
    if judge_out.get("verdict") not in ("BUY", "WATCH", "AVOID"):
        return False
    try:
        c = float(judge_out.get("confidence"))
    except (TypeError, ValueError):
        return False
    if not (1 <= c <= 10):
        return False
    return True


def _call_claude_code(prompt, model="haiku"):
    if not shutil.which("claude"):
        return None
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json", "--model", model],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        envelope = json.loads(proc.stdout)
        if envelope.get("is_error"):
            return None
        return _extract_json(envelope.get("result", ""))
    except Exception:
        return None


def _call_anthropic(prompt):
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-3-5-haiku-20241022",
                "max_tokens": 800,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        return _extract_json(text)
    except Exception:
        return None


def _call_openai(prompt):
    if not config.OPENAI_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}", "content-type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "Return strict JSON only, no prose."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        return _extract_json(text)
    except Exception:
        return None


_MAGNITUDE_SCALES = (1e3, 1e5, 1e6, 1e7, 1e9, 1e12)  # K, L(akh), M, Cr(ore), B, T


def _flatten_numbers(obj, acc):
    if isinstance(obj, dict):
        for v in obj.values():
            _flatten_numbers(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_numbers(v, acc)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        val = float(obj)
        acc.add(round(val, 1))
        acc.add(round(val))
        # Large evidence numbers (market cap, free cash flow, ...) are routinely
        # abbreviated in prose ("185B", "17.7T") — recognize the common scaled
        # forms too, so a correctly-grounded abbreviation isn't flagged as invented.
        if abs(val) >= 100_000:
            for scale in _MAGNITUDE_SCALES:
                if abs(val) >= scale:
                    scaled = val / scale
                    acc.add(round(scaled, 1))
                    acc.add(round(scaled))


def _verify_text(text, evidence_numbers):
    unverified = []
    for m in NUM_RE.findall(text or ""):
        try:
            val = float(m)
        except ValueError:
            continue
        # skip small integers 0-10 — almost always scores/confidence/counts, not evidence figures
        if val == int(val) and 0 <= val <= 10:
            continue
        # English prose routinely drops the minus sign for a negative figure when a verb
        # already implies it ("earnings fell 22%" instead of "-22%") — check both signs
        # before flagging, so natural phrasing of a real negative evidence number isn't
        # mistaken for an invented one.
        candidates = {round(val, 1), round(val), round(-val, 1), round(-val)}
        if not candidates & evidence_numbers:
            unverified.append(m)
    return unverified


def _verify_trade_levels(judge_out, trade_levels):
    """entry/stop_loss/take_profit/risk_reward_pct must be the Judge echoing the
    FIXED TRADE LEVELS verbatim, never a recomputed or invented number."""
    fields = ("entry", "stop_loss", "take_profit", "risk_reward_pct")
    mismatches = []

    if trade_levels is None:
        for f in fields:
            got = judge_out.get(f)
            if got is not None:
                mismatches.append(f"{f}={got!r} (expected null — no live price in evidence)")
        return mismatches

    for f in fields:
        expected = trade_levels[f]
        got = judge_out.get(f)
        try:
            got_val = float(got)
        except (TypeError, ValueError):
            mismatches.append(f"{f}={got!r} (expected {expected})")
            continue
        if round(got_val, 2) != round(float(expected), 2):
            mismatches.append(f"{f}={got_val} (expected {expected})")
    return mismatches


def evaluate(evidence):
    forced = config.LLM_PROVIDER
    trade_levels = scoring.compute_trade_levels(evidence)

    if forced == "deterministic":
        det = scoring.evaluate(evidence)
        det["engine"] = "deterministic"
        return det

    if forced in ("claude_code", "anthropic", "openai"):
        provider_order = [forced]
    else:
        provider_order = ["claude_code", "anthropic", "openai"]

    prompt = _build_prompt(evidence, trade_levels)
    result, engine = None, None
    for provider in provider_order:
        caller = {"claude_code": _call_claude_code, "anthropic": _call_anthropic, "openai": _call_openai}[provider]
        r = caller(prompt)
        if r and _validate_llm_result(r):
            result, engine = r, provider
            break

    if result is None:
        det = scoring.evaluate(evidence)
        det["engine"] = "deterministic"
        return det

    numbers = set()
    _flatten_numbers(evidence, numbers)
    if trade_levels:
        # formula-derived figures are legitimately grounded, not invented — recognize them
        # if an agent's prose mentions the entry/SL/TP price
        for key in ("entry", "stop_loss", "take_profit", "risk_reward_pct"):
            val = trade_levels.get(key)
            if val is not None:
                numbers.add(round(float(val), 1))
                numbers.add(round(float(val)))

    scores = {}
    all_text = []
    for agent in AGENT_KEYS:
        seat = result.get(agent, {})
        point = str(seat.get("point", ""))
        scores[agent] = {"score": int(round(float(seat.get("score", 0)))), "reasons": point}
        all_text.append(point)

    j = result.get("judge", {})
    bull_score = scores["bull"]["score"]
    bear_score = scores["bear"]["score"]
    rationale = str(j.get("rationale", ""))
    verdict = {
        "winner": j.get("winner") if j.get("winner") in ("Bull", "Bear") else ("Bull" if bull_score >= bear_score else "Bear"),
        "verdict": j.get("verdict", "WATCH"),
        "confidence": int(round(float(j.get("confidence", 5)))),
        "rationale": rationale,
        "key_catalyst": str(j.get("key_catalyst", "")),
        "bull_score": bull_score,
        "bear_score": bear_score,
        "net": bull_score - bear_score,
        # same check the deterministic engine uses to gate a BUY, computed here too so
        # every run's audit trail shows this signal regardless of which engine decided —
        # for the LLM path it's informational, the LLM forms its own judgment.
        "technical_confirmation": scoring.check_technical_confirmation(evidence),
        # never trust the LLM's echoed numbers as the source of truth — always use our
        # own formula output, even if it echoed correctly. The verifier below just checks
        # whether it echoed correctly, as a grounding signal.
        "entry": trade_levels["entry"] if trade_levels else None,
        "stop_loss": trade_levels["stop_loss"] if trade_levels else None,
        "take_profit": trade_levels["take_profit"] if trade_levels else None,
        "sl_pct": trade_levels["sl_pct"] if trade_levels else None,
        "risk_reward_pct": trade_levels["risk_reward_pct"] if trade_levels else None,
        "rr_ratio": trade_levels["rr_ratio"] if trade_levels else None,
        "analyst_target_note": trade_levels["analyst_target_note"] if trade_levels else None,
    }
    all_text.append(rationale)

    unverified = []
    for t in all_text:
        unverified.extend(_verify_text(t, numbers))
    unverified.extend(_verify_trade_levels(j, trade_levels))

    return {
        "scores": scores,
        "verdict": verdict,
        "engine": engine,
        "verified": len(unverified) == 0,
        "unverified_claims": unverified,
    }
