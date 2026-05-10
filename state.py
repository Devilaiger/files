"""
state.py — Simple in-memory FSM for multi-step conversations.

States used by Feature 1 (trigger setup):
  AWAIT_TRIGGER_TEXT    – bot asked the admin to type the trigger text
  AWAIT_TRIGGER_MSG     – bot asked the admin to send the message to attach

PERSISTENCE
-----------
  The primary store is an in-memory dict (_store) for maximum speed.
  A background asyncio task (started via start_background_save) silently
  writes the entire store to state.json every 5 seconds.
  On bot startup, call load() to restore sessions that were in progress
  before the last restart — admins seamlessly pick up where they left off.

  Keys serialised as "<user_id>:<chat_id>"; created_at as ISO-8601 string.
  All session data values are JSON-safe primitives (str / int / None).
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("bot.state")

# ── State constants ────────────────────────────────────────────────────────────
AWAIT_TRIGGER_TEXT = "await_trigger_text"
AWAIT_TRIGGER_MSG  = "await_trigger_msg"

# ── Persistence path ───────────────────────────────────────────────────────────
_STATE_FILE = pathlib.Path("state.json")

# ── Background-save interval (seconds) ────────────────────────────────────────
_SAVE_INTERVAL: int = 5


@dataclass
class ConvState:
    step: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_store: dict[Any, ConvState] = {}


def key(user_id: int, chat_id: int) -> tuple[int, int]:
    """Conversation key scoped to one user in one chat."""
    return (user_id, chat_id)


# ── Public API ─────────────────────────────────────────────────────────────────

def set(user_id: Any, step: str, **data: Any) -> None:
    """Set or replace the active state for a user."""
    _store[user_id] = ConvState(step=step, data=dict(data))


def get(user_id: Any) -> Optional[ConvState]:
    """Return current state, or None if user has none."""
    return _store.get(user_id)


def update(user_id: Any, **data: Any) -> None:
    """Merge extra data into an existing state without changing the step."""
    s = _store.get(user_id)
    if s:
        s.data.update(data)


def clear(user_id: Any) -> None:
    """Remove state for a user."""
    _store.pop(user_id, None)


def has(user_id: Any) -> bool:
    return user_id in _store


def find_for_user(user_id: int) -> tuple[Any, ConvState] | tuple[None, None]:
    """Return any active state for a user, used only for /cancel diagnostics."""
    for k, v in _store.items():
        if k == user_id or (isinstance(k, tuple) and k and k[0] == user_id):
            return k, v
    return None, None


def clear_for_user(user_id: int) -> int:
    """Clear every active state owned by a user. Returns number removed."""
    keys = [
        k for k in _store
        if k == user_id or (isinstance(k, tuple) and k and k[0] == user_id)
    ]
    for k in keys:
        _store.pop(k, None)
    return len(keys)


# ── Serialisation helpers ──────────────────────────────────────────────────────

def _key_to_str(k: Any) -> str:
    """Convert a store key (tuple or int) to a JSON-safe string."""
    if isinstance(k, tuple):
        return f"{k[0]}:{k[1]}"
    return str(k)


def _str_to_key(s: str) -> Any:
    """Reverse of _key_to_str."""
    if ":" in s:
        parts = s.split(":", 1)
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            return s
    try:
        return int(s)
    except ValueError:
        return s


def _store_to_dict() -> dict:
    """Serialise _store to a plain JSON-safe dict."""
    out: dict = {}
    for k, v in _store.items():
        out[_key_to_str(k)] = {
            "step": v.step,
            "data": v.data,
            "created_at": v.created_at.isoformat(),
        }
    return out


def _dict_to_store(d: dict) -> None:
    """Deserialise a JSON dict into _store (clears existing data first)."""
    _store.clear()
    for str_key, raw in d.items():
        k = _str_to_key(str_key)
        try:
            created_at = datetime.fromisoformat(raw["created_at"])
        except Exception:
            created_at = datetime.now(timezone.utc)
        _store[k] = ConvState(
            step=raw["step"],
            data=raw.get("data", {}),
            created_at=created_at,
        )


# ── Disk persistence ───────────────────────────────────────────────────────────

def save() -> None:
    """
    Persist the current in-memory state to state.json (blocking, safe).
    Called automatically by the background task; can also be called manually.
    """
    try:
        payload = json.dumps(_store_to_dict(), ensure_ascii=False)
        _STATE_FILE.write_text(payload, encoding="utf-8")
    except Exception as e:
        logger.warning("State save failed: %s", e)


def load() -> None:
    """
    Load sessions from state.json into RAM on bot startup.
    No-op if the file doesn't exist or is malformed.
    """
    if not _STATE_FILE.exists():
        logger.debug("No state.json found — starting with empty state.")
        return
    try:
        raw = _STATE_FILE.read_text(encoding="utf-8")
        d = json.loads(raw)
        _dict_to_store(d)
        count = len(_store)
        if count:
            logger.info(
                "Restored %d in-progress wizard session(s) from state.json — "
                "admins can continue without interruption.",
                count,
            )
        else:
            logger.debug("state.json was empty — no sessions to restore.")
    except json.JSONDecodeError as e:
        logger.warning("state.json is corrupted (JSONDecodeError: %s) — starting fresh.", e)
    except Exception as e:
        logger.warning("State load failed: %s — starting fresh.", e)


# ── Background save task ───────────────────────────────────────────────────────

async def _background_save_loop() -> None:
    """Silently save state to disk every _SAVE_INTERVAL seconds."""
    while True:
        await asyncio.sleep(_SAVE_INTERVAL)
        if _store:  # Skip I/O entirely when there are no active sessions
            save()


def start_background_save() -> asyncio.Task:
    """
    Schedule the background save loop.
    Must be called inside a running asyncio event loop (e.g. inside startup()).
    Returns the Task so it can be cancelled on shutdown if needed.
    """
    task = asyncio.ensure_future(_background_save_loop())
    logger.info("State persistence: background save task started (every %ds).", _SAVE_INTERVAL)
    return task
