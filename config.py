"""Tiny .env loader — no python-dotenv dependency. Never hardcode secrets here."""
import os


def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

BRAND = os.environ.get("BRAND", "Nivesh Council")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "").strip().lower()

CONFIDENCE_THRESHOLD = int(os.environ.get("CONFIDENCE_THRESHOLD", "7"))
AGENT_DELAY = float(os.environ.get("AGENT_DELAY", "0.35"))
# Scout shortlists this % of each cap-segment bucket (large/mid/small), rounded —
# e.g. 40% of 10 stocks = 4, 40% of 12 = 5. See data_sources.shortlist().
SHORTLIST_PERCENT = float(os.environ.get("SHORTLIST_PERCENT", "40"))
PORT = int(os.environ.get("PORT", "5000"))

# --- Trade-level formula inputs (BUY signals only) ---
# Stop-loss = entry - (entry * SL_PERCENT/100). Take-profit = entry + (entry - stop_loss) * RISK_REWARD_RATIO.
SL_PERCENT = float(os.environ.get("SL_PERCENT", "3"))
RISK_REWARD_RATIO = float(os.environ.get("RISK_REWARD_RATIO", "2.5"))

# How long to keep buy_logs/*.txt audit dumps before auto-deleting them.
BUY_LOG_RETENTION_DAYS = int(os.environ.get("BUY_LOG_RETENTION_DAYS", "30"))

# --- Scheduled run (optional) — fires one run automatically each weekday ---
SCHEDULE_ENABLED = os.environ.get("SCHEDULE_ENABLED", "false").strip().lower() in ("1", "true", "yes")
SCHEDULE_TIME = os.environ.get("SCHEDULE_TIME", "09:20").strip()  # HH:MM, 24h, IST
SCHEDULE_MODE = os.environ.get("SCHEDULE_MODE", "auto").strip().lower()  # demo | live | auto
