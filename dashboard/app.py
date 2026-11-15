"""Private file-sync and shift-management web dashboard.

Start with: python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8765
The dashboard deliberately does not start Telegram polling or a second scheduler.
"""
from __future__ import annotations

import os
import secrets
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bot.config import CatalogSettings, Settings, _load_dotenv
from bot.database.repository import SQLiteFileRepository
from bot.services.catalog_reconciliation import reconcile_catalog, source_url
from bot.services.file_registry import FileRegistry
from bot.services.google_sheets import GoogleSheetsCatalog
from bot.services.review import ReviewService
from bot.services.shift_schedule import ShiftStore, day_index, format_reminder

_load_dotenv()
password = os.getenv("DASHBOARD_PASSWORD", "")
secret = os.getenv("DASHBOARD_SESSION_SECRET", "")
if len(password) < 12 or len(secret) < 32:
    raise RuntimeError(
        "Set DASHBOARD_PASSWORD (12+ chars) and DASHBOARD_SESSION_SECRET (32+ chars) "
        "before starting the private dashboard"
    )

app = FastAPI(title="HyperBot Admin Dashboard", docs_url=None, redoc_url=None,
              openapi_url=None)
app.add_middleware(
    SessionMiddleware, secret_key=secret, same_site="strict", max_age=14400,
    https_only=os.getenv("DASHBOARD_SECURE_COOKIES", "false").lower() == "true",
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
LOGIN_FAILURES: dict[str, tuple[int, float]] = {}


def service_context():
    settings = Settings.from_env()
    repo = SQLiteFileRepository(settings.database_path)
    sheets = (GoogleSheetsCatalog(settings.google_sheet_id, settings.google_application_credentials)
              if settings.google_sheet_id else None)
    store = ShiftStore(settings.database_path)
    return settings, repo, sheets, store


def session(request: Request, csrf: str | None = None) -> None:
    if (not request.session.get("auth")
            or time.time() - float(request.session.get("login_at", 0)) > 14400):
        raise HTTPException(status_code=401, detail="Sign in required")
    if csrf is not None and not secrets.compare_digest(
        str(request.session.get("csrf", "")), csrf
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def redirect(message: str = "") -> RedirectResponse:
    return RedirectResponse(url="/?notice=" + quote(message[:180]), status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
def login(request: Request, passphrase: str = Form(...)):
    host = request.client.host if request.client else "unknown"
    strikes, since = LOGIN_FAILURES.get(host, (0, time.monotonic()))
    if time.monotonic() - since > 900:
        strikes, since = 0, time.monotonic()
    if strikes >= 8:
        raise HTTPException(429, "Too many login attempts. Try again later.")
    if not secrets.compare_digest(passphrase, password):
        LOGIN_FAILURES[host] = (strikes + 1, since)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect password"}, status_code=401
        )
    LOGIN_FAILURES.pop(host, None)
    request.session.clear()
    request.session.update({
        "auth": True, "login_at": time.time(), "csrf": secrets.token_urlsafe(24)
    })
    return redirect("Signed in")


@app.post("/logout")
def logout(request: Request, csrf: str = Form(...)):
    session(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, notice: str = ""):
    session(request)
    settings, repo, sheets, store = service_context()
    entries = []
    sheet_error = None
    if sheets:
        try:
            entries = sheets.load_entries()
        except Exception as exc:
            sheet_error = type(exc).__name__
    candidates = []
    for c in repo.list_candidates(limit=100):
        m = repo.get_discovered(c.discovered_message_id)
        if not m:
            continue
        candidates.append({
            "candidate": c, "message": m,
            "source_url": source_url(settings.source_chat_username, m.topic_id, m.message_id)
            if settings.source_chat_username else ""
        })
    today = datetime.now(ZoneInfo("Asia/Damascus")).date()
    shifts = store.upcoming(today, 14)
    return templates.TemplateResponse(request, "dashboard.html", {
        "notice": notice, "csrf": request.session["csrf"],
        "candidates": candidates, "entries": entries,
        "records": repo.list()[:100], "shifts": shifts,
        "weekly": store.get_weekly(), "history": store.history(),
        "today": today, "sheet_error": sheet_error,
        "group_configured": bool(os.getenv("SHIFT_GROUP_CHAT_ID")),
    })


def verified_candidate(repo, settings, candidate_id: int):
    candidate = repo.get_candidate(candidate_id)
    if candidate is None or candidate.status not in ("exact", "ambiguous", "unmatched"):
        raise ValueError("Pending candidate not found")
    message = repo.get_discovered(candidate.discovered_message_id)
    if message is None or message.source_chat_id != settings.source_chat_id:
        raise ValueError("Not a discovery from the configured Hyper Vault")
    if not message.filename and not message.mime_type:
        raise ValueError("Not a file")
    if repo.get_by_source_message(message.source_chat_id, message.message_id):
        raise ValueError("Source message already registered")
    if not settings.source_chat_username:
        raise ValueError("Set SOURCE_CHAT_USERNAME to generate source URLs")
    return candidate, message


@app.post("/files/link")
def link_existing(request: Request, csrf: str = Form(...),
                  candidate_id: int = Form(...), sheet: str = Form(...),
                  row_number: int = Form(...)):
    session(request, csrf)
    settings, repo, sheets, _ = service_context()
    if not sheets:
        return redirect("Google Sheets is not configured")
    try:
        candidate, message = verified_candidate(repo, settings, candidate_id)
        entries = sheets.load_entries()
        targets = [e for e in entries if e.sheet_name == sheet and e.row_number == row_number]
        if len(targets) != 1:
            raise ValueError("Catalog row missing or changed")
        target = targets[0]
        link = source_url(settings.source_chat_username, message.topic_id, message.message_id)
        # Do not steal another registered resource or silently overwrite a link.
        if target.source_type and target.source_type.startswith("https://t.me/") and target.source_type != link:
            raise ValueError("Catalog row has a different Telegram source. Resolve it first.")
        if not target.file_id:
            target = sheets.ensure_entry_file_id(
                sheet_name=sheet, row_number=row_number,
                requested_file_id=f"sw_{secrets.token_hex(12)}" if sheet == "softwares"
                                  else f"course_{secrets.token_hex(12)}",
                expected_title=target.title,
                expected_source_url=target.source_type or "", approved=True,
            )
        sheets.update_source_link(
            sheet_name=sheet, row_number=row_number,
            expected_title=target.title, expected_source_url=target.source_type or "",
            source_url=link,
        )
        fresh = sheets.load_entries()
        review = ReviewService(
            repo, FileRegistry(repo, settings.source_chat_id),
            fresh, settings.bot_username, sheets,
        )
        outcome = review.approve(candidate_id, -1, file_id=target.file_id,
                                 discovered_ids=(message.id,))
        return redirect("File linked; Google sync " +
                        ("verified" if outcome.synchronized else "pending retry"))
    except (ValueError, RuntimeError, LookupError) as exc:
        return redirect(f"Could not link: {type(exc).__name__}: {exc}")


@app.post("/files/create")
def create_file(request: Request, csrf: str = Form(...), candidate_id: int = Form(...),
                sheet: str = Form(...), title: str = Form(...),
                size: str = Form(""), path: str = Form(""),
                category: str = Form(""), details: str = Form("")):
    session(request, csrf)
    settings, repo, sheets, _ = service_context()
    if not sheets:
        return redirect("Google Sheets is not configured")
    try:
        _, message = verified_candidate(repo, settings, candidate_id)
        source = source_url(settings.source_chat_username, message.topic_id, message.message_id)
        file_id = ("sw_" if sheet == "softwares" else "course_") + secrets.token_hex(12)
        sheets.append_catalog_entry(
            sheet, file_id=file_id, title=title.strip(), size=size.strip(),
            path=path.strip(), category=category.strip(), source_url=source,
            details=details.strip(),
        )
        fresh = sheets.load_entries()
        review = ReviewService(
            repo, FileRegistry(repo, settings.source_chat_id),
            fresh, settings.bot_username, sheets,
        )
        outcome = review.approve(candidate_id, -1, file_id=file_id,
                                 discovered_ids=(message.id,))
        return redirect("Created catalog row; sync " +
                        ("verified" if outcome.synchronized else "pending retry"))
    except (ValueError, RuntimeError, LookupError) as exc:
        return redirect(f"Could not create: {type(exc).__name__}: {exc}")


@app.post("/files/reconcile")
def reconcile(request: Request, csrf: str = Form(...)):
    session(request, csrf)
    settings, repo, sheets, _ = service_context()
    if not sheets or not settings.source_chat_username:
        return redirect("Configure Google Sheets and SOURCE_CHAT_USERNAME")
    result = reconcile_catalog(
        repo, sheets, source_chat_id=settings.source_chat_id,
        source_username=settings.source_chat_username,
        bot_username=settings.bot_username, auto_register=True,
    )
    return redirect("Reconciliation: " + str(result))


@app.post("/shifts/weekly")
def save_weekly(request: Request, csrf: str = Form(...), day: int = Form(...),
                coach: str = Form(...), co_coach: str = Form(...),
                member1: str = Form(...), member2: str = Form(...),
                member3: str = Form(...), member4: str = Form(...)):
    session(request, csrf)
    _, _, _, store = service_context()
    try:
        store.set_weekly(day, [coach, co_coach, member1, member2, member3, member4])
        return redirect("Weekly shift saved")
    except ValueError as exc:
        return redirect(str(exc))


@app.post("/shifts/override")
def save_override(request: Request, csrf: str = Form(...), shift_date: date = Form(...),
                  coach: str = Form(...), co_coach: str = Form(...),
                  member1: str = Form(...), member2: str = Form(...),
                  member3: str = Form(...), member4: str = Form(...)):
    session(request, csrf)
    _, _, _, store = service_context()
    try:
        store.set_override(shift_date, [coach, co_coach, member1, member2, member3, member4])
        return redirect("Date override saved")
    except ValueError as exc:
        return redirect(str(exc))


@app.post("/shifts/cancel")
def cancel_shift(request: Request, csrf: str = Form(...),
                 shift_date: date = Form(...), cancel: bool = Form(True)):
    session(request, csrf)
    _, _, _, store = service_context()
    try:
        (store.cancel if cancel else store.uncancel)(shift_date)
        return redirect("Shift cancellation updated")
    except ValueError as exc:
        return redirect(str(exc))
