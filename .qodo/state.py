"""
state.py — Simple in-memory FSM for multi-step conversations.

States used by Feature 1 (trigger setup):
  AWAIT_TRIGGER_TEXT    – bot asked the admin to type the trigger text
  AWAIT_TRIGGER_MSG     – bot asked the admin to send the message to attach

No persistent storage needed; state is lost on bot restart (acceptable,
since these are short-lived admin interactions).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ── State constants ────────────────────────────────────────────────────────────
AWAIT_TRIGGER_TEXT = "await_trigger_text"
AWAIT_TRIGGER_MSG  = "await_trigger_msg"


@dataclass
class ConvState:
    step: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_store: dict[int, ConvState] = {}


# ── Public API ─────────────────────────────────────────────────────────────────

def set(user_id: int, step: str, **data: Any) -> None:
    """Set or replace the active state for a user."""
    _store[user_id] = ConvState(step=step, data=dict(data))


def get(user_id: int) -> Optional[ConvState]:
    """Return current state, or None if user has none."""
    return _store.get(user_id)


def update(user_id: int, **data: Any) -> None:
    """Merge extra data into an existing state without changing the step."""
    s = _store.get(user_id)
    if s:
        s.data.update(data)


def clear(user_id: int) -> None:
    """Remove state for a user."""
    _store.pop(user_id, None)


def has(user_id: int) -> bool:
    return user_id in _store
