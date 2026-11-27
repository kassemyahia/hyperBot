"""Opt-in HTTPS admin API for Hyperdash. Run as one worker beside the bot."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import deque
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from bot.config import _load_dotenv
from dashboard import operations as ops

logger = logging.getLogger(__name__)
PREFIX = "/api/v1"
SESSION_SECONDS = 3600


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Payload):
    password: SecretStr = Field(max_length=512)


class Link(Payload):
    candidate_id: int = Field(gt=0)
    sheet: Literal["softwares", "courses"]
    row_number: int = Field(ge=2)
    row_version: str = Field(pattern=r"^[a-f0-9]{64}$")


class Create(Payload):
    candidate_id: int = Field(gt=0)
    sheet: Literal["softwares", "courses"]
    title: str = Field(min_length=1, max_length=200)
    size: str = Field(default="", max_length=80)
    path: str = Field(default="", max_length=250)
    category: str = Field(default="", max_length=100)
    details: str = Field(default="", max_length=1500)


class CatalogAdd(Payload):
    sheet: Literal["softwares", "courses"]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(min_length=1, max_length=200)
    size: str = Field(default="", max_length=80)
    path: str = Field(default="", max_length=250)
    category: str = Field(default="", max_length=100)
    source_url: str = Field(default="", max_length=2000)
    details: str = Field(default="", max_length=1200)


def create_app():
    _load_dotenv()
    origin = os.getenv("HYPERDASH_ALLOWED_ORIGIN", "").strip()
    password = os.getenv("HYPERDASH_PASSWORD", "")
    url = urlsplit(origin)
    loopback = url.hostname in ("localhost", "127.0.0.1", "::1")
    if (not url.netloc or url.username or url.password or url.path or url.query or url.fragment
            or not (url.scheme == "https" or (loopback and url.scheme == "http"))
            or "*" in origin):
        raise RuntimeError("Set HYPERDASH_ALLOWED_ORIGIN to one exact HTTPS origin (HTTP loopback for development)")
    if not 20 <= len(password) <= 512:
        raise RuntimeError("Set a unique HYPERDASH_PASSWORD of 20-512 characters")

    app = FastAPI(title="Hyperdash API", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=[origin], allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Never echo malformed login passwords or other submitted input.
        return JSONResponse({"detail": "Invalid request payload"}, status_code=422)

    guard = threading.Lock()
    writes = threading.Lock()
    sessions: dict[str, float] = {}
    attempts: deque[tuple[float, str]] = deque()
    requests: deque[float] = deque()

    @app.middleware("http")
    async def secure_response(request: Request, call_next):
        supplied = request.headers.get("origin")
        if supplied is not None and supplied != origin:
            response = JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        elif request.method == "POST" and (
            request.headers.get("content-type", "").split(";")[0] != "application/json"
            and request.url.path != PREFIX + "/logout"
        ):
            response = JSONResponse({"detail": "JSON required"}, status_code=415)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def authorize(authorization: str = Header(default="")) -> str:
        if not authorization.startswith("Bearer ") or len(authorization) > 100:
            raise HTTPException(401, "Sign in required")
        key = digest(authorization[7:])
        now = time.monotonic()
        with guard:
            if sessions.get(key, 0) <= now:
                sessions.pop(key, None)
                raise HTTPException(401, "Session expired")
            while requests and now - requests[0] > 60:
                requests.popleft()
            if len(requests) >= 120:
                raise HTTPException(429, "Too many requests")
            requests.append(now)
        return key

    @app.get(PREFIX + "/health")
    def health():
        return {"api_version": 1, "status": "ok"}

    @app.post(PREFIX + "/login")
    def login(request: Request, payload: Login):
        host = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with guard:
            while attempts and now - attempts[0][0] > 900:
                attempts.popleft()
            if len(attempts) >= 60 or sum(ip == host for _, ip in attempts) >= 8:
                raise HTTPException(429, "Too many login attempts")
            attempts.append((now, host))
            if not hmac.compare_digest(payload.password.get_secret_value().encode(), password.encode()):
                raise HTTPException(401, "Incorrect password")
            for key in list(sessions):
                if sessions[key] <= now:
                    sessions.pop(key)
            if len(sessions) >= 100:
                raise HTTPException(429, "Too many active sessions")
            token = secrets.token_urlsafe(32)
            sessions[digest(token)] = now + SESSION_SECONDS
        return {"access_token": token, "token_type": "Bearer", "expires_in": SESSION_SECONDS}

    @app.post(PREFIX + "/logout")
    def logout(key: str = Depends(authorize)):
        with guard:
            sessions.pop(key, None)
        return {"ok": True}

    @app.get(PREFIX + "/dashboard")
    def dashboard(_key: str = Depends(authorize)):
        data = ops.snapshot(*ops.service_context())
        return {
            "today": data["today"],
            "candidates": [{
                "id": item["candidate"].id, "status": item["candidate"].status,
                "suggested_title": item["candidate"].website_title,
                "filename": item["message"].filename,
                "topic": item["message"].topic_title,
                "size": item["message"].file_size,
            } for item in data["candidates"]],
            "catalog": [{
                "sheet": e.sheet_name, "row": e.row_number, "title": e.title,
                "file_id": e.file_id, "size": e.size, "category": e.category,
                "status": e.telegram_status, "row_version": ops.row_version(e),
            } for e in data["entries"]],
            "records": [{"title": r.title, "file_id": r.website_id, "active": r.active}
                        for r in data["records"]],
            "shifts": data["shifts"],
            "history": [{"date": h["date"], "status": h["status"], "attempted_at": h["attempted_at"]}
                        for h in data["history"]],
            "shift_sheet_enabled": data["shift_sheet_enabled"],
            "shift_sheet_url": data["shift_sheet_url"],
            "group_configured": data["group_configured"],
            "catalog_error": bool(data["sheet_error"]),
            "catalog_add_supported": True,
            "shift_error": bool(data["shift_sheet_error"]),
        }

    def mutation(operation, *args):
        if not writes.acquire(blocking=False):
            raise HTTPException(409, "Another file operation is in progress")
        try:
            settings, repo, sheets, _ = ops.service_context()
            return operation(settings, repo, sheets, *args)
        except ValueError:
            logger.warning("Hyperdash file operation rejected")
            raise HTTPException(409, "Operation rejected; refresh the dashboard and verify the selected row")
        except Exception:
            logger.exception("Hyperdash file operation failed")
            raise HTTPException(503, "Operation failed; refresh and inspect server logs before retrying")
        finally:
            writes.release()

    @app.post(PREFIX + "/files/link")
    def link(payload: Link, _key: str = Depends(authorize)):
        return mutation(ops.link_candidate, payload.candidate_id, payload.sheet,
                        payload.row_number, payload.row_version)

    @app.post(PREFIX + "/files/create")
    def create(payload: Create, _key: str = Depends(authorize)):
        return mutation(ops.create_candidate, payload.candidate_id, payload.sheet,
                        payload.title, payload.size, payload.path, payload.category, payload.details)

    @app.post(PREFIX + "/catalog/add")
    def catalog_add(payload: CatalogAdd, _key: str = Depends(authorize)):
        return mutation(ops.add_catalog_entry, payload.sheet, payload.request_id,
                        payload.title, payload.size, payload.path, payload.category,
                        payload.source_url, payload.details)

    @app.post(PREFIX + "/files/reconcile")
    def reconcile(_key: str = Depends(authorize)):
        return mutation(ops.reconcile_files)

    return app
