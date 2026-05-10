# Project Maps and Visualizations

This file contains Mermaid visual diagrams generated precisely from the codebase logic to help any new developer immediately grasp the structure and data flow.

## 1. System Mind Map

```mermaid
mindmap
  root((Telegram Bot))
    main.py
      general_dispatcher
      priority_routing
        1. Wizard State
        2. Show: Search
        3. Trigger Match
    search.py
      Indexing
        _index_channel
        auto_index_new_post
      Querying
        _do_search
        Phase 1: Exact
        Phase 2: Substring
        Phase 3: Fuzzy (rapidfuzz)
    triggers.py
      Wizard FSM
        cmd_set_trigger
        handle_state_reply
      Matching
        handle_trigger_match
      Media Storage
        _store_media_in_storage
        _serialize_media
    Storage & Memory
      db.py (MongoDB)
        triggers_col
        posts_index
        group_authority
      cache.py (In-Memory)
        _triggers dict
        find_match
      state.py (FSM)
        _store dict
```

## 2. Core Data Flow: Message Dispatching Pipeline

```mermaid
sequenceDiagram
    participant User
    participant Main as main.py (Dispatcher)
    participant State as state.py
    participant Triggers as triggers.py
    participant Search as search.py
    participant Cache as cache.py

    User->>Main: Sends Message
    Main->>State: state.key(sender_id, chat_id)
    State-->>Main: Return has_session (True/False)
    
    alt has_session == True
        Main->>Triggers: handle_state_reply(event)
        Triggers->>State: Extract current state step
        Triggers->>Triggers: Process Input (e.g., _handle_state_reply_inner)
        Triggers-->>User: Reply Wizard Next Step / Success
    else Text starts with "Show:"
        Main->>Search: handle_show_search(event)
        Search->>Search: _do_search()
        Search-->>User: Return Markdown List of Posts
    else Normal Text
        Main->>Triggers: asyncio.create_task(handle_trigger_match)
        Triggers->>Cache: find_match(group_id, text)
        Cache-->>Triggers: Return matched trigger / None
        alt Match Found
            Triggers-->>User: Send Replay Media / Text
        end
    end
```

## 3. Data Flow: Setting up a Trigger (Wizard)

```mermaid
sequenceDiagram
    participant Admin
    participant Triggers as triggers.py
    participant State as state.py
    participant DB as db.py
    participant Storage as Storage Channel

    Admin->>Triggers: /set_trigger
    Triggers->>State: set(AWAIT_TRIGGER_TEXT, initiated_chat_id)
    Triggers-->>Admin: "Step 1: Type Keyword"
    
    Admin->>Triggers: "keyword"
    Triggers->>State: set(AWAIT_TRIGGER_MSG, trigger_text="keyword")
    Triggers-->>Admin: "Step 2: Send Media/Text"
    
    Admin->>Triggers: Sends Photo
    Triggers->>Storage: _store_media_in_storage(photo)
    Storage-->>Triggers: Return stored_msg ID
    Triggers->>Triggers: _serialize_media(media) -> Base64
    Triggers->>DB: upsert_trigger(trigger_text, media_b64)
    Triggers->>State: clear(session_key)
    Triggers-->>Admin: "Trigger Saved!"
```

## 4. Database Connection Topology

```mermaid
erDiagram
    SEARCH_GROUP ||--o{ CHANNEL_MAPPING : maps_to
    MAIN_CHANNEL ||--o{ CHANNEL_MAPPING : connected_to
    MAIN_CHANNEL ||--o{ POSTS_INDEX : contains
    SEARCH_GROUP ||--o{ TRIGGERS : contains
    SEARCH_GROUP ||--|| GROUP_AUTHORITY : managed_by

    SEARCH_GROUP {
        int group_id
        string title
    }
    MAIN_CHANNEL {
        int channel_id
        string title
    }
    POSTS_INDEX {
        int message_id
        string normalized_text
    }
    TRIGGERS {
        string trigger
        string storage_type
        string storage_media_b64
    }
    GROUP_AUTHORITY {
        int adder_id
        array allowed_ids
    }
```
