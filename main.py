"""
main.py — Bot entry point.

Dispatcher priority for non-command messages:
  1. Active setup wizard state (admin only, any chat)
  2. Show: search  — only in registered search groups
  3. Trigger match — only in registered search groups (fires for ANYONE)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from telethon import TelegramClient, events
from telethon.events import StopPropagation
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import (
    ChannelParticipantsAdmins, 
    ChannelParticipantCreator, 
    ChatParticipantCreator
)
from keep_alive import start_server
import os
import cache
import config
import db
import state
import search
import triggers
from triggers import handle_state_reply, handle_trigger_match
from search import handle_show_search

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("motor").setLevel(logging.WARNING)

logger = logging.getLogger("bot.main")

client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)


def _event_text(event: events.NewMessage.Event) -> str:
    msg = getattr(event, "message", None)
    return getattr(event, "text", None) or getattr(msg, "message", None) or ""


# ══════════════════════════════════════════════════════════════════════════════
#  GENERAL DISPATCHER
# ══════════════════════════════════════════════════════════════════════════════

@client.on(events.NewMessage(incoming=True))
async def general_dispatcher(event: events.NewMessage.Event) -> None:
    """
    Route every non-command incoming message through the priority chain.
    Commands (/...) are handled by dedicated pattern-matched handlers.
    
    SCALABILITY NOTE:
    - Uses asyncio.create_task for parallel trigger processing
    - Each trigger match runs independently without blocking
    - Cache lookups are O(n) but scoped to each group
    - Supports 100+ groups through independent group_id isolation
    """
    text = _event_text(event)

    # Skip commands — handled by pattern handlers with StopPropagation
    if text.startswith("/"):
        return

    sender_id = event.sender_id

    # ── Priority 1: active setup wizard (admin state machine) ─────────────────
    # We check if this specific user has an active session in THIS chat, 
    # or if they have ANY session that might need handling (e.g. for isolation warnings).
    session_key = state.key(sender_id, event.chat_id)
    has_session = state.has(session_key)
    
    if not has_session:
        # Fallback: Does the user have a session in ANY chat?
        # find_for_user returns (key, state)
        k, s = state.find_for_user(sender_id)
        if s:
            has_session = True

    if has_session:
        logger.debug("Dispatcher: Found session for user %s in chat %s. Calling handle_state_reply.", sender_id, event.chat_id)
        consumed = await handle_state_reply(event)
        if consumed:
            return
    else:
        logger.debug("Dispatcher: No session found for user %s in chat %s.", sender_id, event.chat_id)

    # ── Priority 2 & 3: only in registered search groups ──────────────────────
    # DATA LEAK PREVENTION: Each group is isolated by event.chat_id
    # No group can access another group's data
    if not event.is_private:
        if not await db.is_search_group(event.chat_id):
            return  # Bot is in this chat but it's not a registered search group

    # ── Priority 2: Show: search (Strictly restricted to .env admins) ─────────
    if text.lower().startswith("show:"):
        if event.sender_id in config.ADMIN_IDS:
            await handle_show_search(event)
        return

    # ── Priority 3: trigger match (Public Product - Parallelised for Scale) ──
    # Fires for ANYONE in search groups. Using create_task for parallelism.
    # DATA SAFETY: trigger is matched ONLY for event.chat_id - no cross-group data leak
    asyncio.create_task(handle_trigger_match(event))


# ══════════════════════════════════════════════════════════════════════════════
#  /start and /help
# ══════════════════════════════════════════════════════════════════════════════

@client.on(events.NewMessage(pattern=r"^/start(?:\s|$)", incoming=True))
async def cmd_start(event: events.NewMessage.Event) -> None:
    await event.reply("👋 **Bot is running!** Send /help for all commands.", parse_mode="md")
    raise StopPropagation


@client.on(events.NewMessage(pattern=r"^/help(?:\s|$)", incoming=True))
async def cmd_help(event: events.NewMessage.Event) -> None:
    """
    Role-based dynamic help menu.
    - Env Admins (ADMIN_IDS): See all commands.
    - Group Admins/Adder: See management commands.
    - Regular Users: See only basic info.
    """
    is_auth_admin = event.sender_id in config.ADMIN_IDS
    from helpers import is_admin
    is_group_mgr = await is_admin(event) if not event.is_private else False

    text = "📖 **Bot Help Menu**\n\n"

    if is_auth_admin:
        text += "👑 **Authorized Bot Administrator Commands**\n"
        text += "`/list_search_groups` — list all active groups\n"
        text += "`/activate_triggers` — register a new group\n"
        text += "`/refresh_admins` — force permission sync\n"
        text += "`/bot_admins` — view management list\n\n"

    if is_group_mgr or is_auth_admin:
        text += "🛠️ **Group Management**\n"
        text += "`/set_trigger` — start trigger wizard\n"
        text += "`/trigger_list` — list current triggers\n"
        text += "`/delete <index>` — remove a trigger\n"
        text += "`/add_bot_admin <id/user>` — add group-level admin\n"
        text += "`/rem_bot_admin <id/user>` — remove group-level admin\n"
        text += "`/cancel` — abort active wizard\n"
        text += "`/refresh` — sync trigger cache\n\n"

    if not is_auth_admin and not is_group_mgr:
        text += "ℹ️ This bot provides automated triggers for this group.\n"
        text += "If you are an admin and need help setting it up, contact an **Authorized Bot Administrator**.\n"
    else:
        text += "💡 _Note: Some commands only work inside a group where you have permissions._"

    await event.reply(text, parse_mode="md")
    raise StopPropagation


# ══════════════════════════════════════════════════════════════════════════════
#  PLAN B: BOT ADMIN MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@client.on(events.ChatAction)
async def handler_bot_added(event: events.ChatAction.Event) -> None:
    """Detect when the bot is added to a group and record the 'Adder'."""
    if event.user_added:
        me = await client.get_me()
        if event.user_id == me.id:
            chat_id = event.chat_id
            # Identify who added the bot
            # Safety check: action_message might be None
            msg = getattr(event, "action_message", None)
            adder_id = getattr(msg, "sender_id", None) if msg else None
            
            if not adder_id:
                logger.warning("Could not identify adder for group %s", chat_id)
                return

            # Store in DB
            await db.upsert_group_authority(chat_id, adder_id)
            logger.info("Bot added to group %s by user %s", chat_id, adder_id)


async def _require_adder_or_manager(event) -> bool:
    """Helper to restrict commands to the Adder or Authorized Admins."""
    sender_id = event.sender_id
    if sender_id in config.ADMIN_IDS:
        return True
    
    chat_id = event.chat_id
    adder_id = await db.get_group_adder(chat_id)
    if sender_id == adder_id:
        return True
    
    await event.reply("❌ Only the **Group Adder** or an Authorized Bot Administrator can manage bot admins.")
    return False


@client.on(events.NewMessage(pattern=r"^/bot_admins(?:\s|$)", incoming=True))
async def cmd_list_bot_admins(event: events.NewMessage.Event) -> None:
    if event.is_private:
        return
    
    auth = await db.get_group_authority(event.chat_id)
    if not auth:
        await event.reply("⚠️ No bot admin record found. Try adding the bot again or use `/refresh_admins`.")
        return

    adder_id = auth.get("adder_id")
    allowed = auth.get("allowed_ids", [])
    banned = auth.get("banned_ids", [])

    text = f"🛡️ **Bot Management: {event.chat.title}**\n\n"
    text += f"👑 **Adder:** `{adder_id}`\n"
    
    if allowed:
        text += "\n✅ **Allowed Bot Admins:**\n"
        for uid in allowed:
            text += f"• `{uid}`\n"
    
    if banned:
        text += "\n🚫 **Explicitly Banned:**\n"
        for uid in banned:
            text += f"• `{uid}`\n"
    
    if not allowed and not banned:
        text += "\n_No custom permissions set. Standard Telegram admins (unbanned) are used._"

    await event.reply(text, parse_mode="md")
    raise StopPropagation


@client.on(events.NewMessage(pattern=r"^/add_bot_admin(?:\s+)(.+)", incoming=True))
async def cmd_add_bot_admin(event: events.NewMessage.Event) -> None:
    if event.is_private or not await _require_adder_or_manager(event):
        return
    
    input_str = event.pattern_match.group(1).strip().lstrip("@")
    try:
        # Resolve username or ID
        if input_str.isdigit() or (input_str.startswith("-") and input_str[1:].isdigit()):
            user = await event.client.get_entity(int(input_str))
        else:
            user = await event.client.get_entity(input_str)
        user_id = user.id
    except Exception as e:
        await event.reply(f"❌ Could not resolve user `{input_str}`: {e}")
        return

    if await db.add_bot_admin(event.chat_id, user_id):
        await event.reply(f"✅ User `{user_id}` (@{getattr(user, 'username', 'N/A')}) is now a **Bot Admin** for this group.")
    else:
        await event.reply(f"ℹ️ User `{user_id}` was already an allowed bot admin.")
    raise StopPropagation


@client.on(events.NewMessage(pattern=r"^/rem_bot_admin(?:\s+)(.+)", incoming=True))
async def cmd_rem_bot_admin(event: events.NewMessage.Event) -> None:
    if event.is_private or not await _require_adder_or_manager(event):
        return
    
    input_str = event.pattern_match.group(1).strip().lstrip("@")
    try:
        if input_str.isdigit() or (input_str.startswith("-") and input_str[1:].isdigit()):
            user = await event.client.get_entity(int(input_str))
        else:
            user = await event.client.get_entity(input_str)
        user_id = user.id
    except Exception as e:
        await event.reply(f"❌ Could not resolve user `{input_str}`: {e}")
        return

    # Prevent removing the Adder or Authorized Bot Administrators
    adder_id = await db.get_group_adder(event.chat_id)
    if user_id == adder_id or user_id in config.ADMIN_IDS:
        await event.reply("❌ Cannot remove the Adder or an Authorized Bot Administrator.")
        return

    if await db.remove_bot_admin(event.chat_id, user_id):
        await event.reply(f"🚫 User `{user_id}` (@{getattr(user, 'username', 'N/A')}) has been **removed** from bot management in this group.")
    else:
        await event.reply(f"ℹ️ User `{user_id}` was already removed/banned.")
    raise StopPropagation


@client.on(events.NewMessage(pattern=r"^/refresh_admins(?:\s|$)", incoming=True))
async def cmd_refresh_admins(event: events.NewMessage.Event) -> None:
    """
    Force a re-detection of the 'Adder' and sync permissions.
    Available to Telegram Admins (if Adder is missing) or Authorized Bot Administrators (always).
    """
    if event.is_private: return
    
    from helpers import is_user_admin
    is_tg_admin = await is_user_admin(event)
    is_auth_admin = event.sender_id in config.ADMIN_IDS

    if not is_tg_admin and not is_auth_admin:
        await event.reply("❌ Only a Telegram Admin or an Authorized Bot Administrator can use `/refresh_admins`.")
        return

    chat_id = event.chat_id
    adder_id = await db.get_group_adder(chat_id)
    
    # Authorized admins can ALWAYS refresh. 
    # Telegram admins can only refresh if the Adder is missing or they ARE the adder.
    if not adder_id or is_auth_admin:
        try:
            # Creator/Adder detection
            found_creator_id = None
            
            # 1. Try to find the creator among participants (works for most groups)
            try:
                # iter_participants with filter=Admins works for Channels/Supergroups
                async for p in event.client.iter_participants(chat_id, filter=ChannelParticipantsAdmins):
                    if isinstance(getattr(p, 'participant', None), (ChannelParticipantCreator, ChatParticipantCreator)):
                        found_creator_id = p.id
                        break
            except Exception:
                pass

# 2. Fallback for basic groups: check the chat object itself
            if not found_creator_id:
                try:
                    full_chat = await client(GetFullChatRequest(chat_id)) if not event.is_channel else None
                    if full_chat and hasattr(full_chat, 'full_chat') and hasattr(full_chat.full_chat, 'participants'):
                        # Telethon full_chat structure check
                        if hasattr(full_chat.full_chat.participants, 'participants'):
                            for p in full_chat.full_chat.participants.participants:
                                if isinstance(p, ChatParticipantCreator):
                                    found_creator_id = p.user_id
                                    break
                except Exception:
                    pass

            # 3. Final fallback: if creator not found, use the person who ran the command
            final_adder_id = found_creator_id or event.sender_id
            
            # Sync Telegram Admins to Bot Admins (excluding banned ones)
            auth = await db.get_group_authority(chat_id)
            banned_ids = auth.get("banned_ids", []) if auth else []
            
            tg_admins = []
            try:
                async for p in event.client.iter_participants(chat_id, filter=ChannelParticipantsAdmins):
                    if p.id not in banned_ids:
                        tg_admins.append(p.id)
            except Exception:
                # Fallback for basic groups (iter_participants filter=Admins might fail)
                participants = await event.client.get_participants(chat_id)
                for p in participants:
                    perms = await event.client.get_permissions(chat_id, p.id)
                    if (perms.is_admin or perms.is_creator) and p.id not in banned_ids:
                        tg_admins.append(p.id)
            
            await db.upsert_group_authority(chat_id, final_adder_id, allowed_ids=tg_admins)
            
            await event.reply(
                f"✅ Success! **{final_adder_id}** is now recorded as the **Bot Adder**.\n"
                f"🔄 Admin list synced: **{len(tg_admins)}** Telegram admins added to Bot Admins (excluding manually removed ones).\n\n"
                f"_Note: The Adder now has primary control._"
            )
            logger.info("Admin refresh successful for group %s. New Adder: %s, Admins: %s", chat_id, final_adder_id, len(tg_admins))
        except Exception as e:
            logger.error("Refresh admins failed: %s", e, exc_info=True)
            await event.reply(f"❌ Failed to refresh: {e}")
    else:
        await event.reply(
            f"ℹ️ Bot Adder is already recorded as `{adder_id}`.\n\n"
            "If you need to change this, please ask an **Authorized Bot Administrator** to run `/refresh_admins` here."
        )
    
    raise StopPropagation


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════════════════════

async def startup() -> None:
    logger.info("Connecting to MongoDB…")
    await db.setup_indexes()
    await db.migrate()
    await db.cleanup_invalid_ids()

    logger.info("Warming per-group trigger cache…")
    await cache.warm(db.fetch_all_triggers)

    logger.info("Starting Telegram client…")
    await client.start(bot_token=config.BOT_TOKEN)

    me = await client.get_me()
    logger.info("Logged in as @%s (id=%s)", me.username, me.id)

    logger.info("Resolving storage channel…")
    ok = await triggers.resolve_storage_peer(client)
    if not ok:
        logger.warning(
            "Storage channel could not be resolved. "
            "Media triggers will fail until this is fixed and bot is restarted."
        )
    else:
        await triggers.migrate_storage_to_hidden(client)

    triggers.register(client)
    search.register(client)

    logger.info("Bot ready.")


async def main() -> None:
    """
    Main entry point with auto-reconnect on connection issues.
    """
    while True:
        try:
            await startup()
            logger.info("Bot started successfully, waiting for events...")
            await client.run_until_disconnected()
        except Exception as e:
            logger.warning("Connection lost: %s. Reconnecting in 5 seconds...", e)
            import asyncio
            await asyncio.sleep(5)
            # Loop will reconnect


def _handle_signal(sig, frame):
    """Handle shutdown signals gracefully."""
    logger.info("Received signal %s — shutting down…", sig)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

if __name__ == "__main__":
    start_server(port=int(os.environ.get("PORT", 10000)))
    asyncio.run(main())
