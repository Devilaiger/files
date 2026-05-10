# Project Problems, Bugs, and Technical Debt

This file logs all actual, code-level problems, bugs, security risks, and unnecessary implementations discovered by analyzing the codebase.

## 1. Critical Bugs & Risks

### 1.1 Insecure Object Serialization (`pickle`)
- **Location:** `triggers.py` (`_serialize_media` and `_deserialize_media`)
- **Problem:** The code uses `pickle.dumps()` and `base64.b64encode()` to serialize arbitrary Telegram `Media` objects directly into the MongoDB `triggers_col`. 
- **Risk:** Unpickling data from a database is a massive security risk if the DB is ever compromised (RCE vulnerability). Furthermore, Telegram `Media` objects frequently change internal structures with library updates (Telethon). When Telethon updates, older pickled media objects will fail to unpickle, permanently breaking old triggers.
- **Solution:** Extract raw IDs (`document_id`, `access_hash`, `file_reference`) and store them in standard JSON formats instead of pickling complex Python class instances.

### 1.2 State Management Data Loss
- **Location:** `state.py`
- **Problem:** `_store: dict[Any, ConvState] = {}` is a global in-memory dictionary.
- **Risk:** Any bot restart (e.g., deployments, crashes) instantly wipes out all ongoing `/set_trigger` setups. If a user is midway through adding a trigger, they will be left hanging without any feedback.

### 1.3 Trigger Cache Scaling Issue
- **Location:** `cache.py`
- **Problem:** The caching mechanism stores ALL triggers for ALL groups in memory (`_triggers: dict[int, list[dict]]`). The `warm()` function fetches the entire `triggers` collection at startup.
- **Risk:** At 100+ groups with thousands of triggers each, memory consumption will spike dramatically. Furthermore, because it's an in-memory dictionary, this bot CANNOT be horizontally scaled. If deployed across multiple workers, cache invalidation (`invalidate_group`) will only apply to the worker handling the request, leaving other workers serving stale triggers.

### 1.4 FloodWait Handling Blocking
- **Location:** `search.py` (`_index_channel`)
- **Problem:** When a `FloodWaitError` is hit during indexing, the code uses `await asyncio.sleep(e.seconds)`.
- **Risk:** While `asyncio.sleep` yields to the event loop, indexing hundreds of channels can result in multiple long-running tasks sleeping, creating memory pressure. It also pauses the specific index loop for potentially hours, without gracefully yielding state if the bot restarts during that sleep.

## 2. Unnecessary Complexity & Redundancy

### 2.1 Double Storage Attempt for Media
- **Location:** `triggers.py` (`_store_media_in_storage`)
- **Problem:** The function attempts to send media three times in a try-except cascade (first with caption, then without caption, then via forward). This creates excessive API calls and delays on failure.
- **Fix:** Properly detect media types upfront (e.g., separating Stickers from generic media) rather than relying on "try it and fail."

### 2.2 Redundant Priority Fallback
- **Location:** `main.py` (`general_dispatcher`)
- **Problem:** The logic checks `state.has(session_key)` and if false, immediately searches the ENTIRE state dictionary via `state.find_for_user(sender_id)` just to see if a session exists in *another* chat.
- **Fix:** O(N) iteration over states per message received is extremely inefficient. If isolation is needed, index states by `user_id` as well as `session_key`.

### 2.3 Hardcoded Fallbacks for Admin Detection
- **Location:** `main.py` (`cmd_refresh_admins`)
- **Problem:** Deeply nested try-except blocks iterating over `event.client.iter_participants(chat_id, filter=ChannelParticipantsAdmins)` and then a fallback to `GetFullChatRequest`.
- **Fix:** Consolidate Telegram admin fetching into `helpers.py` into a single, clean function with robust error catching.

## 3. Missing Graceful Shutdown
- **Location:** `main.py`
- **Problem:** Signal handlers are registered (`signal.signal(signal.SIGINT, _handle_signal)`), but they only print a log statement. They do not close the `TelegramClient` or disconnect `AsyncIOMotorClient`.
- **Risk:** Uncommitted writes or hanging DB connections.
