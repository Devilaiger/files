# Telegram Bot — Trigger Replay + Show Search

A production-ready Telethon bot with two core features:

- **Feature 1** — Admin-defined keyword triggers that auto-replay stored messages
- **Feature 2** — `Show: <name>` search across indexed Telegram channels

---

## Stack

| Layer | Technology |
|-------|-----------|
| Bot framework | [Telethon](https://docs.telethon.dev/) (MTProto) |
| Database | MongoDB (async via Motor) |
| Fuzzy search | rapidfuzz |
| Runtime | Python 3.11+ |

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- MongoDB (local or Atlas)
- Telegram API credentials from [my.telegram.org](https://my.telegram.org/apps)
- A bot token from [@BotFather](https://t.me/BotFather)

### 2. Clone & install

```bash
git clone <your-repo>
cd telegram_bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your values
```

### 4. BotFather settings (required)

Go to [@BotFather](https://t.me/BotFather) → your bot:

- **Disable Privacy Mode** (`/setprivacy` → `Disable`)  
  ← Required to receive all group messages, not just `/commands`.
- **Allow Groups** (`/setjoingroups` → `Enable`)

### 5. Run

```bash
python main.py
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `API_ID` | ✅ | — | From my.telegram.org |
| `API_HASH` | ✅ | — | From my.telegram.org |
| `BOT_TOKEN` | ✅ | — | From @BotFather |
| `MONGO_URI` | ✅ | `mongodb://localhost:27017` | MongoDB connection string |
| `DB_NAME` | | `tgbot` | Database name |
| `ADMIN_IDS` | ✅ | — | Comma-separated Telegram user IDs |
| `FUZZY_THRESHOLD` | | `90` | Fuzzy match strictness (0–100) |
| `MAX_SEARCH_RESULTS` | | `5` | Max results per Show: search |
| `TRIGGERS_PER_PAGE` | | `10` | Triggers per page in /trigger_list |
| `INDEX_LIMIT` | | `5000` | Max messages to pull per channel |
| `SESSION_NAME` | | `bot_session` | Telethon session file name |

---

## Feature 1 — Trigger System

### Setup (3 methods)

#### Method 1 — Interactive wizard

```
/set_trigger
```
Bot guides you step-by-step: asks for keyword → asks for message.

#### Method 2 — With inline keyword

```
/set_trigger queen of tears
```
Bot immediately asks for the message to attach.

#### Method 3 — Reply to an existing message

```
/set_trigger        ← while replying to a message
```
Bot uses the **first word** of the replied message as the trigger, then asks you to send the response message.

---

### Runtime

Every incoming message (PM, group, channel) is checked:
1. Message text is lowercased.
2. Each stored trigger is checked as a **substring** — first match wins.
3. The matched stored message is **forwarded** to the chat.

---

### Management Commands

| Command | Description |
|---------|-------------|
| `/trigger_list` | Paginated list of all triggers with ◀▶ buttons |
| `/delete <n>` | Delete trigger at index `n` (from list) |
| `/cancel` | Abort the current setup wizard |

---

## Feature 2 — Show Search

### Search (anyone in any chat)

```
Show: Breaking Bad
Show: queen of tears
Show: the bear season 2
```

The prefix `Show:` (case-insensitive) is required. Everything after it is the query.

**Search logic:**
1. **Exact match** — finds posts where normalized text exactly equals the query.
2. **Fuzzy fallback** — if no exact match, uses `token_sort_ratio` with a configurable threshold (default 90%).
3. Max `MAX_SEARCH_RESULTS` results returned.

**Results:**
- Public channel → clickable `t.me/{username}/{id}` link
- Private channel → message forwarded directly

---

### Channel Management (admin only)

| Command | Description |
|---------|-------------|
| `/add_channel_search @username` | Add channel and immediately index it |
| `/add_channel_search -100123456` | Add by numeric ID (for private channels) |
| `/remove_channel_search @username` | Remove channel and wipe its index |
| `/list_channels` | Show all configured search channels |
| `/reindex_channel @username` | Wipe and rebuild index for a channel |
| `/channel_stats` | Show indexed post counts per channel |

**Adding a private channel:**
1. Add the bot as an admin of the private channel.
2. Get the channel ID (use @getidsbot or check bot logs).
3. `/add_channel_search -100<channel_id>`

---

## MongoDB Collections

### `triggers`
```json
{
  "trigger": "queen of tears",
  "chat_id": -1001234567890,
  "message_id": 55,
  "created_at": "2024-01-01T00:00:00Z"
}
```

### `search_channels`
```json
{
  "channel_id": -1001234567890,
  "username": "mychannel",
  "title": "My Channel",
  "added_at": "2024-01-01T00:00:00Z"
}
```

### `posts_index`
```json
{
  "channel_id": -1001234567890,
  "message_id": 456,
  "text": "Breaking Bad - Full Series HD",
  "normalized_text": "breaking bad full series hd",
  "indexed_at": "2024-01-01T00:00:00Z"
}
```

---

## Project Structure

```
telegram_bot/
├── main.py              # Entry point + general dispatcher
├── config.py            # Config loader (env vars, fail-fast)
├── db.py                # All MongoDB operations (Motor async)
├── state.py             # In-memory FSM for multi-step flows
├── cache.py             # In-memory trigger cache
├── features/
│   ├── __init__.py
│   ├── helpers.py       # Shared utils (admin check, normalize, forward)
│   ├── triggers.py      # Feature 1 — all trigger handlers
│   └── search.py        # Feature 2 — search + channel management
├── requirements.txt
├── .env.example
└── README.md
```

---

## Production Deployment

### systemd service

```ini
# /etc/systemd/system/tgbot.service
[Unit]
Description=Telegram Bot
After=network.target mongod.service

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/telegram_bot
EnvironmentFile=/opt/telegram_bot/.env
ExecStart=/opt/telegram_bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable tgbot
sudo systemctl start tgbot
sudo journalctl -u tgbot -f   # live logs
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

```bash
docker build -t tgbot .
docker run -d --env-file .env --name tgbot tgbot
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot doesn't see group messages | Disable privacy mode in @BotFather |
| Cannot access private channel | Add bot as channel admin, use numeric ID |
| `FloodWaitError` during indexing | Reduce `INDEX_LIMIT`, index in off-peak hours |
| Trigger fires but message not sent | Source message may be deleted — check bot logs |
| Fuzzy search returns wrong results | Raise `FUZZY_THRESHOLD` to 92–95 |
| `MongoServerSelectionTimeoutError` | Check `MONGO_URI` and MongoDB service |
