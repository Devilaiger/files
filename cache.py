"""
cache.py — Per-group in-memory cache for the trigger lists.

Structure
─────────
  _triggers: dict[group_id (int) → list[trigger_doc]]

Matching rule
─────────────
  Longest trigger that is a substring of the incoming text wins.
  This prevents short triggers from silencing more specific ones.

Lifecycle
─────────
  warm()              — load all triggers at startup, grouped by group_id
  invalidate_group()  — re-fetch only the affected group after a write
  find_match()        — O(n) over the group's trigger list (hundreds at most)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# group_id → list of trigger documents
_triggers: dict[int, list[dict]] = {}
_lock: asyncio.Lock = asyncio.Lock()


async def warm(db_fetch_all: Callable[[], Coroutine]) -> None:
    """
    Load ALL triggers from DB at startup.
    Groups them by group_id in memory.
    """
    global _triggers
    all_docs = await db_fetch_all()
    grouped: dict[int, list[dict]] = {}
    for doc in all_docs:
        gid = doc.get("group_id")
        if gid is None:
            # Skip documents missing group_id (pre-migration remnants)
            logger.warning("cache.warm: skipping trigger '%s' — no group_id", doc.get("trigger"))
            continue
        grouped.setdefault(gid, []).append(doc)
    async with _lock:
        _triggers = grouped
    total = sum(len(v) for v in grouped.values())
    logger.info("Trigger cache warmed: %d triggers across %d group(s)", total, len(grouped))


async def invalidate_group(
    group_id: int,
    db_fetch_for_group: Callable[[int], Coroutine],
) -> None:
    """
    Re-fetch triggers for a single group after a write (add/delete).
    Only the affected group's slice is refreshed; other groups untouched.
    """
    docs = await db_fetch_for_group(group_id)
    async with _lock:
        _triggers[group_id] = docs
    logger.debug("Trigger cache refreshed for group %d: %d entries", group_id, len(docs))


def find_match(group_id: int, text: str) -> Optional[dict]:
    """
    Return the trigger with the LONGEST matching keyword that is a substring
    of `text` (case-insensitive), within the given group.

    Longest match wins — prevents short generic triggers from hiding longer ones.
    Returns None if no trigger matches.
    """
    if not text:
        return None
    text_lower = text.lower()
    group_triggers = _triggers.get(group_id, [])
    matched = [t for t in group_triggers if t["trigger"] in text_lower]
    if not matched:
        return None
    return max(matched, key=lambda t: len(t["trigger"]))


def snapshot(group_id: int) -> list[dict]:
    """Shallow copy of triggers for a group (for debugging / listing)."""
    return list(_triggers.get(group_id, []))
