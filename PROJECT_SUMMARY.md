# Project Summary

## What this bot does

This is a Python 3 Telethon bot backed by MongoDB. It has two main features:

1. Per-group triggers: an admin creates a keyword in a registered search group, attaches a text/media response, and the bot replays that response when users mention the keyword in that same group.
2. Show search: registered groups can search connected main channels with `Show: <name>`. Search results are scoped to the main channels connected to the current group.

## Important files

- `main.py`: starts Telethon, MongoDB setup, cache warmup, storage channel resolution, and routes non-command messages.
- `triggers.py`: trigger commands, wizard state handling, storage-channel media copying, trigger matching, and trigger replay.
- `search.py`: main-channel registration, search-group registration, group-to-channel mappings, indexing, `Show:` search, and scoped listing/stats commands.
- `db.py`: MongoDB collections, indexes, migrations, trigger storage, group/channel mappings, and post index queries.
- `state.py`: in-memory wizard state for multi-step trigger setup.
- `cache.py`: per-group trigger cache used at runtime.
- `helpers.py`: shared admin checks, text normalization, channel resolution, and forward/copy fallback helpers.
- `config.py`: environment variables and runtime constants.

## Data model

- `triggers`: stores `{trigger, group_id, storage_type, storage_text, storage_chat_id, storage_message_id}`. The unique key is `(trigger, group_id)`, so the same keyword can exist in multiple groups independently.
- `search_groups`: registered groups where triggers and `Show:` are active.
- `main_channels`: channels indexed as searchable content sources.
- `channel_mappings`: links one search group to one or more main channels.
- `posts_index`: normalized searchable text from main-channel posts.

## Empty media registration

Captionless media must be accepted during trigger setup Step 2. The bot should treat the following as valid attachable trigger responses:

- photo without caption
- video without caption
- document without caption
- sticker
- audio/voice
- text or link messages

The code now checks real media separately from text, so Step 2 does not depend on `event.text` or captions. If this still fails in a group, the usual Telegram-side cause is BotFather privacy mode: the bot must have privacy disabled and must be able to receive normal group messages, not only commands.

## Group isolation

Trigger wizard state is scoped by `(admin_user_id, chat_id)`. This means:

- A wizard started in Group A can only be completed by messages in Group A.
- A message sent in Group B by the same admin cannot complete Group A's wizard.
- The same person can administer Group A and Group B, but each group's trigger data and commands remain tied to that group.

Most search-management commands already use `event.chat_id` when run inside a group. Group admins see or edit only the current group's mappings, trigger list, stats, and search behavior. `ADMIN_IDS` remain super-admins by design and can use global views from private chat.

## Storage channel behavior

Media trigger responses are briefly copied into `STORAGE_CHANNEL_ID` with `send_file` first, which avoids Telegram's visible "Forwarded from" header. By default `STORAGE_DELETE_AFTER_SAVE=True`, so the bot saves a reusable media reference in MongoDB and then deletes the visible storage-channel post. This keeps the storage channel looking empty to members while triggers still replay from the saved reference.

On startup, the bot also migrates older visible storage-channel trigger posts into hidden Mongo-backed media references and deletes those old storage messages after migration. Older metadata captions such as `#trigger | ...` are stripped during migration.

The storage channel must be kept intact because trigger replay depends on the stored message IDs. If a storage message is deleted, the trigger record remains in MongoDB but replay will fail until the trigger is recreated.
