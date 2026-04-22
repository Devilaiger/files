"""
search.py - Feature 2: Show Search + Channel/Group Connection System

HOW IT WORKS
------------
1. Admin adds bot to a main channel and a search group.
2. Register each separately:
     /add_channel_search      - run INSIDE the group (uses event.chat_id always)
     /add_mainchannel @ch     - register a channel as content source and index it
3. Link them from inside the search group:
     /connect_channel @ch     - run INSIDE the search group (uses event.chat_id)
4. Anyone in a registered search group can use:
     Show: <name>             - searches ONLY connected main channels for that group

ID RULE: Groups are ALWAYS identified by event.chat_id at runtime.
         No user-supplied group ID is ever used for group operations.
         Channel IDs resolved via Telethon are always in -100XXXXXXXXXX format.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from rapidfuzz import fuzz
from rapidfuzz import process as fuzz_process
from telethon import TelegramClient, events
from telethon.errors import ChannelPrivateError, FloodWaitError
from telethon.events import StopPropagation
from telethon.tl.types import Message

import config
import db
from helpers import forward_or_copy, is_admin, normalize_text, resolve_channel

logger = logging.getLogger(__name__)

_SHOW_PREFIX = "show:"


# ── Admin check in a specific chat ────────────────────────────────────────────

async def _is_admin_in_chat(client: TelegramClient, chat_id: int, user_id: int) -> bool:
    if user_id in config.ADMIN_IDS:
        return True
    try:
        perms = await client.get_permissions(chat_id, user_id)
        return perms.is_admin or perms.is_creator
    except Exception:
        return False


# ==============================================================================
#  REGISTRATION COMMANDS
# ==============================================================================

async def cmd_add_mainchannel(event: events.NewMessage.Event) -> None:
    """
    /add_mainchannel <id|@username>
    Registers a channel as a main content source and immediately indexes it.
    """
    if not await is_admin(event):
        await event.reply("Admin only.")
        raise StopPropagation

    parts = event.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("Usage: /add_mainchannel <channel_id or @username>")
        raise StopPropagation

    identifier = parts[1].strip()
    resolved = await resolve_channel(event.client, identifier)
    if resolved is None:
        await event.reply(
            f"Cannot access {identifier}. Make sure the bot is a member/admin of that channel."
        )
        raise StopPropagation

    ch_id, username, title = resolved
    print(f"[DEBUG] /add_mainchannel  resolved channel_id={ch_id}  title={title}")

    if await db.is_main_channel(ch_id):
        await event.reply(f"{title} is already a main channel.")
        raise StopPropagation

    await db.add_main_channel(ch_id, username, title, event.sender_id)
    status_msg = await event.reply(f"{title} added as main channel. Indexing posts now...")
    indexed = await _index_channel(event.client, ch_id, status_msg)
    await status_msg.edit(f"{title} is now a main channel. Indexed {indexed} posts.")
    raise StopPropagation


async def cmd_add_channel_search(event: events.NewMessage.Event) -> None:
    """
    /add_channel_search
    Registers THIS group as a search group.
    MUST be run inside the target group. group_id = event.chat_id always.
    """
    if event.is_private:
        await event.reply(
            "Run /add_channel_search inside the group you want to register. "
            "The bot will use that group ID automatically."
        )
        raise StopPropagation

    if not await is_admin(event):
        await event.reply("Admin only.")
        raise StopPropagation

    group_id = event.chat_id
    print(f"[DEBUG] /add_channel_search  event.chat_id={group_id}")

    chat = await event.get_chat()
    title = getattr(chat, "title", str(group_id))

    if await db.is_search_group(group_id):
        await event.reply(f"{title} is already a search group.")
        raise StopPropagation

    await db.add_search_group(group_id, title, event.sender_id)
    await event.reply(
        f"{title} registered as a search group.\n\n"
        f"Next: link it to a main channel with:\n"
        f"/connect_channel @channel_username"
    )
    raise StopPropagation


async def cmd_connect_channel(event: events.NewMessage.Event) -> None:
    """
    /connect_channel <main_channel>
    Run INSIDE the search group. group_id = event.chat_id always.
    Only the main channel identifier is taken from the argument.
    """
    if event.is_private:
        await event.reply(
            "Run /connect_channel <main_channel> inside the search group. "
            "The bot will use that group ID automatically."
        )
        raise StopPropagation

    if not await is_admin(event):
        await event.reply("Admin only.")
        raise StopPropagation

    parts = event.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await event.reply(
            "Usage: /connect_channel <main_channel>\n"
            "Run this command inside the search group.\n"
            "Example: /connect_channel @Seoul_Entertainment_DKD"
        )
        raise StopPropagation

    search_id = event.chat_id
    sender_id = event.sender_id
    main_ident = parts[1].strip()

    print(f"[DEBUG] /connect_channel  search_group={search_id}  main_ident={main_ident}")

    if not await db.is_search_group(search_id):
        await event.reply(
            "This group is not registered as a search group yet. "
            "Run /add_channel_search here first."
        )
        raise StopPropagation

    main_resolved = await resolve_channel(event.client, main_ident)
    if main_resolved is None:
        await event.reply(
            f"Cannot access {main_ident}. Make sure the bot is a member/admin of that channel."
        )
        raise StopPropagation

    main_id, _, main_title = main_resolved
    print(f"[DEBUG] /connect_channel  resolved main_channel={main_id}  title={main_title}")

    if not await db.is_main_channel(main_id):
        await event.reply(
            f"{main_title} is not registered as a main channel. "
            f"Run /add_mainchannel {main_ident} first to index its posts."
        )
        raise StopPropagation

    in_main = await _is_admin_in_chat(event.client, main_id, sender_id)
    if not in_main:
        await event.reply(f"You must be an admin in {main_title} to connect it.")
        raise StopPropagation

    if await db.has_mapping(search_id, main_id):
        await event.reply(f"This group is already connected to {main_title}.")
        raise StopPropagation

    await db.add_channel_mapping(search_id, main_id, sender_id)
    chat = await event.get_chat()
    search_title = getattr(chat, "title", str(search_id))
    await event.reply(
        f"Connected!\n\n"
        f"Search group: {search_title}\n"
        f"Main channel: {main_title}\n\n"
        f"Users here can now use Show: <name> to search."
    )
    raise StopPropagation


async def cmd_disconnect_channel(event: events.NewMessage.Event) -> None:
    """
    /disconnect_channel <main_channel>
    Run INSIDE the search group to remove a specific main-channel link.
    """
    if event.is_private:
        await event.reply(
            "Run /disconnect_channel <main_channel> inside the search group."
        )
        raise StopPropagation

    if not await is_admin(event):
        await event.reply("Admin only.")
        raise StopPropagation

    parts = event.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("Usage: /disconnect_channel <main_channel>")
        raise StopPropagation

    search_id = event.chat_id
    main_resolved = await resolve_channel(event.client, parts[1].strip())
    if main_resolved is None:
        await event.reply("Could not resolve that channel identifier.")
        raise StopPropagation

    main_id, _, main_title = main_resolved
    removed = await db.remove_channel_mapping(search_id, main_id)
    if removed:
        await event.reply(f"Disconnected this group from {main_title}.")
    else:
        await event.reply(f"No connection found between this group and {main_title}.")
    raise StopPropagation


# ==============================================================================
#  LEGACY SHORTCUT: /connect_as (run inside the target chat)
# ==============================================================================

async def cmd_connect_as(event: events.NewMessage.Event) -> None:
    """
    /connect_as main   - register THIS channel as a main channel
    /connect_as search - register THIS group as a search group
    Must be run INSIDE the target chat.
    """
    if event.is_private:
        await event.reply(
            "Run /connect_as main or /connect_as search inside the target group or channel."
        )
        raise StopPropagation

    if not await is_admin(event):
        await event.reply("You must be an admin of this chat to connect it.")
        raise StopPropagation

    parts = event.text.strip().split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip().lower() not in ("main", "search"):
        await event.reply(
            "Usage:\n"
            "/connect_as main   - index this channel posts\n"
            "/connect_as search - bot responds to triggers and Show: here"
        )
        raise StopPropagation

    mode = parts[1].strip().lower()
    chat = await event.get_chat()
    chat_id = event.chat_id
    title = getattr(chat, "title", str(chat_id))
    username = getattr(chat, "username", None)

    if mode == "main":
        await db.add_main_channel(chat_id, username, title, event.sender_id)
        status_msg = await event.reply(f"{title} connected as main channel. Indexing posts now...")
        indexed = await _index_channel(event.client, chat_id, status_msg)
        await status_msg.edit(f"{title} is now a main channel. Indexed {indexed} posts.")
    else:
        await db.add_search_group(chat_id, title, event.sender_id)
        await event.reply(
            f"{title} connected as search group.\n\n"
            f"Link it to a main channel with:\n"
            f"/connect_channel @channel_username\n\n"
            f"Then anyone here can use Show: <name> to search."
        )
    raise StopPropagation


async def cmd_disconnect(event: events.NewMessage.Event) -> None:
    """
    /disconnect - run INSIDE a connected chat to remove it.
    Also removes all channel mappings involving this chat.
    """
    if event.is_private:
        await event.reply("Run /disconnect inside the chat you want to remove.")
        raise StopPropagation

    if not await is_admin(event):
        await event.reply("You must be an admin of this chat to disconnect it.")
        raise StopPropagation

    chat_id = event.chat_id
    chat = await event.get_chat()
    title = getattr(chat, "title", str(chat_id))

    removed_main = await db.remove_main_channel(chat_id)
    removed_search = await db.remove_search_group(chat_id)

    if removed_main:
        wiped_posts = await db.delete_channel_posts(chat_id)
        wiped_mappings = await db.remove_all_mappings_for_main(chat_id)
        await event.reply(
            f"{title} disconnected as main channel. "
            f"Wiped {wiped_posts} indexed posts and {wiped_mappings} search-group link(s)."
        )
    elif removed_search:
        wiped_mappings = await db.remove_all_mappings_for_group(chat_id)
        await event.reply(
            f"{title} disconnected as search group. "
            f"Removed {wiped_mappings} main-channel link(s). "
            "Bot will no longer respond to triggers or Show: here."
        )
    else:
        await event.reply(
            "This chat is not connected. "
            "Use /connect_as main or /connect_as search to connect it first."
        )
    raise StopPropagation


# ==============================================================================
#  LISTING COMMANDS
# ==============================================================================

async def cmd_list_main(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        await event.reply("Admin only.")
        raise StopPropagation

    channels = await db.get_main_channels()
    if not channels:
        await event.reply("No main channels connected yet.")
        raise StopPropagation

    lines = ["Main Channels (content source for Show: search)\n"]
    for i, ch in enumerate(channels, 1):
        un = f"@{ch['username']}" if ch.get("username") else "private"
        count = await db.count_indexed_posts(ch["channel_id"])
        lines.append(f"{i}. {ch.get('title','?')} | {ch['channel_id']} ({un}) | {count} posts")

    await event.reply("\n".join(lines))
    raise StopPropagation


async def cmd_list_search_groups(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        await event.reply("Admin only.")
        raise StopPropagation

    groups = await db.get_search_groups()
    if not groups:
        await event.reply("No search groups connected yet.")
        raise StopPropagation

    lines = ["Search Groups (bot responds here)\n"]
    for i, g in enumerate(groups, 1):
        lines.append(f"{i}. {g.get('title','?')} | {g['group_id']}")

    await event.reply("\n".join(lines))
    raise StopPropagation


async def cmd_list_connections(event: events.NewMessage.Event) -> None:
    """
    /list_connections [search_group_id]
    Shows all search_group -> main_channel mappings.
    """
    if not await is_admin(event):
        await event.reply("Admin only.")
        raise StopPropagation

    parts = event.text.strip().split(maxsplit=1)
    filter_group: Optional[int] = None

    if len(parts) > 1:
        arg = parts[1].strip()
        if arg.lstrip("-").isdigit():
            filter_group = int(arg)
        else:
            resolved = await resolve_channel(event.client, arg)
            if resolved:
                filter_group = resolved[0]

    if filter_group:
        mapping_docs = await db.get_mappings_for_group(filter_group)
        if not mapping_docs:
            await event.reply(f"No connections for group {filter_group}.")
            raise StopPropagation
        lines = [f"Connections for group {filter_group}\n"]
        for m in mapping_docs:
            ch_doc = await db.get_main_channel_by_id(m["main_channel_id"])
            ch_name = ch_doc.get("title", str(m["main_channel_id"])) if ch_doc else str(m["main_channel_id"])
            lines.append(f"- {ch_name} ({m['main_channel_id']})")
        await event.reply("\n".join(lines))
        raise StopPropagation

    all_groups = await db.get_search_groups()
    if not all_groups:
        await event.reply("No search groups registered.")
        raise StopPropagation

    lines = ["All Channel Connections\n"]
    for g in all_groups:
        gid = g["group_id"]
        mapping_docs = await db.get_mappings_for_group(gid)
        connected = []
        for m in mapping_docs:
            ch_doc = await db.get_main_channel_by_id(m["main_channel_id"])
            ch_name = ch_doc.get("title", str(m["main_channel_id"])) if ch_doc else str(m["main_channel_id"])
            connected.append(ch_name)
        ch_list = ", ".join(connected) if connected else "(none)"
        lines.append(f"{g.get('title','?')} -> {ch_list}")

    await event.reply("\n".join(lines))
    raise StopPropagation


async def cmd_reindex(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        await event.reply("Admin only.")
        raise StopPropagation

    parts = event.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("Usage: /reindex <channel_id or @username>")
        raise StopPropagation

    identifier = parts[1].strip()
    resolved = await resolve_channel(event.client, identifier)
    if resolved is None:
        await event.reply(f"Cannot resolve {identifier}. Make sure bot is a member.")
        raise StopPropagation

    ch_id, _, title = resolved

    if not await db.is_main_channel(ch_id):
        await event.reply(f"{title} is not a main channel. Run /add_mainchannel for it first.")
        raise StopPropagation

    wiped = await db.delete_channel_posts(ch_id)
    status_msg = await event.reply(f"Wiped {wiped} old entries for {title}. Re-indexing...")
    indexed = await _index_channel(event.client, ch_id, status_msg)
    await status_msg.edit(f"Re-index done for {title}. Posts indexed: {indexed}")
    raise StopPropagation


async def cmd_channel_stats(event: events.NewMessage.Event) -> None:
    if not await is_admin(event):
        await event.reply("Admin only.")
        raise StopPropagation

    channels = await db.get_main_channels()
    if not channels:
        await event.reply("No main channels configured.")
        raise StopPropagation

    lines = ["Channel Index Stats\n"]
    for ch in channels:
        count = await db.count_indexed_posts(ch["channel_id"])
        lines.append(f"- {ch.get('title','?')} ({ch['channel_id']}): {count} posts")

    await event.reply("\n".join(lines))
    raise StopPropagation


# ==============================================================================
#  INDEXER
# ==============================================================================

async def _index_channel(client: TelegramClient, channel_id: int, status_msg=None) -> int:
    count = 0
    try:
        async for msg in client.iter_messages(channel_id, limit=config.INDEX_LIMIT):
            text = msg.text or msg.caption or ""
            if not text:
                continue
            normalized = normalize_text(text)
            if not normalized:
                continue
            await db.upsert_post(channel_id, msg.id, text, normalized)
            count += 1
            if status_msg and count % 500 == 0:
                try:
                    await status_msg.edit(f"Indexed {count} posts so far...")
                except Exception:
                    pass
    except ChannelPrivateError:
        logger.error("Channel %s is private - bot has no access.", channel_id)
    except FloodWaitError as e:
        logger.warning("FloodWait during indexing: %ds", e.seconds)
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.error("Indexing error for %s: %s", channel_id, e)

    logger.info("Indexed %d posts from channel %s", count, channel_id)
    return count


async def auto_index_new_post(message: Message, channel_id: int) -> None:
    """Auto-index new posts from main channels as they arrive."""
    text = message.text or message.caption or ""
    if not text:
        return
    normalized = normalize_text(text)
    if normalized:
        await db.upsert_post(channel_id, message.id, text, normalized)
        logger.debug("Auto-indexed post %s from main channel %s", message.id, channel_id)


# ==============================================================================
#  SEARCH LOGIC (scoped to connected main channels only)
# ==============================================================================

async def _do_search(query: str, search_group_id: int) -> list:
    """
    Search for query within the main channels connected to search_group_id ONLY.
    Returns [] immediately if the group has no connected main channels.
    """
    channel_ids = await db.get_main_channel_ids_for_group(search_group_id)
    print(f"[DEBUG] _do_search  group={search_group_id}  connected_channels={channel_ids}")
    if not channel_ids:
        return []

    normalized_query = normalize_text(query)

    # Phase 1: exact match within connected channels
    exact = await db.exact_search(normalized_query, channel_ids)
    if exact:
        return exact[: config.MAX_SEARCH_RESULTS]

    # Phase 2: fuzzy within connected channels
    all_posts = await db.get_posts_for_fuzzy(channel_ids)
    if not all_posts:
        return []

    choices = {p["normalized_text"]: p for p in all_posts}
    matches = fuzz_process.extract(
        normalized_query,
        list(choices.keys()),
        scorer=fuzz.token_sort_ratio,
        score_cutoff=config.FUZZY_THRESHOLD,
        limit=config.MAX_SEARCH_RESULTS,
    )
    return [choices[m[0]] for m in matches]


# ==============================================================================
#  SHOW: HANDLER
# ==============================================================================

async def handle_show_search(event: events.NewMessage.Event) -> bool:
    """
    Handle a Show: <name> message.
    Only active in registered search groups.
    Searches ONLY the main channels connected to that specific group.
    """
    text = (event.text or "").strip()
    if not text.lower().startswith(_SHOW_PREFIX):
        return False

    show_name = text[len(_SHOW_PREFIX):].strip()
    if not show_name:
        await event.reply("Usage: Show: <show name>")
        return True

    search_group_id = event.chat_id
    print(f"[DEBUG] Show: search  event.chat_id={search_group_id}  query={show_name!r}")

    channel_ids = await db.get_main_channel_ids_for_group(search_group_id)
    print(f"[DEBUG] Show: search  connected_channels={channel_ids}")

    if not channel_ids:
        await event.reply(
            "This group has no connected main channels. "
            "An admin must run /connect_channel to link a content source."
        )
        return True

    results = await _do_search(show_name, search_group_id)

    if not results:
        await event.reply(f'No results found for "{show_name}"')
        return True

    channel_cache: dict = {}
    links: list = []
    private_results: list = []

    for post in results:
        ch_id = post["channel_id"]
        if ch_id not in channel_cache:
            ch_doc = await db.get_main_channel_by_id(ch_id)
            channel_cache[ch_id] = ch_doc or {}
        ch_doc = channel_cache[ch_id]

        username = ch_doc.get("username")
        if username:
            title = ch_doc.get("title", username)
            links.append(f"- [{title}](https://t.me/{username}/{post['message_id']})")
        else:
            private_results.append(post)

    parts_out: list = [f'Results for "{show_name}":\n']
    if links:
        parts_out.append("\n".join(links))

    await event.reply("\n".join(parts_out), parse_mode="md", link_preview=False)

    for post in private_results:
        success = await forward_or_copy(
            event.client,
            post["channel_id"],
            post["message_id"],
            event.chat_id,
        )
        if not success:
            logger.warning(
                "Could not forward private post %s/%s",
                post["channel_id"],
                post["message_id"],
            )

    return True


# ==============================================================================
#  AUTO-INDEX new posts from main channels
# ==============================================================================

async def handle_new_channel_post(event: events.NewMessage.Event) -> None:
    ch_id = event.chat_id
    if not await db.is_main_channel(ch_id):
        return
    await auto_index_new_post(event.message, ch_id)


# ==============================================================================
#  REGISTRATION
# ==============================================================================

def register(client: TelegramClient) -> None:
    client.add_event_handler(
        cmd_add_mainchannel,
        events.NewMessage(pattern=r"^/add_mainchannel(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_add_channel_search,
        events.NewMessage(pattern=r"^/add_channel_search(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_connect_channel,
        events.NewMessage(pattern=r"^/connect_channel(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_disconnect_channel,
        events.NewMessage(pattern=r"^/disconnect_channel(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_connect_as,
        events.NewMessage(pattern=r"^/connect_as(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_disconnect,
        events.NewMessage(pattern=r"^/disconnect(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_list_main,
        events.NewMessage(pattern=r"^/list_main(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_list_search_groups,
        events.NewMessage(pattern=r"^/list_search_groups(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_list_connections,
        events.NewMessage(pattern=r"^/list_connections(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_reindex,
        events.NewMessage(pattern=r"^/reindex(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        cmd_channel_stats,
        events.NewMessage(pattern=r"^/channel_stats(?:\s|$)", incoming=True),
    )
    client.add_event_handler(
        handle_new_channel_post,
        events.NewMessage(incoming=True),
    )
    logger.info("Search handlers registered.")
