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

# ── Admin check ────────────────────────────────────────────────────────────────

async def is_user_admin(event, chat_id: int = None) -> bool:
    """
    Check if the sender is a Telegram admin/creator in the chat.
    Handles Anonymous Admins (where sender_id == chat_id).
    """
    target_chat_id = chat_id or event.chat_id
    sender_id = event.sender_id

    if not target_chat_id:
        logger.info("Admin check denied: No target_chat_id found.")
        return False

    # If it's a private chat (user ID), we can't be a "Telegram admin" in the group sense
    # unless target_chat_id is explicitly a group ID.
    if event.is_private and chat_id is None:
        return False

    # Anonymous Admin check (only if target is a group/channel)
    if sender_id == target_chat_id:
        logger.info(f"Admin check granted: User {sender_id} is Anonymous Admin of {target_chat_id}")
        return True

    try:
        # Check Telegram permissions
        perms = await event.client.get_permissions(target_chat_id, sender_id)
        is_adm = perms.is_admin or perms.is_creator
        if is_adm:
            logger.info(f"Admin check granted: User {sender_id} is Telegram admin/creator of {target_chat_id}")
        return is_adm
    except Exception as e:
        logger.debug(f"is_user_admin check failed for {sender_id} in {target_chat_id}: {e}")
        return False


async def is_admin(event, chat_id: int = None) -> bool:
    """
    Return True if the event sender is an authorised admin for the target chat.
    If chat_id is not provided, defaults to event.chat_id.

    Plan B Hierarchy:
      1. Authorized Bot Administrator: config.ADMIN_IDS
      2. Adder: The user who invited the bot (db.get_group_adder)
      3. Bot Admin: Users explicitly added via /add_bot_admin (db.get_group_authority)
      4. Telegram Admin: Standard group admins, unless explicitly banned.
    """
    sender_id = event.sender_id
    if sender_id in config.ADMIN_IDS:
        logger.info(f"Admin check granted: User {sender_id} is an Authorized Bot Administrator.")
        return True

    target_chat_id = chat_id or event.chat_id
    if not target_chat_id:
        logger.info("Admin check denied: No target_chat_id for authorization.")
        return False

    # If we are in a PM and no specific group was targetted, standard user is not admin.
    if event.is_private and chat_id is None:
        return False

    import db  # Local import to avoid circularity

    # 1. Check if user is the Adder
    adder_id = await db.get_group_adder(target_chat_id)
    if sender_id == adder_id:
        logger.info(f"Admin check granted: User {sender_id} is the Adder of {target_chat_id}")
        return True

    # 2. Check if user is in allowed_ids or banned_ids
    auth = await db.get_group_authority(target_chat_id)
    if auth:
        if sender_id in auth.get("allowed_ids", []):
            logger.info(f"Admin check granted: User {sender_id} is an authorized Bot Admin for {target_chat_id}")
            return True
        if sender_id in auth.get("banned_ids", []):
            logger.info(f"Admin check denied: User {sender_id} is explicitly banned in {target_chat_id}")
            return False

    # 3. Fallback: Standard Telegram Admin check
    return await is_user_admin(event, chat_id=target_chat_id)


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
        created_by = t.get("created_by_name") or "System"
        lines.append(f"`{i}.` {trigger_text} (by {created_by})")

    return "\n".join(lines), total_pages, page


async def get_bot_permissions(client, chat_id: int):
    """
    Check if the bot has admin rights and specific permissions in the chat.
    Returns a permissions object or None if not an admin.
    """
    try:
        me = await client.get_me()
        return await client.get_permissions(chat_id, me)
    except Exception:
        return None
