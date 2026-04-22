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


# ══════════════════════════════════════════════════════════════════════════════
#  GENERAL DISPATCHER
# ══════════════════════════════════════════════════════════════════════════════

@client.on(events.NewMessage(incoming=True))
async def general_dispatcher(event: events.NewMessage.Event) -> None:
    """
    Route every non-command incoming message through the priority chain.
    Commands (/...) are handled by dedicated pattern-matched handlers.
    """
    text = event.text or event.caption or ""

    # Skip commands — handled by pattern handlers with StopPropagation
    if text.startswith("/"):
        return

    sender_id = event.sender_id

    # ── Priority 1: active setup wizard (admin state machine) ─────────────────
    # Works in any chat — wizard follow-up can arrive from anywhere.
    if state.has(sender_id):
        consumed = await handle_state_reply(event)
        if consumed:
            return

    # ── Priority 2 & 3: only in registered search groups ──────────────────────
    if not event.is_private:
        if not await db.is_search_group(event.chat_id):
            return  # Bot is in this chat but it's not a registered search group

    # ── Priority 2: Show: search ───────────────────────────────────────────────
    if text.lower().startswith("show:"):
        await handle_show_search(event)
        return

    # ── Priority 3: trigger match (fires for ANYONE in search groups) ──────────
    await handle_trigger_match(event)


# ══════════════════════════════════════════════════════════════════════════════
#  /start and /help
# ══════════════════════════════════════════════════════════════════════════════

@client.on(events.NewMessage(pattern=r"^/start(?:\s|$)", incoming=True))
async def cmd_start(event: events.NewMessage.Event) -> None:
    await event.reply("👋 **Bot is running!** Send /help for all commands.", parse_mode="md")
    raise StopPropagation


@client.on(events.NewMessage(pattern=r"^/help(?:\s|$)", incoming=True))
async def cmd_help(event: events.NewMessage.Event) -> None:
    await event.reply(
        "📖 **Commands**\n\n"

        "**── Registration (admin) ──**\n"
        "`/add_mainchannel <id|@user>`      — add & index a main channel\n"
        "`/add_channel_search <id|@user>`   — register a search group\n"
        "`/connect_channel <group> <ch>`    — link search group → main channel\n"
        "`/disconnect_channel <group> <ch>` — unlink a specific pair\n"
        "`/connect_as main|search`          — shortcut (run INSIDE target chat)\n"
        "`/disconnect`                      — remove THIS chat's connection\n\n"

        "**── Listing (admin) ──**\n"
        "`/list_main`              — all main channels\n"
        "`/list_search_groups`     — all search groups\n"
        "`/list_connections [grp]` — show search group → main channel mappings\n"
        "`/channel_stats`          — indexed post counts\n"
        "`/reindex <id|@user>`     — rebuild index for a main channel\n\n"

        "**── Trigger System (admin, inside a search group) ──**\n"
        "`/set_trigger`               — interactive wizard\n"
        "`/set_trigger <text>`        — direct (bot asks for message)\n"
        "`/set_trigger` _(reply)_     — use replied message's first word\n"
        "`/trigger_list`              — triggers for THIS group\n"
        "`/delete <n>`                — delete trigger by index\n"
        "`/cancel`                    — abort wizard\n\n"

        "**── Show Search (anyone, in a search group) ──**\n"
        "`Show: <show name>`   — search across connected main channels",
        parse_mode="md",
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

    triggers.register(client)
    search.register(client)

    logger.info("Bot ready.")


async def main() -> None:
    await startup()
    await client.run_until_disconnected()


def _handle_signal(sig, frame):
    logger.info("Received signal %s — shutting down…", sig)
    asyncio.get_event_loop().stop()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

if __name__ == "__main__":
    asyncio.run(main())
