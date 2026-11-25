# Standalone Hyperdash API

The UI is maintained in https://github.com/kassemyahia/Hyperdash. The API lives here and shares the bot's existing database and Google Sheets configuration. No second Telegram poller or shift scheduler is started.

Set HYPERDASH_ALLOWED_ORIGIN to the exact frontend origin (e.g. https://dash.example.com; if GitHub Pages uses https://your-account.github.io/Hyperdash/, the origin is https://your-account.github.io). Set HYPERDASH_PASSWORD to a separate random password of at least 20 characters. Use the same DATABASE_URL and Google/shift configuration as the bot.

Run from the same project directory, alongside the existing bot process:

    python -m uvicorn dashboard.api:create_app --factory --host 127.0.0.1 --port 8765 --workers 1

Expose this process through a trusted HTTPS reverse proxy at your own API domain; do not expose plain HTTP. For local testing, set the allowed origin to http://localhost:8080 and keep the API bound to localhost. A public URL is not anonymous access: all admin routes except login and basic health require a short-lived bearer token. Login attempts are bounded in memory, and tokens are revoked at logout or server restart. Keep one worker; use proxy rate limiting, request-size limits and restricted access (VPN/identity-aware proxy recommended) for internet-facing deployment. Do not log Authorization headers or request bodies. Only trust forwarded client IP headers from your own proxy. API activation is opt-in; the existing local dashboard remains available separately on another port.

Routes (prefix /api/v1):
- GET /health (only API process health, not bot health)
- POST /login, POST /logout
- GET /dashboard (read-only snapshot, up to 100 candidates/records, up to 30 upcoming shifts and reminder history)
- POST /files/link (requires current row_version), /files/create, /files/reconcile

The sheet remains authoritative for shift assignments; no shift mutation routes are exposed. The API serializes only necessary fields, not bot tokens, original vault URLs, service-account credentials, or private chat IDs. File writes reuse existing services and preserve their current limitations: Sheets/SQLite writes are not atomic across systems. Do not retry ambiguous writes blindly; refresh and check server logs first. Multiple processes (bot and API) must share persistent local SQLite storage; don't deploy separate ephemeral copies of the database. Avoid running both local dashboard and API mutations concurrently. The per-process write lock cannot coordinate with the bot or other processes.

Deployment requires the hosting platform to run an HTTP process and the polling worker concurrently or separately with a shared persistent volume. No hosting account has been configured by this code change.
