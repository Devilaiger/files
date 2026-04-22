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

import cache
import config
import db
import state
from helpers import (
    build_trigger_list_text,
    forward_or_copy,
    normalize_trigger,
)

logger = logging.getLogger(__name__)


# ── Cache helper ───────────────────────────────────────────────────────────────

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


# ── Permission helpers ─────────────────────────────────────────────────────────

async def _resolve_group_id(event) -> tuple:
    """
    Determine which group_id a trigger command targets.

    Inside a group  -> group_id = event.chat_id (user-supplied args ignored).
    From PM         -> only env ADMIN_IDS allowed; group_id must be first arg.

    Returns (group_id, error_message). error_message is None on success.
    """
    if not event.is_private:
        return event.chat_id, None

    # PM path: only env ADMIN_IDS
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
    """
    Permission rules:
      env ADMIN_IDS        -> always allowed (any group)
      Telegram group admin -> allowed only for their own group, from inside it
      Normal user          -> denied
    """
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
    """Verify group_id is registered. Sends error reply and returns False if not."""
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
    print(f"[DEBUG] /set_trigger  sender={sender_id}  group={group_id}  is_private={event.is_private}")

    # Strip "/set_trigger" and (if PM) the group_id token to get the actual arg
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
            f"Trigger: {trigger_text}\n\nNow send the message to replay when this fires.",
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
            f"Trigger: `{trigger_text}`\n\nNow send the message to attach.",
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
        # /delete <group_id> <index>
        if len(raw_parts) < 3 or not raw_parts[2].isdigit():
            await event.reply("Usage from PM: /delete <group_id> <index>")
            raise StopPropagation
        index = int(raw_parts[2])
    else:
        # /delete <index>
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


# ==============================================================================
#  STATE REPLY HANDLER
# ==============================================================================

async def handle_state_reply(event: events.NewMessage.Event) -> bool:
    """
    Process a message that is part of an ongoing setup wizard.
    group_id is always read from FSM state (captured when wizard started),
    so the trigger is attributed to the correct group even when the follow-up
    arrives from PM (env admin flow).
    Returns True if message was consumed.
    """
    sender_id = event.sender_id
    current = state.get(sender_id)
    if not current:
        return False

    # Step 1: waiting for trigger text (Method 1)
    if current.step == state.AWAIT_TRIGGER_TEXT:
        trigger_text = normalize_trigger(event.text or "")
        if not trigger_text:
            await event.reply("Please send a non-empty trigger text.")
            return True
        state.set(
            sender_id,
            state.AWAIT_TRIGGER_MSG,
            trigger_text=trigger_text,
            group_id=current.data.get("group_id"),
        )
        await event.reply(
            f"Trigger: `{trigger_text}`\n\nNow send the message to attach.",
            parse_mode="md",
        )
        return True

    # Step 2: waiting for the message to store
    if current.step == state.AWAIT_TRIGGER_MSG:
        trigger_text = current.data.get("trigger_text", "")
        group_id = current.data.get("group_id")

        if not trigger_text or not group_id:
            state.clear(sender_id)
            await event.reply(
                "Internal error: state data lost. "
                "Start over with /set_trigger inside the search group."
            )
            return True

        source_chat_id = event.chat_id
        source_msg_id = event.id

        await db.upsert_trigger(trigger_text, group_id, source_chat_id, source_msg_id)
        await _refresh_group_cache(group_id)
        state.clear(sender_id)

        await event.reply(
            f"Trigger saved.\n\n"
            f"Keyword: {trigger_text}\n"
            f"Group: {group_id}\n"
            f"Message ID: {source_msg_id} in chat {source_chat_id}"
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
    text = event.text or event.caption or ""
    if not text:
        return False

    group_id = event.chat_id
    matched = cache.find_match(group_id, text)
    if not matched:
        return False

    logger.info(
        "Trigger '%s' matched in group %s (msg %s)",
        matched["trigger"],
        group_id,
        event.id,
    )

    success = await forward_or_copy(
        event.client,
        matched["source_chat_id"],
        matched["source_message_id"],
        event.chat_id,
    )

    if not success:
        logger.warning(
            "Could not resend trigger response for '%s' — source message may be deleted.",
            matched["trigger"],
        )

    return True


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
        cb_trigger_page,
        events.CallbackQuery(pattern=rb"^tpage:"),
    )
    logger.info("Trigger handlers registered.")
