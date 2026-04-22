"""
triggers.py — Feature 1: Per-Group Trigger -> Message Replay

Permission model
----------------
  env ADMIN_IDS  : super-admin, unlimited access to any group.
                   Can use trigger commands from PM by supplying group_id.
  Group admin    : can manage triggers ONLY for their own group, from inside it.
  Normal user    : no access.

Commands (inside a group — group admin OR env admin):
  /set_trigger              – interactive wizard
  /set_trigger <text>       – direct
  /set_trigger  [reply]     – reply-based (in-group only)
  /trigger_list             – list triggers for this group
  /delete <index>           – delete trigger by index
  /cancel                   – abort wizard

Commands from PM (env ADMIN_IDS only):
  /set_trigger <group_id>             – wizard for specific group
  /set_trigger <group_id> <text>      – direct for specific group
  /trigger_list <group_id>            – list triggers for specific group
  /delete <group_id> <index>          – delete trigger
"""
from __future__ import annotations

import logging

from telethon import TelegramClient, events
from telethon.events import StopPropagation
from telethon.tl.custom import Button
from telethon.tl.types import (
    DocumentAttributeSticker,
    MessageMediaDocument,
    MessageMediaWebPage,
)

import cache
import config
import db
import state
from helpers import (
    build_trigger_list_text,
    normalize_trigger,
)

logger = logging.getLogger(__name__)

# ── Storage channel entity (resolved once at startup) ─────────────────────────
_storage_peer = None


async def resolve_storage_peer(client) -> bool:
    global _storage_peer
    try:
        _storage_peer = await client.get_input_entity(config.STORAGE_CHANNEL_ID)
        logger.info("Storage channel resolved: %s", config.STORAGE_CHANNEL_ID)
        return True
    except Exception as e:
        logger.error(
            "Cannot resolve storage channel %s: %s\n"
            "Make sure the bot is an admin of STORAGE_CHANNEL_ID before starting.",
            config.STORAGE_CHANNEL_ID, e,
        )
        return False


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _refresh_group_cache(group_id: int) -> None:
    await cache.invalidate_group(group_id, db.fetch_triggers_for_group)


def _nav_buttons(page: int, total_pages: int) -> list | None:
    if total_pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(Button.inline("Prev", f"tpage:{page - 1}".encode()))
    row.append(Button.inline(f"{page + 1}/{total_pages}", b"tpage:noop"))
    if page < total_pages - 1:
        row.append(Button.inline("Next", f"tpage:{page + 1}".encode()))
    return [row]


def _is_sticker(media) -> bool:
    """Return True if the media is a Telegram sticker (can't have a caption)."""
    if not isinstance(media, MessageMediaDocument):
        return False
    if not getattr(media, "document", None):
        return False
    return any(
        isinstance(a, DocumentAttributeSticker)
        for a in (media.document.attributes or [])
    )


async def _store_media_in_storage(client, source_chat_id: int, source_msg_id: int,
                                   media, meta_caption: str) -> int | None:
    """
    Copy media to the storage channel WITHOUT a 'Forwarded from' header.

    Strategy (each step is a fallback):
      1. send_file with metadata caption   — clean, labelled entry
      2. send_file without caption          — for types that reject captions (stickers)
      3. forward_messages                   — last resort

    Returns the storage message ID on success, or None on total failure.
    """
    # Stickers cannot carry a caption via the API; skip straight to step 2.
    if _is_sticker(media):
        try:
            sent = await client.send_file(_storage_peer, media, silent=True)
            return sent.id
        except Exception as e:
            logger.warning("send_file (no caption) failed for sticker: %s — trying forward", e)
            try:
                fwd = await client.forward_messages(_storage_peer, source_msg_id, source_chat_id)
                stored = fwd[0] if isinstance(fwd, list) else fwd
                return stored.id
            except Exception as fe:
                logger.error("All storage methods failed for sticker: %s", fe)
                return None

    # Step 1: send_file WITH caption (photos, videos, documents, audio, voice…)
    try:
        sent = await client.send_file(
            _storage_peer, media,
            caption=meta_caption,
            parse_mode=None,
            silent=True,
        )
        return sent.id
    except Exception as e:
        logger.warning("send_file with caption failed: %s — retrying without caption", e)

    # Step 2: send_file WITHOUT caption
    try:
        sent = await client.send_file(_storage_peer, media, silent=True)
        return sent.id
    except Exception as e:
        logger.warning("send_file without caption failed: %s — falling back to forward", e)

    # Step 3: forward_messages (last resort — will show "Forwarded from" header)
    try:
        fwd = await client.forward_messages(_storage_peer, source_msg_id, source_chat_id)
        stored = fwd[0] if isinstance(fwd, list) else fwd
        return stored.id
    except Exception as e:
        logger.error("All storage methods failed: %s", e)
        return None


# ── Permission helpers ─────────────────────────────────────────────────────────

async def _resolve_group_id(event) -> tuple:
    if not event.is_private:
        return event.chat_id, None

    if event.sender_id not in config.ADMIN_IDS:
        return None, (
            "Trigger commands must be run inside a registered search group.\n"
            "Use /connect_as search in the target group first."
        )

    parts = event.text.strip().split(maxsplit=2)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return None, (
            "From PM, supply the group ID as the first argument:\n"
            "/set_trigger <group_id> [trigger_text]\n\n"
            "Get the group ID from /list_search_groups"
        )

    return int(parts[1]), None


async def _require_trigger_permission(event, group_id: int) -> bool:
    sender_id = event.sender_id
    if sender_id in config.ADMIN_IDS:
        return True
    if not event.is_private and event.chat_id == group_id:
        try:
            perms = await event.client.get_permissions(event.chat_id, sender_id)
            if perms.is_admin or perms.is_creator:
                return True
        except Exception:
            pass
    return False


async def _require_search_group(group_id: int, reply_event) -> bool:
    if not await db.is_search_group(group_id):
        await reply_event.reply(
            f"Group {group_id} is not a registered search group.\n"
            "Run /add_channel_search inside that group first."
        )
        return False
    return True


# ==============================================================================
#  COMMAND HANDLERS
# ==============================================================================

async def cmd_set_trigger(event: events.NewMessage.Event) -> None:
    group_id, err = await _resolve_group_id(event)
    if err:
        await event.reply(err)
        raise StopPropagation

    if not await _require_trigger_permission(event, group_id):
        await event.reply("No permission to manage triggers for this group.")
        raise StopPropagation

    if not await _require_search_group(group_id, event):
        raise StopPropagation

    sender_id = event.sender_id

    raw_parts = event.text.strip().split(maxsplit=1)
    arg_portion = raw_parts[1] if len(raw_parts) > 1 else ""
    if event.is_private and arg_portion:
        sub = arg_portion.split(maxsplit=1)
        arg_portion = sub[1] if len(sub) > 1 else ""

    has_arg = bool(arg_portion)
    has_reply = event.is_reply and not event.is_private

    # Method 3: reply to existing message (in-group only)
    if has_reply and not has_arg:
        replied = await event.get_reply_message()
        if not replied or not replied.text:
            await event.reply("The replied-to message has no text to use as trigger.")
            raise StopPropagation
        trigger_text = normalize_trigger(replied.text.split()[0])
        state.set(sender_id, state.AWAIT_TRIGGER_MSG, trigger_text=trigger_text, group_id=group_id)
        await event.reply(
            f"Trigger: `{trigger_text}`\n\n"
            "Now **send the message** to attach — text, image, video, document, sticker, or forward any message.",
            parse_mode="md",
        )
        raise StopPropagation

    # Method 2: text provided inline
    if has_arg:
        trigger_text = normalize_trigger(arg_portion)
        if not trigger_text:
            await event.reply("Trigger text is empty after normalisation.")
            raise StopPropagation
        state.set(sender_id, state.AWAIT_TRIGGER_MSG, trigger_text=trigger_text, group_id=group_id)
        await event.reply(
            f"Trigger: `{trigger_text}`\n\n"
            "Now **send the message** to attach — text, image, video, document, sticker, or forward any message.",
            parse_mode="md",
        )
        raise StopPropagation

    # Method 1: interactive wizard
    state.set(sender_id, state.AWAIT_TRIGGER_TEXT, group_id=group_id)
    await event.reply(
        f"Send the trigger text (keyword to watch for).\nGroup: `{group_id}`",
        parse_mode="md",
    )
    raise StopPropagation


async def cmd_trigger_list(event: events.NewMessage.Event) -> None:
    group_id, err = await _resolve_group_id(event)
    if err:
        await event.reply(err)
        raise StopPropagation

    if not await _require_trigger_permission(event, group_id):
        await event.reply("No permission to view triggers for this group.")
        raise StopPropagation

    if not await _require_search_group(group_id, event):
        raise StopPropagation

    triggers = await db.fetch_triggers_for_group(group_id)
    text, total_pages, page = build_trigger_list_text(triggers, 0, config.TRIGGERS_PER_PAGE)
    buttons = _nav_buttons(0, total_pages)
    await event.reply(text, buttons=buttons, parse_mode="md")
    raise StopPropagation


async def cmd_delete_trigger(event: events.NewMessage.Event) -> None:
    group_id, err = await _resolve_group_id(event)
    if err:
        await event.reply(err)
        raise StopPropagation

    if not await _require_trigger_permission(event, group_id):
        await event.reply("No permission to delete triggers for this group.")
        raise StopPropagation

    if not await _require_search_group(group_id, event):
        raise StopPropagation

    raw_parts = event.text.strip().split()
    if event.is_private:
        if len(raw_parts) < 3 or not raw_parts[2].isdigit():
            await event.reply("Usage from PM: /delete <group_id> <index>")
            raise StopPropagation
        index = int(raw_parts[2])
    else:
        if len(raw_parts) < 2 or not raw_parts[1].isdigit():
            await event.reply("Usage: /delete <index>\nGet index from /trigger_list")
            raise StopPropagation
        index = int(raw_parts[1])

    success, deleted_text = await db.delete_trigger_at_index(group_id, index)
    if success:
        await _refresh_group_cache(group_id)
        await event.reply(f"Deleted trigger #{index}: {deleted_text}")
    else:
        total = len(await db.fetch_triggers_for_group(group_id))
        await event.reply(
            f"Invalid index {index}. Valid range: 1-{total}.\nUse /trigger_list to see the list."
        )
    raise StopPropagation


async def cmd_cancel(event: events.NewMessage.Event) -> None:
    if state.has(event.sender_id):
        state.clear(event.sender_id)
        await event.reply("Cancelled.")
    else:
        await event.reply("Nothing to cancel.")
    raise StopPropagation


# ── Callback: pagination ───────────────────────────────────────────────────────

async def cb_trigger_page(event: events.CallbackQuery.Event) -> None:
    data = event.data.decode()
    if data == "tpage:noop":
        await event.answer()
        return

    try:
        page = int(data.split(":")[1])
    except (IndexError, ValueError):
        await event.answer("Invalid page.")
        return

    triggers = await db.fetch_triggers_for_group(event.chat_id)
    text, total_pages, page = build_trigger_list_text(triggers, page, config.TRIGGERS_PER_PAGE)
    buttons = _nav_buttons(page, total_pages)
    try:
        await event.edit(text, buttons=buttons, parse_mode="md")
    except Exception:
        pass
    await event.answer()


# ── In-flight guard ───────────────────────────────────────────────────────────
_in_flight: set[tuple[int, int]] = set()


# ==============================================================================
#  STATE REPLY HANDLER
# ==============================================================================

async def handle_state_reply(event: events.NewMessage.Event) -> bool:
    sender_id = event.sender_id
    msg_id = event.id
    flight_key = (sender_id, msg_id)

    current = state.get(sender_id)
    if not current:
        return False

    if flight_key in _in_flight:
        return True
    _in_flight.add(flight_key)

    try:
        return await _handle_state_reply_inner(event, sender_id, current)
    finally:
        _in_flight.discard(flight_key)


async def _handle_state_reply_inner(event, sender_id: int, current) -> bool:
    # ── Step 1: waiting for trigger text ─────────────────────────────────────
    if current.step == state.AWAIT_TRIGGER_TEXT:
        trigger_text = normalize_trigger(event.text or "")
        if not trigger_text:
            await event.reply("❌ Please send a non-empty trigger text.")
            return True
        state.set(
            sender_id,
            state.AWAIT_TRIGGER_MSG,
            trigger_text=trigger_text,
            group_id=current.data.get("group_id"),
        )
        await event.reply(
            f"📌 Trigger: `{trigger_text}`\n\n"
            "Now **send the message** to attach — text, image, video, document, "
            "sticker, or forward any message.",
            parse_mode="md",
        )
        return True

    # ── Step 2: waiting for the response message to store ─────────────────────
    if current.step == state.AWAIT_TRIGGER_MSG:
        trigger_text = current.data.get("trigger_text", "")
        group_id = current.data.get("group_id")

        if not trigger_text or not group_id:
            state.clear(sender_id)
            await event.reply(
                "❌ Internal error: state data lost. "
                "Start over with /set_trigger inside the search group."
            )
            return True

        msg = await event.client.get_messages(event.chat_id, ids=event.id)
        if msg is None:
            await event.reply("❌ Could not read your message. Try again.")
            return True

        content_text = msg.text or msg.message or ""

        # TEXT / LINK: no media, OR only a web-page preview, AND has actual text
        is_text_or_link = (
            not msg.media
            or isinstance(msg.media, MessageMediaWebPage)
        ) and bool(content_text)

        # ── TEXT / LINK path ────────────────────────────────────────────────
        if is_text_or_link:
            await db.upsert_trigger(
                trigger_text, group_id,
                storage_type="text",
                storage_text=content_text,
                storage_chat_id=None,
                storage_message_id=None,
            )
            await _refresh_group_cache(group_id)
            state.clear(sender_id)
            await event.reply(
                f"✅ Trigger saved!\n\n"
                f"🔑 Keyword: `{trigger_text}`\n"
                f"📜 Type: text/link — stored directly in DB\n\n"
                "You can delete this message from the group — the trigger still works.",
                parse_mode="md",
            )
            return True

        # ── MEDIA path (photo, video, document, audio, voice, sticker…) ─────
        if _storage_peer is None:
            state.clear(sender_id)
            await event.reply(
                "❌ Storage channel not resolved at startup.\n"
                "Restart the bot and confirm STORAGE_CHANNEL_ID is correct "
                "and bot is an admin there. Then run /set_trigger again."
            )
            return True

        # Build metadata caption (written into the storage message so the
        # storage channel stays self-documenting)
        meta_caption = (
            f"#trigger | {trigger_text} | group: {group_id}\n"
            + (content_text or "")
        ).strip()

        # Store using send_file (clean copy — no "Forwarded from" header).
        # Falls back through multiple strategies; stickers handled specially.
        storage_msg_id = await _store_media_in_storage(
            event.client, event.chat_id, event.id, msg.media, meta_caption
        )

        if storage_msg_id is None:
            state.clear(sender_id)
            await event.reply(
                "❌ Could not save media to storage channel.\n"
                "Make sure the bot is an admin of STORAGE_CHANNEL_ID "
                "with permission to post messages, then run /set_trigger again."
            )
            return True

        await db.upsert_trigger(
            trigger_text, group_id,
            storage_type="media",
            storage_text=None,
            storage_chat_id=config.STORAGE_CHANNEL_ID,
            storage_message_id=storage_msg_id,
        )
        await _refresh_group_cache(group_id)
        state.clear(sender_id)

        media_type = "sticker" if _is_sticker(msg.media) else "media"
        await event.reply(
            f"✅ Trigger saved!\n\n"
            f"🔑 Keyword: `{trigger_text}`\n"
            f"📦 Type: {media_type} — stored in storage channel (msg `{storage_msg_id}`)\n\n"
            "You can delete this message from the group — the trigger still works.",
            parse_mode="md",
        )
        return True

    return False


# ==============================================================================
#  RUNTIME: TRIGGER MATCHING
# ==============================================================================

async def handle_trigger_match(event: events.NewMessage.Event) -> bool:
    text = event.text or event.caption or ""
    if not text:
        return False

    group_id = event.chat_id
    matched = cache.find_match(group_id, text)
    if not matched:
        return False

    logger.info(
        "Trigger '%s' matched in group %s (msg %s)",
        matched["trigger"], group_id, event.id,
    )

    stype = matched.get("storage_type", "media")

    # ── TEXT / LINK delivery ──────────────────────────────────────────────────
    if stype == "text":
        stored_text = matched.get("storage_text", "")
        if not stored_text:
            logger.warning("Trigger '%s' has storage_type=text but no storage_text", matched["trigger"])
            return True
        try:
            await event.client.send_message(
                event.chat_id, stored_text,
                link_preview=True,
            )
        except Exception as e:
            logger.warning("Text trigger '%s' send failed: %s", matched["trigger"], e)
        return True

    # ── MEDIA delivery — copy from storage (no "Forwarded from" header) ───────
    chat_id = matched.get("storage_chat_id") or matched.get("source_chat_id")
    msg_id  = matched.get("storage_message_id") or matched.get("source_message_id")

    if not chat_id or not msg_id:
        logger.warning(
            "Trigger '%s' has no storage reference — re-create with /set_trigger",
            matched["trigger"],
        )
        return True

    try:
        stored_msg = await event.client.get_messages(chat_id, ids=msg_id)
        if stored_msg is None:
            logger.warning(
                "Storage message %s/%s not found — trigger '%s' must be re-created",
                chat_id, msg_id, matched["trigger"],
            )
            return True

        # Strip the metadata line we wrote at storage time
        caption = stored_msg.text or stored_msg.message or ""
        if caption.startswith("#trigger |"):
            lines = caption.split("\n", 2)
            caption = lines[-1].strip() if len(lines) > 1 else ""

        if stored_msg.media:
            await event.client.send_file(
                event.chat_id,
                stored_msg.media,
                caption=caption or None,
                parse_mode="html",
            )
        elif caption:
            await event.client.send_message(
                event.chat_id, caption,
                link_preview=True,
            )
    except Exception as e:
        logger.warning("Media trigger '%s' delivery failed: %s", matched["trigger"], e)
    return True


# ==============================================================================
#  /refresh
# ==============================================================================

async def cmd_refresh(event: events.NewMessage.Event) -> None:
    group_id, err = await _resolve_group_id(event)
    if err:
        await event.reply(err)
        raise StopPropagation

    if not await _require_trigger_permission(event, group_id):
        await event.reply("⛔ No permission to refresh this group.")
        raise StopPropagation

    await _refresh_group_cache(group_id)

    if not event.is_private:
        try:
            await event.client.get_participants(
                event.chat_id,
                filter=__import__(
                    "telethon.tl.types", fromlist=["ChannelParticipantsAdmins"]
                ).ChannelParticipantsAdmins(),
            )
        except Exception:
            pass

    count = len(cache.snapshot(group_id))
    await event.reply(
        f"✅ Refreshed!\n"
        f"Trigger cache for group `{group_id}`: `{count}` trigger(s) loaded.\n"
        "Admin permissions re-fetched from Telegram.",
        parse_mode="md",
    )
    raise StopPropagation


# ==============================================================================
#  REGISTRATION
# ==============================================================================

def register(client: TelegramClient) -> None:
    client.add_event_handler(
        cmd_set_trigger,
        events.NewMessage(pattern=r"^/set_trigger(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_trigger_list,
        events.NewMessage(pattern=r"^/trigger_list(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_delete_trigger,
        events.NewMessage(pattern=r"^/delete(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_cancel,
        events.NewMessage(pattern=r"^/cancel(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_refresh,
        events.NewMessage(pattern=r"^/refresh(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cb_trigger_page,
        events.CallbackQuery(pattern=rb"^tpage:"),
    )
    logger.info("Trigger handlers registered.")
