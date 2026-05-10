# Logic Simulations & Test Cases

This document simulates exact logic flows to understand how the system behaves under standard, abnormal, and edge-case conditions, explicitly mapping to the code inside `triggers.py`, `state.py`, and `search.py`.

## Simulation 1: The Setup Wizard Isolation 
**Context:** User (ID: 111) is an Admin in Group A and Group B.

1. User sends `/set_trigger` in **Group A**.
   - `triggers.cmd_set_trigger` executes.
   - `state.set((111, GroupA), AWAIT_TRIGGER_TEXT)` is recorded.
   - `initiated_chat_id = GroupA` is attached to the state.
2. User sends "batman" in **Group B** (accidentally).
   - `main.general_dispatcher` receives event for Group B.
   - Calls `state.has((111, GroupB))` -> Returns `False`.
   - Fallback calls `state.find_for_user(111)` -> Returns the state from Group A!
   - `handle_state_reply` is fired.
   - `handle_state_reply` sees `event.chat_id (Group B) != initiated_chat_id (Group A)`.
   - **Result:** Logs an isolation warning and `returns False`. The word "batman" is ignored by the wizard and allowed to pass through the dispatcher as a normal message.
3. User sends "batman" in **Group A**.
   - Event matched. `AWAIT_TRIGGER_MSG` is set.
4. Another User (ID: 222) sends a photo in **Group A**.
   - `handle_state_reply` runs. `event.sender_id (222) != created_by_id (111)`.
   - **Result:** Photo is ignored. Wizard remains open exclusively for User 111.

## Simulation 2: Trigger Matching Substring Rule
**Context:** Group has triggers saved: `[{"trigger": "apple"}, {"trigger": "apple pie"}]`.

1. User sends "I love apple pie".
2. `cache.find_match` converts to lowercase: "i love apple pie".
3. List comprehension checks matches:
   - "apple" is in text -> True (Length 5)
   - "apple pie" is in text -> True (Length 9)
4. `max()` calculates the longest match.
5. **Result:** The bot responds with the media attached to "apple pie", effectively preventing short triggers from overriding specific long-tail triggers.

## Simulation 3: Search Phase Filtering
**Context:** User searches `Show: The Boys` in Group X.

1. DB checks Phase 1 (`exact_search`): Does normalized text strictly equal `the boys`?
   - Finds: 0 results.
2. DB checks Phase 2 (`substring_search`): Does normalized text regex contain `the boys`?
   - Finds: "download the boys season 1". Returns it.
3. DB checks Phase 3 (`fuzz_process`):
   - Compares "the boys" to all posts using `fuzz.partial_ratio`.
   - It matches "da boyz" with a score of 85. If `config.FUZZY_THRESHOLD` is 80, it returns this too.
4. **Result:** Bot replies with exact substring matches first, supplemented by fuzzy typo matches.

## Edge Cases Verified in Code

| Scenario | Code Action | Result |
| :--- | :--- | :--- |
| **User sends a Sticker in Step 1 of Wizard** | `_handle_state_reply_inner` checks if `event.message.media` exists and is not a WebPage. | Rejects sticker, tells user "Step 1 needs a text keyword". Wizard stays open. |
| **Bot is removed from Storage Channel** | `resolve_storage_peer` in `main.py` fails on startup. | `_storage_peer` remains `None`. Setup wizard will fail at step 2 with a specific error message advising to restart. |
| **User replies to a message with `/set_trigger`** | `cmd_set_trigger` detects `has_reply`. Grabs first word of replied message. | Automatically uses the replied text as the keyword, jumps straight to `AWAIT_TRIGGER_MSG`. |
| **Pickle Deserialization Failure** | `_deserialize_media` encounters an old telethon class. | Captures exception, logs a warning. Returns `None`. `handle_trigger_match` will fail to send media. |
