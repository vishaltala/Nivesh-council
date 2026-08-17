"""Auto mode: picks which universes/*.json set to scan by finding today's biggest-
moving NSE sectoral index — ported from the standalone nse_sectoral_indices.py
script (originally at ~/Desktop/Algo/Indian Stocks/) into the app itself, rather
than shelling out to a file that lives outside this project.

NSE blocks requests without proper headers/cookies, so this first visits the
homepage to collect session cookies, then calls the sectoral indices API. The
API path/field names can change over time — if this stops working, open the
heatmap page (https://www.nseindia.com/market-data/live-market-indices/heatmap)
in a browser, check DevTools -> Network -> Fetch/XHR for the request returning
index data, and update NSE_API_URL below.
"""
import time

import requests

NSE_HOME_URL = "https://www.nseindia.com"
NSE_API_URL = "https://www.nseindia.com/api/allIndices"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/live-market-indices/heatmap",
}

# The 23 sectoral indices from the NSE heatmap, matched by a set of required
# substrings (checked against the uppercased "index" field). Order matters: more
# specific rules are listed before broader ones so e.g. "NIFTY BANK" doesn't
# accidentally swallow "NIFTY PSU BANK".
SECTORAL_RULES = [
    ("NIFTY AUTO", ["AUTO"]),
    ("NIFTY PSU BANK", ["PSU", "BANK"]),
    ("NIFTY PVT BANK", ["PVT", "BANK"]),
    ("NIFTY BANK", ["BANK"]),  # after PSU/PVT so it only catches the plain one
    ("NIFTY FMCG", ["FMCG"]),
    ("NIFTY IT", ["NIFTY IT"]),
    ("NIFTY MEDIA", ["MEDIA"]),
    ("NIFTY METAL", ["METAL"]),
    ("NIFTY PHARMA", ["PHARMA"]),
    ("NIFTY REALTY", ["REALTY"]),
    ("NIFTY HEALTHCARE", ["HEALTH"]),
    ("NIFTY OIL AND GAS", ["OIL"]),
    ("NIFTY CEMENT", ["CEMENT"]),
    ("NIFTY CHEMICALS", ["CHEMICAL"]),
]


def _get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(NSE_HOME_URL, timeout=10)  # sets cookies the API call needs
    time.sleep(1)
    return session


def _filter_sectoral(indices):
    results = []
    used_raw_names = set()
    for label, required_substrings in SECTORAL_RULES:
        for item in indices:
            raw_name = (item.get("index") or "").strip()
            if raw_name in used_raw_names:
                continue
            name_upper = raw_name.upper()
            if not name_upper.startswith("NIFTY"):
                continue
            if all(sub in name_upper for sub in required_substrings):
                try:
                    pct_change = float(item.get("percentChange", 0))
                except (TypeError, ValueError):
                    pct_change = None
                results.append({"name": raw_name, "percent_change": pct_change})
                used_raw_names.add(raw_name)
                break
    return results


def fetch_sector_momentum():
    """Returns the 23 NSE sectoral indices sorted by today's % change, biggest
    gainer first — or None if NSE couldn't be reached (blocked request, network
    down, API shape changed). Never raises."""
    try:
        session = _get_session()
        resp = session.get(NSE_API_URL, timeout=10)
        resp.raise_for_status()
        raw = resp.json().get("data", [])
    except Exception:
        return None

    sectoral = _filter_sectoral(raw)
    ranked = [s for s in sectoral if s["percent_change"] is not None]
    ranked.sort(key=lambda s: s["percent_change"], reverse=True)
    return ranked or None


def _find_matching_universe_set(sector_name, available_ids):
    """Exact match first (e.g. "NIFTY REALTY" == a "NIFTY REALTY.json" set), then
    a loose substring match either direction (handles a near-miss like "NIFTY
    HEALTHCARE" vs a file named "NIFTY HEALTHCARE INDEX")."""
    if sector_name in available_ids:
        return sector_name
    sector_upper = sector_name.upper()
    for set_id in available_ids:
        set_upper = set_id.upper()
        if sector_upper in set_upper or set_upper in sector_upper:
            return set_id
    return None


def pick_auto_universe_set(available_ids):
    """Walks today's sectoral movers from biggest gainer down, picking the first
    one that has a matching universes/*.json file. Returns a dict with the pick
    and the full ranking (for transparency in the UI/logs), or None if NSE
    couldn't be reached at all. If NSE responded but not one single sector
    (out of all 23) matches any file you have, "chosen" comes back None too —
    that's surfaced to the user rather than silently guessing a default."""
    ranked = fetch_sector_momentum()
    if ranked is None:
        return None

    chosen = None
    for sector in ranked:
        match = _find_matching_universe_set(sector["name"], available_ids)
        if match:
            chosen = {
                "universe_set": match,
                "sector_name": sector["name"],
                "percent_change": sector["percent_change"],
            }
            break

    return {"chosen": chosen, "ranked": ranked}
