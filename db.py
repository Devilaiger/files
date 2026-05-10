"""
db.py — Async MongoDB operations via Motor.

Collections
───────────
  triggers          – per-group keyword → stored message mapping (Feature 1)
  main_channels     – channels whose posts are indexed as search content (Feature 2)
  search_groups     – groups/channels where bot actively responds to triggers & Show:
  channel_mappings  – search_group ↔ main_channel relationships (Feature 2)
  posts_index       – indexed post text + references from main_channels
  group_authority   – per-group bot admin authority (adder, owner, allowed/banned list)

Schema: triggers
  {trigger, group_id, storage_type, storage_text, storage_chat_id, storage_message_id,
   storage_media_b64, created_at, created_by_id, created_by_name}
  unique index: (trigger, group_id)

Schema: group_authority
  {group_id, adder_id, owner_id, allowed_ids: [...], banned_ids: [...], updated_at}
  unique index: group_id
"""
from __future__ import annotations

import logging
import re as _re
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
group_authority_col  = _db["group_authority"]


# ── Index setup ────────────────────────────────────────────────────────────────
async def _drop_legacy_indexes() -> None:
    for idx_name in ("trigger_1", "trigger_1_group_id_1", "trigger_group_unique"):
        try:
            await triggers_col.drop_index(idx_name)
            logger.info("Dropped index: %s", idx_name)
        except Exception:
            pass


async def setup_indexes() -> None:
    await _drop_legacy_indexes()

    await triggers_col.create_index(
        [("trigger", 1), ("group_id", 1)], unique=True
    )
    await triggers_col.create_index("group_id")
    await triggers_col.create_index("created_at")

    await main_channels_col.create_index("channel_id", unique=True)
    await search_groups_col.create_index("group_id", unique=True)

    await channel_mappings_col.create_index(
        [("search_group_id", 1), ("main_channel_id", 1)], unique=True
    )
    await channel_mappings_col.create_index("search_group_id")
    await channel_mappings_col.create_index("main_channel_id")

    await posts_col.create_index(
        [("channel_id", 1), ("message_id", 1)], unique=True
    )
    await posts_col.create_index("normalized_text")

    await group_authority_col.create_index("group_id", unique=True)

    logger.info("MongoDB indexes verified.")


# ── Migration ──────────────────────────────────────────────────────────────────
async def cleanup_invalid_ids() -> None:
    valid_channel = _re.compile(r'^-100\d{10,}$')

    all_ch = await main_channels_col.find({}, {"channel_id": 1}).to_list(length=None)
    bad_ch = [
        c["channel_id"] for c in all_ch
        if not valid_channel.match(str(c["channel_id"]))
    ]
    if bad_ch:
        await main_channels_col.delete_many({"channel_id": {"$in": bad_ch}})
        await channel_mappings_col.delete_many({"main_channel_id": {"$in": bad_ch}})
        await posts_col.delete_many({"channel_id": {"$in": bad_ch}})
        logger.warning(
            "cleanup_invalid_ids: removed %d main channel(s) with invalid IDs: %s",
            len(bad_ch), bad_ch,
        )
    else:
        logger.info("cleanup_invalid_ids: all channel IDs are valid.")


async def migrate() -> None:
    """
    Runs at every startup. Safe to run repeatedly — all steps are idempotent.

    Step 1 — delete pre-group-schema triggers (no group_id field at all).
    Step 2 — rename source_chat_id/source_message_id -> storage_chat_id/storage_message_id.
    """
    r1 = await triggers_col.delete_many({"group_id": {"$exists": False}})
    if r1.deleted_count:
        logger.warning(
            "Migration step 1: deleted %d trigger(s) with no group_id. "
            "Re-create them with /set_trigger inside the appropriate group.",
            r1.deleted_count,
        )

    r2 = await triggers_col.update_many(
        {"source_chat_id": {"$exists": True}},
        {
            "$rename": {
                "source_chat_id":    "storage_chat_id",
                "source_message_id": "storage_message_id",
            },
            "$set": {"storage_type": "media"},
        },
    )
    if r2.modified_count:
        logger.warning(
            "Migration step 2: renamed source_* fields on %d trigger(s) -> storage_* fields.",
            r2.modified_count,
        )

    if not r1.deleted_count and not r2.modified_count:
        logger.info("Migration: nothing to do.")


# ══════════════════════════════════════════════════════════════════════════════
#  GROUP AUTHORITY  (Plan B: Adder-controlled bot admin system)
# ══════════════════════════════════════════════════════════════════════════════

async def upsert_group_authority(
    group_id: int,
    adder_id: int,
    owner_id: Optional[int] = None,
    allowed_ids: Optional[list[int]] = None,
) -> None:
    """
    Create or update the authority record for a group.
    Called when the bot is first added to a group or when admins are refreshed.
    If allowed_ids is provided, it updates the list.
    """
    now = datetime.now(timezone.utc)
    
    # First check if document exists - if yes, we need to use different update logic
    existing = await group_authority_col.find_one({"group_id": group_id})
    
    if existing:
        # Document exists - use simple update without $setOnInsert
        update_doc: dict = {
            "adder_id":   adder_id,
            "owner_id":   owner_id,
            "updated_at": now,
        }
        if allowed_ids is not None:
            # FIX: If allowed_ids is provided, set it directly
            # Use $set directly for existing documents to avoid the conflict
            update_doc["allowed_ids"] = allowed_ids
        
        await group_authority_col.update_one(
            {"group_id": group_id},
            {"$set": update_doc},
        )
    else:
        # New document - use upsert with $setOnInsert
        await group_authority_col.update_one(
            {"group_id": group_id},
            {
                "$setOnInsert": {
                    "group_id":    group_id,
                    "adder_id":    adder_id,
                    "owner_id":    owner_id,
                    "allowed_ids": allowed_ids if allowed_ids is not None else [],
                    "banned_ids": [],
                    "created_at": now,
                    "updated_at": now,
                },
            },
            upsert=True,
        )



async def get_group_authority(group_id: int) -> Optional[dict]:
    return await group_authority_col.find_one({"group_id": group_id})


async def add_bot_admin(group_id: int, user_id: int) -> bool:
    """
    Add user_id to the allowed_ids list and remove from banned_ids.
    Returns True if modified.
    """
    r = await group_authority_col.update_one(
        {"group_id": group_id},
        {
            "$addToSet": {"allowed_ids": user_id},
            "$pull":     {"banned_ids": user_id},
            "$set":      {"updated_at": datetime.now(timezone.utc)},
        },
    )
    return r.modified_count > 0


async def remove_bot_admin(group_id: int, user_id: int) -> bool:
    """
    Remove user_id from allowed_ids and add to banned_ids.
    This ban survives /refresh — only re-add via add_bot_admin can lift it.
    Returns True if modified.
    """
    r = await group_authority_col.update_one(
        {"group_id": group_id},
        {
            "$pull":     {"allowed_ids": user_id},
            "$addToSet": {"banned_ids": user_id},
            "$set":      {"updated_at": datetime.now(timezone.utc)},
        },
    )
    return r.modified_count > 0


async def is_bot_admin_allowed(group_id: int, user_id: int) -> bool:
    """
    Check if a user_id is explicitly banned from this group's bot management.
    Used as a secondary check after Telegram admin verification.
    Returns True if NOT banned (i.e., allowed to proceed).
    Returns False if banned.
    """
    doc = await group_authority_col.find_one({"group_id": group_id})
    if not doc:
        return True  # No authority record yet — allow (first-time setup)
    banned = doc.get("banned_ids", [])
    return user_id not in banned


async def get_group_adder(group_id: int) -> Optional[int]:
    doc = await group_authority_col.find_one({"group_id": group_id}, {"adder_id": 1})
    return doc.get("adder_id") if doc else None


async def get_group_owner(group_id: int) -> Optional[int]:
    doc = await group_authority_col.find_one({"group_id": group_id}, {"owner_id": 1})
    return doc.get("owner_id") if doc else None


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGERS  (per-group, with creator metadata)
# ══════════════════════════════════════════════════════════════════════════════

async def upsert_trigger(
    trigger_text: str,
    group_id: int,
    storage_type: str,
    storage_text: Optional[str],
    storage_chat_id: Optional[int],
    storage_message_id: Optional[int],
    storage_media_b64: Optional[str] = None,
    created_by_id: Optional[int] = None,
    created_by_name: Optional[str] = None,
) -> None:
    key = trigger_text.lower().strip()
    now = datetime.now(timezone.utc)
    set_doc: dict = {
        "trigger":      key,
        "group_id":     group_id,
        "storage_type": storage_type,
    }
    if storage_type == "text":
        set_doc["storage_text"]       = storage_text
        set_doc["storage_chat_id"]    = None
        set_doc["storage_message_id"] = None
        set_doc["storage_media_b64"]  = None
    else:
        set_doc["storage_text"]       = storage_text
        set_doc["storage_chat_id"]    = storage_chat_id
        set_doc["storage_message_id"] = storage_message_id
        set_doc["storage_media_b64"]  = storage_media_b64

    # Update creator info only on new insert ($setOnInsert)
    insert_doc = {
        "created_at":      now,
        "created_by_id":   created_by_id,
        "created_by_name": created_by_name,
    }

    await triggers_col.update_one(
        {"trigger": key, "group_id": group_id},
        {"$set": set_doc, "$setOnInsert": insert_doc},
        upsert=True,
    )


async def fetch_triggers_for_group(group_id: int) -> list[dict]:
    return (
        await triggers_col
        .find({"group_id": group_id})
        .sort("created_at", 1)
        .to_list(length=None)
    )


async def fetch_all_triggers() -> list[dict]:
    return await triggers_col.find().sort("created_at", 1).to_list(length=None)


async def update_trigger_hidden_media(
    trigger_id,
    storage_text: Optional[str],
    storage_media_b64: str,
) -> None:
    await triggers_col.update_one(
        {"_id": trigger_id},
        {
            "$set": {
                "storage_text": storage_text,
                "storage_media_b64": storage_media_b64,
                "storage_chat_id": None,
                "storage_message_id": None,
            }
        },
    )


async def delete_trigger_at_index(
    group_id: int, one_based_index: int
) -> tuple[bool, Optional[str]]:
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
    r = await channel_mappings_col.delete_many({"search_group_id": search_group_id})
    return r.deleted_count


async def remove_all_mappings_for_main(main_channel_id: int) -> int:
    r = await channel_mappings_col.delete_many({"main_channel_id": main_channel_id})
    return r.deleted_count


async def get_main_channel_ids_for_group(search_group_id: int) -> list[int]:
    docs = await channel_mappings_col.find(
        {"search_group_id": search_group_id},
        {"main_channel_id": 1},
    ).to_list(length=None)
    return [d["main_channel_id"] for d in docs]


async def get_mappings_for_group(search_group_id: int) -> list[dict]:
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
    """Exact match (full normalized text equals query) within the given channels."""
    return await posts_col.find(
        {"normalized_text": query, "channel_id": {"$in": channel_ids}}
    ).to_list(length=None)


async def substring_search(query: str, channel_ids: list[int], limit: int = 10) -> list[dict]:
    """
    Return posts whose normalized_text CONTAINS query as a substring.
    This is the middle tier between exact-match and fuzzy — much faster than
    fuzzy on large indexes and handles cases like 'avengers' matching
    'avengers endgame' or 'the avengers season 2'.
    """
    pattern = _re.compile(_re.escape(query))
    return await posts_col.find(
        {"normalized_text": {"$regex": pattern.pattern}, "channel_id": {"$in": channel_ids}}
    ).limit(limit).to_list(length=None)


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
