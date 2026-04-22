"""
features/helpers.py — Shared utility functions used across features.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    MessageIdInvalidError,
    UserNotParticipantError,
)
from telethon.tl.types import Channel, Chat

import config

logger = logging.getLogger(__name__)


# ── Admin check ────────────────────────────────────────────────────────────────

async def is_admin(event) -> bool:
    """
    Return True if the event sender is an authorised admin.

    Priority:
      1. Sender ID is in ADMIN_IDS (config).
      2. Sender is creator / admin of the current group/channel.
    """
    sender_id = event.sender_id
    if sender_id in config.ADMIN_IDS:
        return True

    # Fallback: group/channel admin check
    if hasattr(event, "is_group") and (event.is_group or getattr(event, "is_channel", False)):
        try:
            perms = await event.client.get_permissions(event.chat_id, sender_id)
            return perms.is_admin or perms.is_creator
        except Exception:
            pass

    return False


# ── Text normalisation ─────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """
    Normalise a string for search indexing / fuzzy matching:
      - Unicode NFKC
      - Lowercase
      - Strip punctuation (keep letters, digits, spaces)
      - Collapse whitespace
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_trigger(text: str) -> str:
    """Lightweight normalisation for trigger keywords (just lowercase + strip)."""
    return text.lower().strip()


# ── Message resend / forward ───────────────────────────────────────────────────

async def forward_or_copy(
    client: TelegramClient,
    source_chat_id: int,
    source_msg_id: int,
    target_chat,
) -> bool:
    """
    Try to forward a message from source_chat_id to target_chat.
    Falls back to copying text/media if forwarding is restricted.
    Returns True on success.
    """
    try:
        await client.forward_messages(target_chat, source_msg_id, source_chat_id)
        return True
    except (MessageIdInvalidError, ValueError):
        logger.warning(
            "Source message %s/%s no longer exists.", source_chat_id, source_msg_id
        )
        return False
    except FloodWaitError as e:
        logger.error("FloodWait: sleeping %ds", e.seconds)
        import asyncio
        await asyncio.sleep(e.seconds)
        return False
    except Exception as forward_err:
        logger.warning("Forward failed (%s), trying copy…", forward_err)

    # ── Fallback: copy ────────────────────────────────────────────────────────
    try:
        msg = await client.get_messages(source_chat_id, ids=source_msg_id)
        if msg is None:
            return False
        if msg.media:
            await client.send_file(
                target_chat,
                msg.media,
                caption=msg.text or "",
                parse_mode="html",
            )
        elif msg.text:
            await client.send_message(target_chat, msg.text, parse_mode="html")
        else:
            return False
        return True
    except Exception as copy_err:
        logger.error("Copy also failed: %s", copy_err)
        return False


# ── Channel resolver ───────────────────────────────────────────────────────────

async def resolve_channel(
    client: TelegramClient, identifier: str
) -> Optional[tuple[int, Optional[str], str]]:
    """
    Resolve a channel identifier (username or numeric ID) to
    (channel_id, username_or_None, title).

    Returns None if the channel cannot be accessed.
    """
    # Strip leading @
    ident = identifier.strip().lstrip("@")
    # Try numeric
    if ident.lstrip("-").isdigit():
        abs_val = abs(int(ident))
        str_abs = str(abs_val)
        # Already in -100XXXXXXXXXX format (13+ digits starting with 100)
        if str_abs.startswith("100") and len(str_abs) >= 12:
            ident_parsed: int | str = -abs_val
        else:
            # Bare peer ID (positive or wrong-negative like -5185720910)
            ident_parsed = int(f"-100{abs_val}")
    else:
        ident_parsed = ident  # username string

    try:
        entity = await client.get_entity(ident_parsed)
    except (ValueError, ChannelPrivateError, UserNotParticipantError) as e:
        logger.warning("Cannot resolve channel %s: %s", identifier, e)
        return None
    except Exception as e:
        logger.error("Unexpected error resolving channel %s: %s", identifier, e)
        return None

    if isinstance(entity, (Channel, Chat)):
        username = getattr(entity, "username", None)
        title = getattr(entity, "title", str(entity.id))
        ch_id = int(f"-100{entity.id}") if entity.id > 0 else entity.id
        return ch_id, username, title

    return None


# ── Pagination helper ──────────────────────────────────────────────────────────

def paginate(items: list, page: int, per_page: int) -> tuple[list, int, int]:
    """
    Return (page_items, total_pages, clamped_page).
    page is 0-indexed.
    """
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    return items[start : start + per_page], total_pages, page


def build_trigger_list_text(
    triggers: list[dict], page: int, per_page: int
) -> tuple[str, int, int]:
    """
    Build the trigger list message text and return
    (text, total_pages, current_page).
    """
    page_items, total_pages, page = paginate(triggers, page, per_page)
    offset = page * per_page

    if not triggers:
        return "📭 No triggers configured yet.", 1, 0

    lines = [f"🔑 **Triggers** — Page {page + 1}/{total_pages}\n"]
    for i, t in enumerate(page_items, start=offset + 1):
        trigger_text = t["trigger"]
        lines.append(f"`{i}.` {trigger_text}")

    return "\n".join(lines), total_pages, page
