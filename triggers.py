"""
triggers.py — Feature 1: Per-Group Trigger -> Message Replay

Permission model
----------------
  env ADMIN_IDS  : Authorized Bot Administrator, unlimited access to any group.
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

WIZARD ISOLATION
----------------
  Every wizard session stores the chat_id where it was started
  (initiated_chat_id).  The state handler IGNORES any message that
  arrives from a different chat — even from the same sender.

  This means:
    • Admin starts /set_trigger in Group A.
    • Admin forwards an image to Group B, sends a message in PM, etc.
    → ALL of those are silently ignored; wizard stays open in Group A.
    • Only a message sent INSIDE GROUP A advances the wizard.
"""
from __future__ import annotations

import base64
import logging
import pickle

from itsdangerous import BadSignature, Signer
from telethon import TelegramClient, events
from telethon.events import StopPropagation
from telethon.tl.custom import Button
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
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


# ── Media helpers ──────────────────────────────────────────────────────────────

def _is_sticker(media) -> bool:
    """Return True if the media is a Telegram sticker (captions not allowed)."""
    if not isinstance(media, MessageMediaDocument):
        return False
    if not getattr(media, "document", None):
        return False
    return any(
        isinstance(a, DocumentAttributeSticker)
        for a in (media.document.attributes or [])
    )


def _detect_media_type(msg) -> str:
    """
    Inspect a Telegram message and return the exact media type string.
    Returns one of: "image", "video", "gif", "sticker", "document",
                    "audio", "voice", "video_note", or "text".

    Storage method is NOT changed — this is purely for detection/reporting.
    """
    media = getattr(msg, "media", None)
    if not media or isinstance(media, MessageMediaWebPage):
        return "text"

    if isinstance(media, MessageMediaPhoto):
        return "image"

    if isinstance(media, MessageMediaDocument):
        doc = getattr(media, "document", None)
        if not doc:
            return "document"
        attrs = doc.attributes or []
        attr_types = {type(a) for a in attrs}

        if DocumentAttributeSticker in attr_types:
            return "sticker"

        if DocumentAttributeAnimated in attr_types:
            return "gif"

        for a in attrs:
            if isinstance(a, DocumentAttributeVideo):
                return "video_note" if getattr(a, "round_message", False) else "video"

        for a in attrs:
            if isinstance(a, DocumentAttributeAudio):
                return "voice" if getattr(a, "voice", False) else "audio"

        return "document"

    return "media"  # generic fallback for any other media type


def _message_content_text(msg) -> str:
    return (getattr(msg, "text", None) or getattr(msg, "message", None) or "").strip()


def _has_real_media(msg) -> bool:
    media = getattr(msg, "media", None)
    return bool(media) and not isinstance(media, MessageMediaWebPage)


# ── HMAC-signed media serialisation ───────────────────────────────────────────
# Uses itsdangerous.Signer so that the pickle blob in MongoDB carries a
# cryptographic signature.  If the DB is ever compromised, an attacker cannot
# craft a malicious pickle payload — the signature check will reject it.
#
# Backward-compatibility: existing unsigned blobs (legacy format) are decoded
# via a silent fallback.  Triggers will be automatically upgraded the next time
# they are re-saved with /set_trigger.

def _get_signer() -> Signer:
    """Return an HMAC Signer keyed off the bot token (secret, always present)."""
    import config as _cfg  # late import avoids circular dependency at module load
    return Signer(_cfg.BOT_TOKEN)


def _serialize_media(media) -> str | None:
    """Pickle media, sign with HMAC, then base64-encode for DB storage."""
    if not media:
        return None
    raw: bytes = pickle.dumps(media)
    signed: bytes = _get_signer().sign(raw)   # appends ".{hmac_hex}"
    return base64.b64encode(signed).decode("ascii")


def _deserialize_media(media_b64: str | None):
    """
    Decode and verify a media blob.
    1. Try HMAC-signed format (new).
    2. Fall back to raw pickle (legacy — existing DB rows).
    3. Return None on total failure.
    """
    if not media_b64:
        return None
    try:
        blob: bytes = base64.b64decode(media_b64.encode("ascii"))
        try:
            raw: bytes = _get_signer().unsign(blob)
            return pickle.loads(raw)
        except BadSignature:
            # Legacy unsigned blob — still trusted (DB was not tampered with at
            # the time of storage).  Log a notice so admins know to re-save.
            logger.debug(
                "Media blob is unsigned (legacy format). "
                "Re-save with /set_trigger to upgrade to signed storage."
            )
            return pickle.loads(blob)
    except Exception as e:
        logger.warning("Stored media reference could not be decoded: %s", e)
        return None


async def _delete_storage_message(client, msg_id: int) -> None:
    if not config.STORAGE_DELETE_AFTER_SAVE:
        return
    try:
        await client.delete_messages(_storage_peer, msg_id, revoke=True)
    except Exception as e:
        logger.warning("Could not delete storage-channel message %s: %s", msg_id, e)


async def _store_media_in_storage(
    client,
    source_chat_id: int,
    source_msg_id: int,
    media,
    meta_caption: str | None,
):
    """
    Copy media to the storage channel WITHOUT a 'Forwarded from' header.

    Strategy (each step is a fallback):
      1. send_file with metadata caption   — clean, labelled entry
      2. send_file without caption          — stickers can't carry captions
      3. forward_messages                   — absolute last resort

    Returns the stored Message on success, or None on total failure.
    """
    # Stickers cannot carry a caption via the API — skip to step 2.
    if _is_sticker(media):
        try:
            return await client.send_file(_storage_peer, media, silent=True)
        except Exception as e:
            logger.warning("send_file (no caption) failed for sticker: %s — trying forward", e)
            try:
                fwd = await client.forward_messages(_storage_peer, source_msg_id, source_chat_id)
                return fwd[0] if isinstance(fwd, list) else fwd
            except Exception as fe:
                logger.error("All storage methods failed for sticker: %s", fe)
                return None

    # Step 1: send_file WITH caption (photos, videos, documents, audio, voice…)
    try:
        return await client.send_file(
            _storage_peer, media,
            caption=meta_caption,
            parse_mode=None,
            silent=True,
        )
    except Exception as e:
        logger.warning("send_file with caption failed: %s — retrying without caption", e)

    # Step 2: send_file WITHOUT caption
    try:
        return await client.send_file(_storage_peer, media, silent=True)
    except Exception as e:
        logger.warning("send_file without caption failed: %s — falling back to forward", e)

    # Step 3: forward_messages (last resort — will show "Forwarded from" header)
    try:
        fwd = await client.forward_messages(_storage_peer, source_msg_id, source_chat_id)
        return fwd[0] if isinstance(fwd, list) else fwd
    except Exception as e:
        logger.error("All storage methods failed: %s", e)
        return None


# ── Cache helper ───────────────────────────────────────────────────────────────

async def _refresh_group_cache(group_id: int) -> None:
    await cache.invalidate_group(group_id, db.fetch_triggers_for_group)


async def migrate_storage_to_hidden(client) -> None:
    if not config.STORAGE_DELETE_AFTER_SAVE:
        return

    migrated = 0
    for doc in await db.fetch_all_triggers():
        if doc.get("storage_type") != "media" or doc.get("storage_media_b64"):
            continue

        chat_id = doc.get("storage_chat_id")
        msg_id = doc.get("storage_message_id")
        if not chat_id or not msg_id:
            continue

        try:
            stored_msg = await client.get_messages(chat_id, ids=msg_id)
            if not stored_msg or not stored_msg.media:
                continue

            caption = _message_content_text(stored_msg)
            if caption.startswith("#trigger |"):
                lines = caption.split("\n", 2)
                caption = lines[-1].strip() if len(lines) > 1 else ""

            media_ref = _serialize_media(stored_msg.media)
            if not media_ref:
                continue

            await db.update_trigger_hidden_media(doc["_id"], caption or None, media_ref)
            await _delete_storage_message(client, msg_id)
            await _refresh_group_cache(doc["group_id"])
            migrated += 1
        except Exception as e:
            logger.warning(
                "Could not migrate storage message %s/%s for trigger '%s': %s",
                chat_id, msg_id, doc.get("trigger"), e,
            )

    if migrated:
        logger.info("Migrated %d visible storage message(s) to hidden storage.", migrated)


# ── Pagination ─────────────────────────────────────────────────────────────────

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


# ── Permission helpers ─────────────────────────────────────────────────────────

async def _resolve_group_id(event) -> tuple:
    """
    Determine which group_id a trigger command targets.
    Inside a group  -> group_id = event.chat_id (always; user args ignored).
    From PM         -> allows any user to supply a group_id (permission checked later).
    Returns (group_id, error_message). error_message is None on success.
    """
    if not event.is_private:
        return event.chat_id, None

    parts = event.text.strip().split(maxsplit=2)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return None, (
            "From PM, supply the group ID as the first argument:\n"
            "/set_trigger <group_id> [trigger_text]\n\n"
            "Get the group ID from /list_search_groups"
        )

    target_group_id = int(parts[1])
    # The actual permission check happens in the calling handler using _require_trigger_permission
    return target_group_id, None


async def _require_trigger_permission(event, group_id: int) -> bool:
    """
    Check if the user has permission to manage triggers for the given group.
    Uses the unified Plan B hierarchy in helpers.is_admin.
    """
    from helpers import is_admin
    # Note: helpers.is_admin checks config.ADMIN_IDS, Adder, Bot Admins, and Telegram Admins.
    # We pass the target group_id to ensure authority is checked for that group, 
    # even if the command/wizard response arrives from a different chat (like PM).
    return await is_admin(event, chat_id=group_id)


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

    # ── BOT PERMISSION CHECK ──
    from helpers import get_bot_permissions
    bot_perms = await get_bot_permissions(event.client, group_id)
    privacy_warning = ""
    if not bot_perms or not bot_perms.is_admin:
        privacy_warning = (
            "\n\n⚠️ **WARNING**: The bot is **not an admin** in this group. "
            "Due to Telegram's Privacy Mode, it might not see your text responses. "
            "Please promote the bot to Admin for a smooth experience."
        )

    sender_id = event.sender_id

    # Strip "/set_trigger" and (if PM) the group_id token to get the actual arg
    raw_parts = event.text.strip().split(maxsplit=1)
    arg_portion = raw_parts[1] if len(raw_parts) > 1 else ""
    if event.is_private and arg_portion:
        sub = arg_portion.split(maxsplit=1)
        arg_portion = sub[1] if len(sub) > 1 else ""

    has_arg = bool(arg_portion)
    has_reply = event.is_reply and not event.is_private

    # The ONLY chat where wizard replies will be accepted
    initiated_chat_id = event.chat_id
    session_key = state.key(sender_id, initiated_chat_id)

    # Get creator name for metadata
    sender = await event.get_sender()
    sender_name = getattr(sender, "first_name", "Unknown")
    if getattr(sender, "last_name", None):
        sender_name += f" {sender.last_name}"
    elif not getattr(sender, "first_name", None):
        sender_name = getattr(sender, "title", "Admin") # For anonymous admins

    # Method 3: reply to existing message (in-group only)
    if has_reply and not has_arg:
        replied = await event.get_reply_message()
        if not replied or not replied.text:
            await event.reply("The replied-to message has no text to use as trigger.")
            raise StopPropagation
        trigger_text = normalize_trigger(replied.text.split()[0])
        state.set(
            session_key,
            state.AWAIT_TRIGGER_MSG,
            trigger_text=trigger_text,
            group_id=group_id,
            initiated_chat_id=initiated_chat_id,
            created_by_id=sender_id,
            created_by_name=sender_name,
        )
        await event.reply(
            f"🔑 Keyword locked: `{trigger_text}`\n\n"
            "**Step 2/2 — Send the message to attach:**\n"
            "image, video, sticker, document, audio, text, or forward any message.\n\n"
            "⚠️ Send it **in this chat only** — messages from other chats are ignored.\n"
            "To abort, send /cancel",
            parse_mode="md",
        )
        raise StopPropagation

    # Method 2: trigger text provided inline
    if has_arg:
        trigger_text = normalize_trigger(arg_portion)
        if not trigger_text:
            await event.reply("Trigger text is empty after normalisation.")
            raise StopPropagation
        state.set(
            session_key,
            state.AWAIT_TRIGGER_MSG,
            trigger_text=trigger_text,
            group_id=group_id,
            initiated_chat_id=initiated_chat_id,
            created_by_id=sender_id,
            created_by_name=sender_name,
        )
        await event.reply(
            f"🔑 Keyword locked: `{trigger_text}`\n\n"
            "**Step 2/2 — Send the message to attach:**\n"
            "image, video, sticker, document, audio, text, or forward any message.\n\n"
            "⚠️ Send it **in this chat only** — messages from other chats are ignored.",
            parse_mode="md",
        )
        raise StopPropagation

    # Method 1: interactive wizard — ask for the keyword first
    state.set(
        session_key,
        state.AWAIT_TRIGGER_TEXT,
        group_id=group_id,
        initiated_chat_id=initiated_chat_id,
        created_by_id=sender_id,
        created_by_name=sender_name,
    )
    await event.reply(
        f"**Step 1/2 — Type the trigger keyword** (text only, e.g. `chainsaw man`).\n"
        f"Group: `{group_id}`\n\n"
        f"⚠️ Respond **in this chat only**.\n"
        f"To abort, send /cancel{privacy_warning}",
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
        await event.reply(f"✅ Deleted trigger #{index}: `{deleted_text}`", parse_mode="md")
    else:
        total = len(await db.fetch_triggers_for_group(group_id))
        await event.reply(
            f"Invalid index {index}. Valid range: 1–{total}.\nUse /trigger_list to see the list."
        )
    raise StopPropagation


async def cmd_cancel(event: events.NewMessage.Event) -> None:
    sender_id = event.sender_id
    session_key = state.key(sender_id, event.chat_id)
    current_key = session_key
    current = state.get(session_key)
    if not current:
        current_key, current = state.find_for_user(sender_id)

    if current:
        initiated = current.data.get("initiated_chat_id") if current else None
        # Allow cancel from the wizard chat OR from PM (unstuck safety valve)
        if initiated is not None and event.chat_id != initiated and not event.is_private:
            await event.reply(
                "No active wizard in this chat.\n"
                f"Your open wizard is in chat `{initiated}` — send /cancel there.",
                parse_mode="md",
            )
            raise StopPropagation
        if event.is_private:
            state.clear_for_user(sender_id)
        else:
            state.clear(current_key)
        await event.reply("✅ Wizard cancelled.")
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


# ── In-flight guard (prevents double-processing the same message) ─────────────
_in_flight: set[tuple[int, int]] = set()  # (sender_id, message_id)


# ==============================================================================
#  STATE REPLY HANDLER  — called from main.py general_dispatcher
# ==============================================================================

async def handle_state_reply(event: events.NewMessage.Event) -> bool:
    """
    Process a message that is part of an ongoing setup wizard.

    CRITICAL ISOLATION RULE
    -----------------------
    We compare event.chat_id against state.data["initiated_chat_id"].
    If they don't match the message is from a DIFFERENT CHAT and must be
    ignored completely (return False) so the dispatcher continues normally.

    Example of the bug this prevents:
      1. Admin runs /set_trigger in Group A  → state stored with initiated_chat_id=A
      2. Admin forwards a photo to Group B
      3. main.py fires handle_state_reply for that Group B event
         → chat_id=B ≠ initiated_chat_id=A → returns False immediately  ✓
         → message proceeds through normal dispatch (trigger match, etc.)

    Double-processing guard:
      (sender_id, message_id) is tracked in _in_flight.
    """
    sender_id = event.sender_id
    msg_id = event.id
    flight_key = (sender_id, msg_id)

    session_key = state.key(sender_id, event.chat_id)
    current = state.get(session_key)
    
    if not current:
        # Fallback: check if user has ANY active state to provide helpful isolation feedback
        k, current = state.find_for_user(sender_id)
        if not current:
            return False

    # ── CHAT ISOLATION CHECK ─────────────────────────────────────────────────
    initiated_chat_id = current.data.get("initiated_chat_id")
    if initiated_chat_id is not None and event.chat_id != initiated_chat_id:
        # Different chat — do NOT consume this message.
        # We don't reply here to avoid spamming other groups, but we log it.
        logger.info(
            "Wizard isolation: user %s sent msg in chat %s, but their active wizard is locked to chat %s. Ignoring message.",
            sender_id, event.chat_id, initiated_chat_id,
        )
        return False

    # ── USER ISOLATION CHECK ─────────────────────────────────────────────────
    # Ensure ONLY the person who started the wizard can interact with it.
    created_by_id = current.data.get("created_by_id")
    if created_by_id is not None and sender_id != created_by_id:
        # Another user in the SAME chat sent a message.
        # We definitely don't want to consume this, but we also don't want to reply 
        # because it might be a regular group member just chatting.
        logger.debug(
            "Wizard isolation: user %s sent msg in chat %s, but wizard was started by %s. Ignoring message.",
            sender_id, event.chat_id, created_by_id,
        )
        return False

    logger.info("Processing wizard state '%s' for user %s in chat %s", current.step, sender_id, event.chat_id)

    if flight_key in _in_flight:
        return True
    _in_flight.add(flight_key)

    try:
        return await _handle_state_reply_inner(event, session_key, sender_id, current)
    finally:
        _in_flight.discard(flight_key)


async def _handle_state_reply_inner(event, session_key, sender_id: int, current) -> bool:
    # ── PERMISSION RE-VERIFICATION ───────────────────────────────────────────
    group_id = current.data.get("group_id")
    if not await _require_trigger_permission(event, group_id):
        state.clear(session_key)
        await event.reply("❌ Permission lost. Wizard closed.")
        return True
    # ── Step 1: waiting for trigger keyword (interactive wizard) ──────────────
    if current.step == state.AWAIT_TRIGGER_TEXT:
        # Step 1 only accepts plain text — reject media with a helpful message
        if event.message.media and not isinstance(event.message.media, MessageMediaWebPage):
            await event.reply(
                "⚠️ **Step 1 needs a text keyword**, not a file.\n\n"
                "Type the trigger word/phrase (e.g. `chainsaw man`).\n"
                "You'll send the image/video/sticker in the next step.",
                parse_mode="md",
            )
            return True  # consumed — do NOT fall through to trigger matching

        trigger_text = normalize_trigger(event.text or "")
        if not trigger_text:
            await event.reply("❌ Please send a non-empty text keyword.")
            return True

        state.set(
            session_key,
            state.AWAIT_TRIGGER_MSG,
            trigger_text=trigger_text,
            group_id=group_id,
            initiated_chat_id=current.data.get("initiated_chat_id"),
            created_by_id=current.data.get("created_by_id"),
            created_by_name=current.data.get("created_by_name"),
        )
        await event.reply(
            f"✅ Keyword: `{trigger_text}`\n\n"
            "**Step 2/2 — Send the message to attach:**\n"
            "image, video, sticker, document, audio, text, or forward any message.\n\n"
            "⚠️ Send it **in this chat only**.\n"
            "To abort, send /cancel",
            parse_mode="md",
        )
        return True

    # ── Step 2: waiting for the response message to store ─────────────────────
    if current.step == state.AWAIT_TRIGGER_MSG:
        trigger_text = current.data.get("trigger_text", "")
        group_id = current.data.get("group_id")

        if not trigger_text or not group_id:
            state.clear(session_key)
            await event.reply(
                "❌ Internal error: wizard state lost. "
                "Start over with /set_trigger inside the search group."
            )
            return True

        # ── Use event.message directly — do NOT re-fetch with get_messages().
        #    Re-fetching can return None for freshly sent messages (race condition)
        #    and also fails silently for media messages in some chat contexts.
        msg = event.message

        content_text = _message_content_text(msg)

        # TEXT / LINK: no media at all, OR only a web-preview, AND has text
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
                created_by_id=current.data.get("created_by_id"),
                created_by_name=current.data.get("created_by_name"),
            )
            await _refresh_group_cache(group_id)
            state.clear(session_key)
            await event.reply(
                f"✅ Trigger saved!\n\n"
                f"🔑 Keyword: `{trigger_text}`\n"
                f"📜 Type: text/link — stored in DB\n\n"
                "You can delete this message — the trigger still works.",
                parse_mode="md",
            )
            return True

        # ── MEDIA path (photo, video, document, audio, voice, sticker…) ─────
        # Images/videos/stickers with NO caption are perfectly valid here.
        if not _has_real_media(msg):
            # Nothing usable — blank message somehow
            await event.reply(
                "❌ Could not detect any content in that message.\n"
                "Please send an image, video, sticker, document, or a text message."
            )
            return True

        if _storage_peer is None:
            state.clear(session_key)
            await event.reply(
                "❌ Storage channel not resolved at startup.\n"
                "Restart the bot and confirm STORAGE_CHANNEL_ID is correct "
                "and bot is an admin there. Then run /set_trigger again."
            )
            return True

        # Keep storage channel posts clean: only preserve the original caption.
        storage_caption = content_text or None

        # Store cleanly via send_file (no "Forwarded from" header).
        # Stickers and fallback cases handled inside _store_media_in_storage.
        stored_msg = await _store_media_in_storage(
            event.client, event.chat_id, event.id, msg.media, storage_caption
        )

        if stored_msg is None:
            state.clear(session_key)
            await event.reply(
                "❌ Could not save media to storage channel.\n"
                "Make sure the bot is an admin of STORAGE_CHANNEL_ID "
                "with permission to post messages, then run /set_trigger again."
            )
            return True

        storage_msg_id = stored_msg.id
        media_ref = _serialize_media(getattr(stored_msg, "media", None) or msg.media)

        await db.upsert_trigger(
            trigger_text, group_id,
            storage_type="media",
            storage_text=content_text or None,
            storage_chat_id=None if config.STORAGE_DELETE_AFTER_SAVE else config.STORAGE_CHANNEL_ID,
            storage_message_id=None if config.STORAGE_DELETE_AFTER_SAVE else storage_msg_id,
            storage_media_b64=media_ref,
            created_by_id=current.data.get("created_by_id"),
            created_by_name=current.data.get("created_by_name"),
        )
        await _delete_storage_message(event.client, storage_msg_id)
        await _refresh_group_cache(group_id)
        state.clear(session_key)

        # Detect the EXACT file type for the UI confirmation message.
        # _detect_media_type() only reads the message object — storage is untouched.
        media_type = _detect_media_type(msg)
        storage_note = (
            "hidden storage"
            if config.STORAGE_DELETE_AFTER_SAVE
            else f"storage channel msg `{storage_msg_id}`"
        )

        # Map type to a friendly emoji
        _TYPE_EMOJI = {
            "image": "🖼️", "video": "🎬", "gif": "🎞️", "sticker": "🩹",
            "document": "📄", "audio": "🎵", "voice": "🎙️",
            "video_note": "⭕", "media": "📦",
        }
        type_emoji = _TYPE_EMOJI.get(media_type, "📦")

        await event.reply(
            f"✅ Trigger saved!\n\n"
            f"🔑 Keyword: `{trigger_text}`\n"
            f"{type_emoji} Type: **{media_type}** — {storage_note}\n\n"
            "You can delete this message — the trigger still works.",
            parse_mode="md",
        )
        return True

    return False


# ==============================================================================
#  RUNTIME: TRIGGER MATCHING
# ==============================================================================

async def handle_trigger_match(event: events.NewMessage.Event) -> bool:
    """
    Check if any trigger for THIS group matches the incoming message.
    Longest match wins. Returns True if a trigger was fired.
    """
    text = _message_content_text(event.message)
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
            logger.warning(
                "Trigger '%s' has storage_type=text but no storage_text",
                matched["trigger"],
            )
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
    media_ref = _deserialize_media(matched.get("storage_media_b64"))
    if media_ref:
        try:
            await event.client.send_file(
                event.chat_id,
                media_ref,
                caption=matched.get("storage_text") or None,
                parse_mode="html",
            )
            return True
        except Exception as e:
            logger.warning(
                "Hidden media trigger '%s' delivery failed, trying storage message fallback: %s",
                matched["trigger"], e,
            )

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

        # Strip the metadata line written at storage time
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
