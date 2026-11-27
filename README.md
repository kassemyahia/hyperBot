# HyperBot

HyperBot is the official Telegram bot for Hyper Team at the Faculty of Informatics
Engineering, University of Aleppo. It keeps the existing Arabic menus, availability
inquiries, download requests, anonymous complaints, administrator messages, and file
delivery, and adds a safe integration between the **Hyper Vault forum supergroup**, a
Google Sheets website catalog, and private Telegram delivery.

The source group is never modified. The historical importer reads metadata only; it
does not download media. Public Google Sheets rows receive only a random bot deep link
and an availability status—never a group ID, message ID, topic ID, token, or credential.

## Architecture and safety

```text
Hyper Vault forum (read only)
  ├─ Telethon historical scanner ─┐
  └─ Bot API new-message updates ─┴─> filter -> conservative matcher -> SQLite review queue
                                                       │
Google Sheets catalog <──────── approved link sync <───┤
                                                       ▼
Website deep link -> private /start f_<random> -> copy_message(s) -> visitor
```

`copy_message`/`copy_messages` is used, never forwarding. Captions are removed by
default. Delivery is private-chat-only and succeeds only for approved, active records.
Tokens remain stable when a source mapping, title, or sheet row changes. Albums and
administrator-grouped multipart resources preserve their approved ordering.

## Fedora installation

Install Python and build tools, clone the repository, and create an isolated virtual
environment:

```bash
sudo dnf install python3 python3-pip git sqlite
git clone https://github.com/kassemyahia/hyperBot.git
cd hyperBot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Python 3.11 or newer is recommended. Activate `.venv` again in each new shell.

## Telegram bot and Hyper Vault setup

1. In `@BotFather`, create the bot or rotate its token with `/revoke`. A token present
   in the legacy source must be considered compromised and must never be reused.
2. Set the new token and bot username in `.env`.
3. Add the bot to the existing **Hyper Vault** supergroup.
4. Disable BotFather privacy mode for this bot so it receives ordinary group media
   updates, or grant the bot administrator access. It needs to see messages and read
   topics; it does not need permission to delete or edit source messages.
5. Protected content cannot be copied by the Bot API. Disable content protection for
   resources intended for delivery, or leave those resources unavailable.
6. Obtain the numeric group ID (normally `-100...`) using a temporary diagnostic bot,
   Telegram API tool, or by inspecting an update while the bot is in the group. Set it
   as `SOURCE_CHAT_ID`. Do not use a public `@username` in place of the numeric ID.
   If the group has a public username, also set `SOURCE_CHAT_USERNAME` so source links
   can be validated during live Bot API discovery.

Run the topic discovery command after configuring Telethon; it prints the authoritative
numeric IDs and titles:

```bash
python -m scripts.import_group --list-topics
```

Configure `INCLUDED_TOPIC_IDS` and `EXCLUDED_TOPIC_IDS` with those IDs. An empty include
list means all topics. Exclusions win. Names are display metadata and are deliberately
not used as access policy.

## Configuration

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Newly rotated BotFather token |
| `BOT_USERNAME` | Bot username, normally `HyperTeamITE_Bot` |
| `SOURCE_CHAT_ID` | Numeric Hyper Vault supergroup ID |
| `SOURCE_CHAT_USERNAME` | Optional public username used only to validate public source URLs |
| `SOURCE_CHANNEL_ID` | Deprecated fallback accepted during migration |
| `ADMIN_USER_IDS` | Comma-separated Telegram **user** IDs; checked on every command/callback |
| `DOWNLOAD_REQUEST_CHAT_ID` | Numeric destination chat for user download requests |
| `DOWNLOAD_REQUEST_TOPIC_ID` | Optional positive forum topic ID; blank preserves existing routing, 1 means General |
| `COMPLAINT_RECIPIENT_CHAT_ID` | Destination for anonymous complaints |
| `DATABASE_URL` | `sqlite:///data/hyperbot.db` by default |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | User API credentials for Telethon import |
| `TELETHON_SESSION_PATH` | Private user session path, without needing a `.session` suffix |
| `INCLUDED_TOPIC_IDS` / `EXCLUDED_TOPIC_IDS` | Comma-separated actual forum topic IDs |
| `MIN_FILE_SIZE_BYTES` | Reject smaller media during discovery |
| `GOOGLE_SHEET_ID` | Private spreadsheet identifier |
| `GOOGLE_APPLICATION_CREDENTIALS` | Absolute service-account JSON path |
| `AUTO_APPROVE_EXACT_MATCHES` | Conservative exact-match auto-registration; default `false` |
| `AUTO_SYNC_GOOGLE_SHEETS` | Sync approvals to Sheets; default `true` |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | Per-user delivery limit |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

Secrets, `.env`, Google JSON credentials, SQLite databases, and Telethon session files
are ignored by Git. Keep them outside the checkout where practical and restrict their
permissions. Telethon is used only by the separate importer, never by bot delivery.

## Create a private Telethon session

Create an application at <https://my.telegram.org>, copy its API ID and API hash into
`.env`, and choose a private session path. On the first importer command, Telethon asks
for the account phone number, login code, and (when enabled) 2FA password:

```bash
mkdir -p data/telethon
chmod 700 data/telethon
python -m scripts.import_group --list-topics
```

Never paste login codes into source files or commit the resulting `.session` file.
Use an authorized team-owned Telegram user that already has read access to Hyper Vault.

## Google Sheets preparation

The spreadsheet must contain tabs named `softwares` (current GID `0`) and `courses`
(current GID `291500461`). Existing columns remain untouched. HyperBot uses these exact
additional column names:

- `File ID`
- `Telegram Link`
- `Telegram Status`

Create a Google Cloud service account, enable the Google Sheets API, download its JSON
key to a private location, and share the spreadsheet with the service-account email as
an editor. Set `GOOGLE_SHEET_ID` and `GOOGLE_APPLICATION_CREDENTIALS`.

Adding stable IDs is an explicit, one-time operation:

```bash
python -m scripts.sync_catalog --initialize-ids
```

It fills only missing IDs for named rows, prefixes IDs by tab (`sw_` or `course_`), and
never regenerates an existing ID. Row numbers are not identities. Review the sheet and
back it up before this first write. Routine import never creates unknown public rows.
Source-linked rows can appear as exact preview matches before IDs exist, but approval,
registration, and website publication remain blocked until each row has a stable ID.

To retry/synchronize all existing registry records:

```bash
python -m scripts.sync_catalog --sync
```

Approvals write only `Telegram Link` and `Telegram Status`. Supported statuses are
`available`, `unavailable`, and `pending_review`. Google failures do not invalidate a
successful private registration; they are recorded in SQLite and can be retried.

## Historical import

All scans are read-only against Hyper Vault. Useful commands are:

```bash
# Discover authoritative topic IDs.
python -m scripts.import_group --list-topics

# Preview/filter/match without changing SQLite.
python -m scripts.import_group --dry-run --exclude-topics 10,11 --limit 500

# Preview one topic with one concise row per Telegram message and match reason.
python -m scripts.import_group --preview --topic-id 123 --limit 100

# Persist a complete scan of configured topics.
python -m scripts.import_group --full-scan --include-topics 123,456

# Continue after Ctrl-C, process restart, or FloodWait interruption.
python -m scripts.import_group --resume --include-topics 123,456

# Scan and persist only one topic.
python -m scripts.import_group --full-scan --topic-id 123

# Work without Google access; eligible resources will be unmatched.
python -m scripts.import_group --dry-run --no-catalog

# Print a review report from SQLite.
python -m scripts.import_group --report --limit 1000
```

Checkpoints are stored per source chat and selected topic. Re-running is safe because
`(source_chat_id, message_id)` is unique. `Ctrl-C` saves progress. Telegram FloodWait
requests save a checkpoint and are respected before the scan resumes. Use `--limit` for
small operational batches; a limited run is intentionally not marked complete.

The filter ignores text conversations, empty and service messages, topic create/edit
events, excluded topics, unsupported media, too-small files, duplicates, and previous
decisions. Metadata includes filename, caption, size, MIME type, album ID, timestamp,
and topic association; media bytes are never downloaded.

Matching first validates exact public or private Telegram source URLs against the
configured Hyper Vault identity and message/topic IDs. It next preserves an existing
approved source-message/File-ID association. Only then does it compare case,
separators, CamelCase, versions, installer noise, aliases, size, extension, category,
caption, and topic title. Duplicate source links and competing metadata remain
`ambiguous`; absent reliable evidence is `unmatched`. YouTube links are never treated
as Telegram sources. Conservative automatic approval is off by default and never
creates unknown catalog rows.

## Administrator review

Every privileged action validates `update.effective_user.id`; a group/channel ID is
never accepted as an administrator. Use these commands in the private bot chat:

```text
/reviews [topic_id]                   summary of pending items
/review_next                          interactive next item
/review_search photoshop              search pending discoveries/matches
/review_decided ignored               list ignored (also accepts rejected)
/review_approve 17                    approve proposed File ID
/review_approve 17 sw_other            choose another existing File ID
/review_approve 17 sw_other 8,9,12     explicitly group multipart discovery IDs
/review_batch 17,18,19                 approve safe proposed matches in a batch
/review_reconsider 21                 return ignored/rejected item to the queue
```

Interactive buttons provide Approve Match, Reject Match, Ignore File,
Search/Choose Another Website Entry, and Show Details. Repeated callback clicks are
idempotent. Rejected and ignored discoveries remain recorded and are not suggested by
later scans. Albums group by Telegram media-group ID only; separate archive parts group
only when an administrator supplies their discovery IDs. Initial imports produce a
summary and on-demand queue rather than one notification per historical message.

Existing direct registration commands remain supported:

```text
/file_register File-ID 451 Display title
/file_begin File-ID Display title
# Forward source messages from Hyper Vault, then:
/file_commit
/file_update File-ID 500,501
/file_link File-ID
/file_test File-ID
/file_disable File-ID
/file_enable File-ID
/files [query]
```

The Bot API identifies a forwarded supergroup but does not expose its original message
ID, so Hyper Vault history should normally be registered through the importer. The
guided workflow remains compatible with legacy channel origins. Explicit message IDs
can also be used, but the visible copy test must succeed before saving.

## Future uploads

The normal polling process accepts messages only from `SOURCE_CHAT_ID`, identifies the
forum thread, applies the same persistent filter/matcher, and queues review candidates.
It cannot confuse private administrator messages or other groups with storage uploads.
Exact, non-album matches may auto-register when `AUTO_APPROVE_EXACT_MATCHES=true`.
Live albums stay in review until all Telegram updates arrive, preventing partial public
resources. Keep only one polling process per bot token and SQLite database.

Start the bot with:

```bash
python -m bot.main
# or the compatible entry point:
python app.py
```

## Website links and delivery

An approved link has this form:

```text
https://t.me/HyperTeamITE_Bot?start=f_<secure-random-token>
```

The token maps privately to one active File ID, one source group, and ordered message
IDs. The visitor must open it in a private chat. HyperBot validates the token and state,
then copies exactly those messages. It announces success only after Telegram confirms
all copies. Deleted messages, lost group access, protected content, API errors, and
partial delivery receive generic safe errors without exposing storage metadata.

Unavailable website resources can use the agreed fallback:

```text
https://t.me/HyperTeamITE_Bot?start=request
```

This enters the existing download-request conversation. Plain `/start`, menus, `/help`,
and `/cancel` retain their existing behavior. Deep links used in a group never deliver.

A credential-free JSON/CSV export remains available for deployments that need it:

```bash
python scripts/export_links.py > telegram-links.json
python scripts/export_links.py --format csv > telegram-links.csv
```

## Tests

External Telegram and Google calls are mocked; no real credentials are required:

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
# standard-library runner also works:
python -m unittest discover -v
```

Tests cover legacy flows, authorization, routing, private delivery, copy failures,
normalization and matching, filtering, historical parsing, duplicate persistence,
album/multipart registration, rejection/ignore/reconsideration, checkpoints, stable
tokens, and `/start request`.

## systemd deployment

Create `/etc/systemd/system/hyperbot.service` (adjust user and paths):

```ini
[Unit]
Description=HyperBot Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=hyperbot
Group=hyperbot
WorkingDirectory=/opt/hyperbot
EnvironmentFile=/opt/hyperbot/.env
ExecStart=/opt/hyperbot/.venv/bin/python -m bot.main
Restart=on-failure
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hyperbot
sudo systemctl status hyperbot
journalctl -u hyperbot -f
```

Run historical imports manually from the virtual environment, not inside the bot
polling service. This keeps large scans and interactive user authentication separate
from visitor delivery.

## Backup and recovery

SQLite uses WAL mode. Use an online backup, not a copy of only the main file:

```bash
mkdir -p /secure/backups
sqlite3 data/hyperbot.db ".backup '/secure/backups/hyperbot-$(date +%F).db'"
```

Also back up `.env`, the Google key, and Telethon session separately in encrypted
storage. Never place them in Git. To recover, stop the service, restore the database to
the `DATABASE_URL` path, verify ownership/permissions, and restart. A restored public
token continues to work because tokens and ordered mappings live in SQLite.

If an import fails, rerun with `--resume`. If Sheets fails, fix credentials/sharing and
run `python -m scripts.sync_catalog --sync`. If copying fails, verify bot membership,
source-message existence, content protection, and `SOURCE_CHAT_ID`, then use
`/file_test File-ID`. Disabling a resource preserves history and its token; deleting a
record should be reserved for genuinely permanent removal.

## Telegram limitations

- The Bot API cannot enumerate arbitrary history; Telethon user authorization is
  therefore required for historical scanning.
- Bots cannot copy protected content or unsupported service messages.
- Telegram has no atomic caption-stripping multi-copy operation. Default sequential
  copies strip captions and may partially succeed; retained-caption albums use
  `copy_messages` and validate the returned count.
- Retrying a partial sequential delivery can duplicate already copied parts because the
  Bot API has no delivery transaction.
- SQLite is intended for one bot process. Use a server database before running multiple
  replicas.

## Project layout

```text
bot/config.py                       validated bot/import/catalog settings
bot/database/                       registry plus versioned discovery/review schema
bot/services/discovery.py           Telethon/Bot metadata parsing and filtering
bot/services/matching.py            conservative catalog matching
bot/services/pipeline.py            idempotent discovery persistence
bot/services/historical_import.py   forum topics, pagination, FloodWait, checkpoints
bot/services/google_sheets.py       stable IDs and range-precise Sheets updates
bot/services/review.py              approval, grouping, registry, and sync
bot/handlers/review.py              secured Telegram review workflow
bot/handlers/storage.py             future Hyper Vault uploads
scripts/import_group.py              historical import CLI
scripts/sync_catalog.py              explicit ID initialization and sync CLI
scripts/export_links.py              secret-free JSON/CSV export
tests/                               mocked regression and integration tests
```

### Download request forum routing

Set `DOWNLOAD_REQUEST_CHAT_ID` to the numeric group ID and `DOWNLOAD_REQUEST_TOPIC_ID` to the numeric topic ID separately (never a combined `chat_topic` string). Topic 1 targets General by omitting the Bot API thread parameter. Other positive IDs are sent as `message_thread_id`. The bot must be permitted to send to the selected topic. If a send fails, it reports failure to the requester and does not retry into a different topic. No database migration is needed.
