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
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from bot.config import CatalogSettings, Settings, _load_dotenv
from bot.database.repository import SQLiteFileRepository
from bot.services.catalog_reconciliation import reconcile_catalog, source_url
from bot.services.file_registry import FileRegistry
from bot.services.google_sheets import GoogleSheetsCatalog
from bot.services.review import ReviewService
from bot.services.shift_schedule import ShiftStore, Shift, day_index, format_reminder
from bot.services.shift_sheet import SheetShiftSchedule
from dashboard.operations import service_context, snapshot, link_candidate, create_candidate, reconcile_files

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
app.mount("/static/dashboard", StaticFiles(directory=str(Path(__file__).parent / "static")), name="dashboard-static")
LOGIN_FAILURES: dict[str, tuple[int, float]] = {}




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


def require_local_shift_mode() -> None:
    """Prevent local shift writes while the Google Sheet is authoritative."""
    _load_dotenv()
    if os.getenv("SHIFT_SHEET_ID", "").strip():
        raise HTTPException(
            status_code=409,
            detail="Shift assignments are read-only here; edit the configured Google Sheet",
        )


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
    return templates.TemplateResponse(request, "dashboard.html", {
        **snapshot(*service_context()), "notice": notice, "csrf": request.session["csrf"],
    })


@app.post("/files/link")
def link_existing(request: Request, csrf: str = Form(...),
                  candidate_id: int = Form(...), sheet: str = Form(...),
                  row_number: int = Form(...)):
    session(request, csrf)
    settings, repo, sheets, _ = service_context()
    try:
        result = link_candidate(settings, repo, sheets, candidate_id, sheet, row_number)
        return redirect("File linked; Google sync " + ("verified" if result["synchronized"] else "pending retry"))
    except (ValueError, RuntimeError, LookupError) as exc:
        return redirect(f"Could not link: {type(exc).__name__}: {exc}")

@app.post("/files/create")
def create_file(request: Request, csrf: str = Form(...), candidate_id: int = Form(...),
                sheet: str = Form(...), title: str = Form(...),
                size: str = Form(""), path: str = Form(""),
                category: str = Form(""), details: str = Form("")):
    session(request, csrf)
    settings, repo, sheets, _ = service_context()
    try:
        result = create_candidate(settings, repo, sheets, candidate_id, sheet, title, size, path, category, details)
        return redirect("Created catalog row; sync " + ("verified" if result["synchronized"] else "pending retry"))
    except (ValueError, RuntimeError, LookupError) as exc:
        return redirect(f"Could not create: {type(exc).__name__}: {exc}")

@app.post("/files/reconcile")
def reconcile(request: Request, csrf: str = Form(...)):
    session(request, csrf)
    settings, repo, sheets, _ = service_context()
    try:
        return redirect("Reconciliation: " + str(reconcile_files(settings, repo, sheets)))
    except (ValueError, RuntimeError, LookupError):
        return redirect("Reconciliation failed; check the server logs")

@app.post("/shifts/weekly")
def save_weekly(request: Request, csrf: str = Form(...), day: int = Form(...),
                coach: str = Form(...), co_coach: str = Form(...),
                member1: str = Form(...), member2: str = Form(...),
                member3: str = Form(...), member4: str = Form(...)):
    session(request, csrf)
    require_local_shift_mode()
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
    require_local_shift_mode()
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
    require_local_shift_mode()
    _, _, _, store = service_context()
    try:
        (store.cancel if cancel else store.uncancel)(shift_date)
        return redirect("Shift cancellation updated")
    except ValueError as exc:
        return redirect(str(exc))
