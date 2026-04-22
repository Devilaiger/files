"""
config.py — Load and validate all environment variables.
Fail fast at startup if required values are missing.
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        print(f"[FATAL] Missing required env var: {name}", file=sys.stderr)
        sys.exit(1)
    return val


def _int_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    return [int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]


# ── Telegram ───────────────────────────────────────────────────────────────────
API_ID: int = int(_require("API_ID"))
API_HASH: str = _require("API_HASH")
BOT_TOKEN: str = _require("BOT_TOKEN")
SESSION_NAME: str = os.getenv("SESSION_NAME", "bot_session")

# ── MongoDB ────────────────────────────────────────────────────────────────────
MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME: str = os.getenv("DB_NAME", "tgbot")

# ── Bot behaviour ──────────────────────────────────────────────────────────────
# Comma-separated Telegram user IDs that are allowed to run admin commands.
# Leave empty only if you want to use group-admin check exclusively (not recommended).
ADMIN_IDS: list[int] = _int_list("ADMIN_IDS")

# Fuzzy search match threshold (0-100).  90 = very strict, good default.
FUZZY_THRESHOLD: float = float(os.getenv("FUZZY_THRESHOLD", "90"))

# Max results returned by the Show: search.
MAX_SEARCH_RESULTS: int = int(os.getenv("MAX_SEARCH_RESULTS", "5"))

# Triggers shown per page in /trigger_list.
TRIGGERS_PER_PAGE: int = int(os.getenv("TRIGGERS_PER_PAGE", "10"))

# How many historic messages to pull when indexing a channel for the first time.
INDEX_LIMIT: int = int(os.getenv("INDEX_LIMIT", "5000"))
