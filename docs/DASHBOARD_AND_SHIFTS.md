# HyperBot private management dashboard and shift reminders

This feature is developed on `feature/admin-dashboard-shifts`, not production `main`.
Existing `file_records`, public tokens, source messages and the published website are not modified by deployment setup.

## Install

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Generate a password and session secret, then add these to your **local, ignored** `.env`:

```dotenv
DASHBOARD_PASSWORD=choose-a-long-unique-password
DASHBOARD_SESSION_SECRET=generate-a-random-32-plus-character-value
DASHBOARD_SECURE_COOKIES=false
SOURCE_CHAT_USERNAME=Hyper_Vault
SHIFT_GROUP_CHAT_ID=
SHIFT_TIMEZONE=Asia/Damascus
CATALOG_RECONCILE_ENABLED=false
CATALOG_AUTO_REGISTER=false
```

Use `python -c 'import secrets; print(secrets.token_urlsafe(48))'` to generate a session secret. Never commit the secret. Keep `DASHBOARD_SECURE_COOKIES=true` when serving over HTTPS in production. The dashboard uses signed 4-hour sessions, same-site cookies, per-session CSRF tokens, login attempt throttling, and a mandatory 12+ character password. Do not publish it behind an unauthenticated proxy or expose it without HTTPS.

## Start the dashboard and bot

In two separate terminal sessions, both from the repo root and with the same `.env` and persistent SQLite volume:

```bash
python app.py
```

```bash
python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765 locally. The dashboard is web-only; it does **not** start an extra Telegram poller or scheduled sender. The existing bot process owns both periodic jobs. To access remotely, use a properly authenticated HTTPS reverse proxy with firewall restrictions, and set `DASHBOARD_SECURE_COOKIES=true`.

## Files — review and publish

1. Upload media to a supported Hyper Vault topic while the bot is running. New messages enter the SQLite discovery queue.
2. Open **Pending uploads** in the private dashboard.
3. Choose **Link existing** and supply the correct spreadsheet tab/row, or **Create new** to append a new catalog item. Review file identity and source carefully.
4. The dashboard preserves existing stable IDs, appends only authorized rows, associates the source message with exactly one registry record and publishes the bot deep link in `Telegram Link`, with `Telegram Status=available`.
5. If Google Sheets succeeds but the SQLite approval fails, correct the issue and retry; if the SQLite approval succeeds but Sheets fails, use reconciliation to repair the link.
6. Use `/file_test File-ID` in your personal admin chat to confirm actual private file delivery.

The sheet contains `File Name`, `Size`, `Path`, `Category`, `Source Type`, `Details`, `File ID`, `Telegram Link`, `Telegram Status`. Original message links live in `Source Type`, and are never used as public download links.

For a row to register automatically after its `Source Type` URL is added, set:

```dotenv
CATALOG_RECONCILE_ENABLED=true
CATALOG_AUTO_REGISTER=true
```

The bot then checks every 5 minutes. Only exact, unique, configured-source message links are accepted; filename similarity alone never triggers unattended publication. Automatic reconciliation requires the source message to have been discovered already by the bot or the historical importer. If the bot was offline during upload, run `python -m scripts.import_group --full-scan --topic-id <id>` once. Unmatched, duplicated and unverified sources remain unavailable. The automatic publishing policy is opt-in because editing the Google Sheet then becomes sufficient authorization to publish the selected source.

## Shifts — Sundays through Thursdays

1. Create a separate Telegram group for shift notifications and add HyperBot with permission to post messages.
2. Set its numeric chat ID as `SHIFT_GROUP_CHAT_ID=-100...` in the bot's `.env`.
3. Restart the bot so it installs the reminder job.
4. From the dashboard, fill the 6 weekly roles for each day: coach, co-coach, 4 members. Each username is 5–32 Telegram-compatible characters and must be unique within its shift.
5. Add date-specific overrides and cancellations in the dashboard as needed.

Schedule: **21:00 Asia/Damascus the day before the shift**. This means Saturday evening for Sunday, through Wednesday evening for Thursday. The scheduler checks once per minute and has a 15-minute send window, skips incomplete/cancelled days, and records sends in SQLite. To avoid duplicate messages after uncertain Telegram network errors, those attempts are marked `uncertain` and **not** automatically retried. Check the shift group and decide manually if necessary.

Telegram `@username` mentions work for users who have usernames. Names can change; verify membership and usernames before relying on mentions. The bot must stay online at reminder time; a powered-off laptop cannot execute jobs. Use an always-on service with a persistent SQLite disk and exactly **one** Telegram polling/scheduler instance.

## Operational caveats

- Back up SQLite and Google Sheets before enabling production writes or deploying migrations.
- The website remains separate and does not need a new repository or API.
- Don't publish the dashboard to GitHub Pages; it includes private administrative operations.
- Spreadsheet append is not transactional with SQLite. On ambiguous API failures, verify the row before retrying.
- A file existing in Telethon's history does not prove the bot currently has permission to copy it. Run `/file_test`.
- Setting `AUTO_APPROVE_EXACT_MATCHES=true` is independent from the new catalog reconciliation policy.
- The dashboard currently authenticates using a private administrator passphrase, not Telegram OAuth.
- Dashboard refresh is manual (reload); a future version can add secure websocket updates and a richer calendar UI.

## Tests

```bash
python -m pytest -q
```

Tests cover shift validation, overrides, cancellation, schedule timing, reminder deduplication, uncertainty handling, and source URL reconciliation.
