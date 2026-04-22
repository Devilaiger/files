"""
db.py — Async MongoDB operations via Motor.

Collections
───────────
  triggers          – per-group keyword → stored message mapping (Feature 1)
  main_channels     – channels whose posts are indexed as search content (Feature 2)
  search_groups     – groups/channels where bot actively responds to triggers & Show:
  channel_mappings  – search_group ↔ main_channel relationships (Feature 2)
  posts_index       – indexed post text + references from main_channels

Schema: triggers
  {trigger, group_id, source_chat_id, source_message_id, created_at}
  unique index: (trigger, group_id)  ← same keyword allowed in different groups

Schema: channel_mappings
  {search_group_id, main_channel_id, connected_by, connected_at}
  unique index: (search_group_id, main_channel_id)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from config import DB_NAME, MONGO_URI

logger = logging.getLogger(__name__)

# ── Client ─────────────────────────────────────────────────────────────────────
_client: AsyncIOMotorClient = AsyncIOMotorClient(MONGO_URI)
_db = _client[DB_NAME]

triggers_col         = _db["triggers"]
main_channels_col    = _db["main_channels"]
search_groups_col    = _db["search_groups"]
channel_mappings_col = _db["channel_mappings"]
posts_col            = _db["posts_index"]


# ── Index setup ────────────────────────────────────────────────────────────────
async def setup_indexes() -> None:
    # triggers: unique per (keyword, group)
    await triggers_col.create_index(
        [("trigger", 1), ("group_id", 1)], unique=True
    )
    await triggers_col.create_index("group_id")
    await triggers_col.create_index("created_at")

    await main_channels_col.create_index("channel_id", unique=True)
    await search_groups_col.create_index("group_id", unique=True)

    # channel_mappings: unique per (search_group, main_channel) pair
    await channel_mappings_col.create_index(
        [("search_group_id", 1), ("main_channel_id", 1)], unique=True
    )
    await channel_mappings_col.create_index("search_group_id")
    await channel_mappings_col.create_index("main_channel_id")

    await posts_col.create_index(
        [("channel_id", 1), ("message_id", 1)], unique=True
    )
    await posts_col.create_index("normalized_text")
    logger.info("MongoDB indexes verified.")


# ── Migration ──────────────────────────────────────────────────────────────────
async def cleanup_invalid_ids() -> None:
    """
    Remove stored documents whose IDs are not in valid -100XXXXXXXXXX format.
    These are leftovers from the old broken resolve_channel that accepted -5...
    style IDs. This runs once at startup alongside migrate().
    """
    import re
    valid_pattern = re.compile(r'^-100\d{10,}$')

    # Clean search_groups with invalid group_id
    all_groups = await search_groups_col.find({}, {"group_id": 1}).to_list(length=None)
    bad_groups = [
        g["group_id"] for g in all_groups
        if not valid_pattern.match(str(g["group_id"]))
    ]
    if bad_groups:
        await search_groups_col.delete_many({"group_id": {"$in": bad_groups}})
        await channel_mappings_col.delete_many({"search_group_id": {"$in": bad_groups}})
        logger.warning(
            "cleanup_invalid_ids: removed %d search group(s) with invalid IDs: %s",
            len(bad_groups), bad_groups,
        )

    # Clean main_channels with invalid channel_id
    all_ch = await main_channels_col.find({}, {"channel_id": 1}).to_list(length=None)
    bad_ch = [
        c["channel_id"] for c in all_ch
        if not valid_pattern.match(str(c["channel_id"]))
    ]
    if bad_ch:
        await main_channels_col.delete_many({"channel_id": {"$in": bad_ch}})
        await channel_mappings_col.delete_many({"main_channel_id": {"$in": bad_ch}})
        await posts_col.delete_many({"channel_id": {"$in": bad_ch}})
        logger.warning(
            "cleanup_invalid_ids: removed %d main channel(s) with invalid IDs: %s",
            len(bad_ch), bad_ch,
        )

    if not bad_groups and not bad_ch:
        logger.info("cleanup_invalid_ids: all IDs are valid.")


async def migrate() -> None:
    """
    Remove trigger documents that predate the per-group schema.
    Old schema: {trigger, chat_id, message_id}   <- no group_id field
    New schema: {trigger, group_id, source_chat_id, source_message_id}

    Old documents cannot be migrated automatically: the old chat_id was the
    source message's chat, not the owning group, so ownership is unrecoverable.
    They are deleted; re-create them with /set_trigger inside the correct group.
    """
    result = await triggers_col.delete_many({"group_id": {"$exists": False}})
    if result.deleted_count:
        logger.warning(
            "Migration: deleted %d old-schema trigger(s) with no group_id. "
            "Re-create them with /set_trigger inside the appropriate search group.",
            result.deleted_count,
        )
    else:
        logger.info("Migration: no old-schema triggers found.")



# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGERS  (per-group)
# ══════════════════════════════════════════════════════════════════════════════

async def upsert_trigger(
    trigger_text: str,
    group_id: int,
    source_chat_id: int,
    source_message_id: int,
) -> None:
    """Insert or update a trigger for a specific group."""
    doc = {
        "trigger":           trigger_text.lower().strip(),
        "group_id":          group_id,
        "source_chat_id":    source_chat_id,
        "source_message_id": source_message_id,
        "created_at":        datetime.now(timezone.utc),
    }
    await triggers_col.replace_one(
        {"trigger": doc["trigger"], "group_id": group_id},
        doc,
        upsert=True,
    )


async def fetch_triggers_for_group(group_id: int) -> list[dict]:
    """Return all triggers that belong to a specific group, sorted by creation time."""
    return (
        await triggers_col
        .find({"group_id": group_id})
        .sort("created_at", 1)
        .to_list(length=None)
    )


async def fetch_all_triggers() -> list[dict]:
    """Return every trigger across all groups (used for startup cache warm)."""
    return await triggers_col.find().sort("created_at", 1).to_list(length=None)


async def delete_trigger_at_index(
    group_id: int, one_based_index: int
) -> tuple[bool, Optional[str]]:
    """Delete the nth trigger (1-based) for a group. Returns (success, deleted_text)."""
    group_triggers = await fetch_triggers_for_group(group_id)
    idx = one_based_index - 1
    if not (0 <= idx < len(group_triggers)):
        return False, None
    doc = group_triggers[idx]
    await triggers_col.delete_one({"_id": doc["_id"]})
    return True, doc["trigger"]


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN CHANNELS  (content source — indexed for Show: search)
# ══════════════════════════════════════════════════════════════════════════════

async def add_main_channel(
    channel_id: int,
    username: Optional[str],
    title: Optional[str],
    connected_by: Optional[int] = None,
) -> None:
    doc = {
        "channel_id":   channel_id,
        "username":     username,
        "title":        title,
        "connected_by": connected_by,
        "connected_at": datetime.now(timezone.utc),
    }
    await main_channels_col.replace_one({"channel_id": channel_id}, doc, upsert=True)


async def remove_main_channel(channel_id: int) -> bool:
    r = await main_channels_col.delete_one({"channel_id": channel_id})
    return r.deleted_count > 0


async def get_main_channels() -> list[dict]:
    return await main_channels_col.find().sort("connected_at", 1).to_list(length=None)


async def get_main_channel_by_id(channel_id: int) -> Optional[dict]:
    return await main_channels_col.find_one({"channel_id": channel_id})


async def is_main_channel(channel_id: int) -> bool:
    return await main_channels_col.count_documents({"channel_id": channel_id}) > 0


# ══════════════════════════════════════════════════════════════════════════════
#  SEARCH GROUPS  (where bot actively listens)
# ══════════════════════════════════════════════════════════════════════════════

async def add_search_group(
    group_id: int,
    title: Optional[str],
    connected_by: Optional[int] = None,
) -> None:
    doc = {
        "group_id":     group_id,
        "title":        title,
        "connected_by": connected_by,
        "connected_at": datetime.now(timezone.utc),
    }
    await search_groups_col.replace_one({"group_id": group_id}, doc, upsert=True)


async def remove_search_group(group_id: int) -> bool:
    r = await search_groups_col.delete_one({"group_id": group_id})
    return r.deleted_count > 0


async def get_search_groups() -> list[dict]:
    return await search_groups_col.find().sort("connected_at", 1).to_list(length=None)


async def is_search_group(group_id: int) -> bool:
    return await search_groups_col.count_documents({"group_id": group_id}) > 0


# ══════════════════════════════════════════════════════════════════════════════
#  CHANNEL MAPPINGS  (search_group ↔ main_channel)
# ══════════════════════════════════════════════════════════════════════════════

async def add_channel_mapping(
    search_group_id: int,
    main_channel_id: int,
    connected_by: Optional[int] = None,
) -> None:
    """Link a search group to a main channel. Idempotent."""
    doc = {
        "search_group_id": search_group_id,
        "main_channel_id": main_channel_id,
        "connected_by":    connected_by,
        "connected_at":    datetime.now(timezone.utc),
    }
    await channel_mappings_col.replace_one(
        {"search_group_id": search_group_id, "main_channel_id": main_channel_id},
        doc,
        upsert=True,
    )


async def remove_channel_mapping(search_group_id: int, main_channel_id: int) -> bool:
    r = await channel_mappings_col.delete_one(
        {"search_group_id": search_group_id, "main_channel_id": main_channel_id}
    )
    return r.deleted_count > 0


async def remove_all_mappings_for_group(search_group_id: int) -> int:
    """Delete all main-channel links for a search group (called on disconnect)."""
    r = await channel_mappings_col.delete_many({"search_group_id": search_group_id})
    return r.deleted_count


async def remove_all_mappings_for_main(main_channel_id: int) -> int:
    """Delete all search-group links for a main channel (called on disconnect)."""
    r = await channel_mappings_col.delete_many({"main_channel_id": main_channel_id})
    return r.deleted_count


async def get_main_channel_ids_for_group(search_group_id: int) -> list[int]:
    """Return main channel IDs connected to a search group."""
    docs = await channel_mappings_col.find(
        {"search_group_id": search_group_id},
        {"main_channel_id": 1},
    ).to_list(length=None)
    return [d["main_channel_id"] for d in docs]


async def get_mappings_for_group(search_group_id: int) -> list[dict]:
    """Return full mapping docs for a search group."""
    return await channel_mappings_col.find(
        {"search_group_id": search_group_id}
    ).to_list(length=None)


async def has_mapping(search_group_id: int, main_channel_id: int) -> bool:
    return (
        await channel_mappings_col.count_documents(
            {"search_group_id": search_group_id, "main_channel_id": main_channel_id}
        )
        > 0
    )


# ══════════════════════════════════════════════════════════════════════════════
#  POSTS INDEX  (text references from main channels)
# ══════════════════════════════════════════════════════════════════════════════

async def upsert_post(
    channel_id: int,
    message_id: int,
    text: str,
    normalized_text: str,
) -> None:
    doc = {
        "channel_id":      channel_id,
        "message_id":      message_id,
        "text":            text,
        "normalized_text": normalized_text,
        "indexed_at":      datetime.now(timezone.utc),
    }
    await posts_col.replace_one(
        {"channel_id": channel_id, "message_id": message_id},
        doc,
        upsert=True,
    )


async def exact_search(query: str, channel_ids: list[int]) -> list[dict]:
    """Exact match within the given set of channels."""
    return await posts_col.find(
        {"normalized_text": query, "channel_id": {"$in": channel_ids}}
    ).to_list(length=None)


async def get_posts_for_fuzzy(channel_ids: list[int]) -> list[dict]:
    """Return lightweight docs for fuzzy scoring, scoped to given channels."""
    return await posts_col.find(
        {"channel_id": {"$in": channel_ids}},
        {"_id": 0, "channel_id": 1, "message_id": 1, "normalized_text": 1},
    ).to_list(length=None)


async def count_indexed_posts(channel_id: int) -> int:
    return await posts_col.count_documents({"channel_id": channel_id})


async def delete_channel_posts(channel_id: int) -> int:
    r = await posts_col.delete_many({"channel_id": channel_id})
    return r.deleted_count
