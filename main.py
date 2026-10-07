# main.py
# =============================================================================
# Single-file backend: FastAPI + PostgreSQL + Redis + aiogram 3 + Selenium.
#
# NEW in this version:
#   - Credentials saved to DB (hosting_email, hosting_password)
#   - After Start, checks Pella Files tab for .db files (30s wait)
#   - /download-db command: manually export user's SQLite DB
#   - Auto-export .db 30 min before expiry
#   - Per-deployment Chrome download directory
# =============================================================================

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html as _html_esc
import io
import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import asyncpg
import redis.asyncio as aioredis
import undetected_chromedriver as uc
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
)
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException as SelTimeout,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
def _env(key: str, default: Optional[str] = None, required: bool = False) -> str:
    v = os.environ.get(key, default)
    if required and not v:
        raise RuntimeError(f"Missing required env var: {key}")
    return v or ""

DATABASE_URL       = _env("DATABASE_URL", required=True)
REDIS_URL          = _env("REDIS_URL", required=True)
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", required=True)
OWNER_TELEGRAM_ID  = int(_env("OWNER_TELEGRAM_ID", "0") or 0)
PUBLIC_BASE_URL    = _env("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

STORAGE_ROOT       = Path(_env("STORAGE_ROOT", "./storage")).resolve()
MAX_ZIP_BYTES      = int(_env("MAX_ZIP_BYTES", str(50 * 1024 * 1024)))
MAX_EXTRACT_BYTES  = int(_env("MAX_EXTRACT_BYTES", str(200 * 1024 * 1024)))
MAX_FILE_COUNT     = int(_env("MAX_FILE_COUNT", "2000"))
SESSION_TTL_SEC    = int(_env("SESSION_TTL_SEC", str(60 * 60 * 12)))
DEPLOYMENT_TTL     = timedelta(hours=24)

PELLA_SIGNUP_URL   = _env("PELLA_SIGNUP_URL", "https://www.pella.app/signup")
PELLA_NEW_URL      = _env("PELLA_NEW_URL",    "https://www.pella.app/new")
TEMPMAIL_URL       = "https://temp-mail.org/en/"
CHROME_HEADLESS    = _env("CHROME_HEADLESS", "false").lower() == "true"

SERVER_READY_WAIT  = int(_env("SERVER_READY_WAIT", "25"))
FILE_CLEANUP_WAIT  = int(_env("FILE_CLEANUP_WAIT", "12"))
CREATE_WAIT_MAX    = int(_env("CREATE_WAIT_MAX", "240"))
DB_CHECK_WAIT      = int(_env("DB_CHECK_WAIT", "30"))

STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
(STORAGE_ROOT / "deployments").mkdir(parents=True, exist_ok=True)
(STORAGE_ROOT / "pella_profiles").mkdir(parents=True, exist_ok=True)

DEP_ID_RE = re.compile(r"^DEP-\d{8}-\d{6}$")


BRAND = {
    "provider": "Hostinger",
    "specs": [
        "4 vCPU cores",
        "8 GB RAM",
        "100 GB NVMe storage",
        "8 TB monthly bandwidth",
    ],
}


def esc(s: Any) -> str:
    return _html_esc.escape(str(s if s is not None else ""))


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
class _JsonFmt(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": r.levelname,
            "event": getattr(r, "event", r.name),
            "message": r.getMessage(),
            "deployment_id": getattr(r, "deployment_id", None),
            "user_id": getattr(r, "user_id", None),
            "operation_id": getattr(r, "operation_id", None),
        }
        return json.dumps({k: v for k, v in payload.items() if v is not None})

_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(_JsonFmt())
_root = logging.getLogger()
_root.handlers[:] = [_h]
_root.setLevel(logging.INFO)
log = logging.getLogger("app")

_REDACT_KEYS = re.compile(
    r"(password|otp|token|secret|cookie|authorization|api[_-]?key)", re.I,
)


def redact(text: str, max_len: int = 2000) -> str:
    if not text:
        return ""
    out = []
    for line in str(text).splitlines()[:200]:
        out.append("[redacted]" if _REDACT_KEYS.search(line) else line)
    return "\n".join(out)[:max_len]


# -----------------------------------------------------------------------------
# State machine
# -----------------------------------------------------------------------------
class S:
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PENDING_REVIEW = "PENDING_REVIEW"
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    ACCOUNT_CREATING = "ACCOUNT_CREATING"
    ACCOUNT_READY = "ACCOUNT_READY"
    HOST_CREATING = "HOST_CREATING"
    HOST_READY = "HOST_READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    RESTARTING = "RESTARTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"
    EXPIRING = "EXPIRING"
    DELETING = "DELETING"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"
    CLEANUP_FAILED = "CLEANUP_FAILED"

TERMINAL = {S.EXPIRED, S.DELETED, S.VALIDATION_FAILED}

ALLOWED: dict[str, set[str]] = {
    S.CREATED:          {S.VALIDATING, S.VALIDATION_FAILED},
    S.VALIDATING:       {S.PENDING_REVIEW, S.QUEUED, S.VALIDATION_FAILED},
    S.PENDING_REVIEW:   {S.QUEUED, S.VALIDATION_FAILED},
    S.QUEUED:           {S.PROVISIONING, S.ERROR, S.VALIDATION_FAILED},
    S.PROVISIONING:     {S.ACCOUNT_CREATING, S.HOST_CREATING, S.HOST_READY,
                         S.STARTING, S.RUNNING, S.ERROR, S.UNKNOWN,
                         S.DELETING, S.EXPIRING, S.STOPPING, S.STOPPED,
                         S.VALIDATION_FAILED},
    S.ACCOUNT_CREATING: {S.ACCOUNT_READY, S.HOST_CREATING, S.HOST_READY,
                         S.STARTING, S.RUNNING, S.ERROR, S.UNKNOWN,
                         S.DELETING, S.EXPIRING, S.STOPPING, S.STOPPED,
                         S.VALIDATION_FAILED},
    S.ACCOUNT_READY:    {S.HOST_CREATING, S.HOST_READY, S.STARTING, S.RUNNING,
                         S.ERROR, S.UNKNOWN, S.DELETING, S.EXPIRING,
                         S.STOPPING, S.STOPPED, S.VALIDATION_FAILED},
    S.HOST_CREATING:    {S.HOST_READY, S.STARTING, S.RUNNING,
                         S.ERROR, S.UNKNOWN, S.DELETING, S.EXPIRING,
                         S.STOPPING, S.STOPPED, S.VALIDATION_FAILED},
    S.HOST_READY:       {S.STARTING, S.RUNNING, S.STOPPING, S.STOPPED,
                         S.ERROR, S.UNKNOWN, S.DELETING, S.EXPIRING,
                         S.VALIDATION_FAILED},
    S.STARTING:         {S.RUNNING, S.ERROR, S.UNKNOWN,
                         S.DELETING, S.EXPIRING, S.STOPPED, S.STOPPING},
    S.RUNNING:          {S.RESTARTING, S.STOPPING, S.ERROR, S.UNKNOWN,
                         S.EXPIRING, S.DELETING, S.STOPPED},
    S.RESTARTING:       {S.RUNNING, S.ERROR, S.UNKNOWN,
                         S.DELETING, S.EXPIRING, S.STOPPED, S.STOPPING},
    S.STOPPING:         {S.STOPPED, S.RUNNING, S.ERROR, S.UNKNOWN,
                         S.DELETING, S.EXPIRING},
    S.STOPPED:          {S.STARTING, S.RUNNING, S.DELETING, S.EXPIRING,
                         S.ERROR, S.UNKNOWN},
    S.EXPIRING:         {S.STOPPING, S.DELETING, S.EXPIRED,
                         S.CLEANUP_FAILED, S.ERROR, S.UNKNOWN},
    S.DELETING:         {S.DELETED, S.EXPIRED, S.CLEANUP_FAILED,
                         S.ERROR, S.UNKNOWN},
    S.EXPIRED:          set(),
    S.DELETED:          set(),
    S.ERROR:            {S.PROVISIONING, S.DELETING, S.EXPIRING,
                         S.STOPPING, S.STOPPED, S.RUNNING, S.ERROR,
                         S.UNKNOWN, S.VALIDATION_FAILED, S.DELETED},
    S.UNKNOWN:          {S.PROVISIONING, S.STARTING, S.STOPPING,
                         S.DELETING, S.EXPIRING, S.UNKNOWN, S.ERROR,
                         S.STOPPED, S.DELETED},
}


# -----------------------------------------------------------------------------
# Globals
# -----------------------------------------------------------------------------
_pg: Optional[asyncpg.Pool] = None
_redis: Optional[aioredis.Redis] = None
_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_shutdown = asyncio.Event()
_signup_progress: dict[str, str] = {}
_driver_lock = threading.Lock()
_main_loop: Optional[asyncio.AbstractEventLoop] = None


# =============================================================================
# DATABASE
# =============================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id      BIGINT PRIMARY KEY,
    username         TEXT,
    first_name       TEXT,
    suspended        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS deployments (
    deployment_id    TEXT PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    project_name     TEXT NOT NULL,
    description      TEXT NOT NULL,
    state            TEXT NOT NULL,
    state_reason     TEXT,
    expires_at       TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_confirmed_status TEXT,
    last_confirmed_at     TIMESTAMPTZ,
    hosting_account_reference  TEXT,
    hosting_project_reference  TEXT,
    hosting_instance_reference TEXT
);
CREATE INDEX IF NOT EXISTS idx_deployments_user ON deployments(user_id);
CREATE INDEX IF NOT EXISTS idx_deployments_expires ON deployments(expires_at);
CREATE TABLE IF NOT EXISTS deployment_files (
    deployment_id  TEXT PRIMARY KEY REFERENCES deployments(deployment_id) ON DELETE CASCADE,
    original_path  TEXT NOT NULL,
    filename       TEXT NOT NULL,
    size_bytes     BIGINT NOT NULL,
    sha256         TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS deployment_events (
    id             BIGSERIAL PRIMARY KEY,
    deployment_id  TEXT NOT NULL REFERENCES deployments(deployment_id) ON DELETE CASCADE,
    event          TEXT NOT NULL,
    old_state      TEXT,
    new_state      TEXT,
    message        TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS deployment_logs (
    id             BIGSERIAL PRIMARY KEY,
    deployment_id  TEXT NOT NULL REFERENCES deployments(deployment_id) ON DELETE CASCADE,
    level          TEXT NOT NULL,
    message        TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS security_reviews (
    deployment_id  TEXT PRIMARY KEY REFERENCES deployments(deployment_id) ON DELETE CASCADE,
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'PENDING',
    decided_by     BIGINT,
    decided_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS admins (
    telegram_id  BIGINT PRIMARY KEY,
    role         TEXT NOT NULL CHECK (role IN ('OWNER','SUPER_ADMIN','MODERATOR','SUPPORT')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    session_id   TEXT PRIMARY KEY,
    telegram_id  BIGINT NOT NULL,
    csrf_token   TEXT NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id           BIGSERIAL PRIMARY KEY,
    telegram_id  BIGINT,
    action       TEXT NOT NULL,
    target       TEXT,
    details      JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Migration: add new columns idempotently
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS hosting_email TEXT;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS hosting_password TEXT;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS has_db_file BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS db_file_names TEXT;
"""


async def db_init() -> None:
    global _pg
    _pg = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with _pg.acquire() as c:
        # Execute schema line-by-line to tolerate ADD COLUMN IF NOT EXISTS
        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            try:
                await c.execute(stmt)
            except Exception as e:
                log.warning("schema stmt failed (may be benign): %s", str(e)[:120])


def db() -> asyncpg.Pool:
    assert _pg is not None
    return _pg


async def redis_init() -> None:
    global _redis
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)


def r() -> aioredis.Redis:
    assert _redis is not None
    return _redis


async def record_event(dep_id: str, event: str, old_state, new_state, message=""):
    try:
        async with db().acquire() as c:
            await c.execute(
                "INSERT INTO deployment_events(deployment_id,event,old_state,"
                "new_state,message) VALUES($1,$2,$3,$4,$5)",
                dep_id, event, old_state, new_state, redact(message),
            )
    except Exception as e:
        log.warning("record_event failed: %s", e)


async def record_log(dep_id: str, level: str, message: str):
    lvl = {"ERROR": logging.ERROR, "WARN": logging.WARNING,
           "INFO": logging.INFO, "DEBUG": logging.DEBUG}.get(level, logging.INFO)
    log.log(lvl, "[%s] %s", dep_id, message)
    try:
        async with db().acquire() as c:
            await c.execute(
                "INSERT INTO deployment_logs(deployment_id,level,message)"
                " VALUES($1,$2,$3)",
                dep_id, level, redact(message),
            )
    except Exception as e:
        log.warning("record_log failed: %s", e)


async def transition(dep_id: str, new_state: str, reason: str = "",
                     *, force: bool = False):
    async with db().acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "SELECT state FROM deployments WHERE deployment_id=$1 FOR UPDATE",
                dep_id,
            )
            if not row:
                raise RuntimeError(f"deployment {dep_id} not found")
            old = row["state"]
            if old == new_state and not force:
                return
            if not force and new_state not in ALLOWED.get(old, set()):
                raise RuntimeError(f"illegal transition {old} -> {new_state}")
            await c.execute(
                "UPDATE deployments SET state=$1, state_reason=$2,"
                " updated_at=NOW() WHERE deployment_id=$3",
                new_state, reason[:500], dep_id,
            )
    await record_event(dep_id, "state_change", old, new_state, reason)


async def deploy_get(dep_id: str):
    async with db().acquire() as c:
        return await c.fetchrow(
            "SELECT * FROM deployments WHERE deployment_id=$1", dep_id)


# =============================================================================
# Redis queue
# =============================================================================
QUEUE_KEY = "jobs:queue"


async def enqueue(job_type: str, deployment_id: str, **payload):
    job = {"id": uuid.uuid4().hex, "type": job_type,
           "deployment_id": deployment_id, "payload": payload,
           "ts": time.time()}
    await r().lpush(QUEUE_KEY, json.dumps(job))


# =============================================================================
# ZIP validation
# =============================================================================
class ZipValidationError(Exception):
    pass


def validate_zip_bytes(blob: bytes) -> dict:
    if len(blob) > MAX_ZIP_BYTES:
        raise ZipValidationError("zip too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise ZipValidationError(f"not a valid zip: {e}") from e

    names, total_u, total_c = [], 0, 0
    has_main = has_reqs = False
    seen: set[str] = set()

    for info in zf.infolist():
        name = info.filename
        if info.flag_bits & 0x1:
            raise ZipValidationError("encrypted zip not allowed")
        if name.endswith("/"):
            continue
        pure = Path(name)
        if pure.is_absolute() or name.startswith(("/", "\\")):
            raise ZipValidationError(f"absolute path: {name}")
        if re.match(r"^[a-zA-Z]:", name):
            raise ZipValidationError(f"windows drive path: {name}")
        if ".." in pure.parts:
            raise ZipValidationError(f"path traversal: {name}")
        if (info.external_attr >> 16) & 0xF000 == 0xA000:
            raise ZipValidationError(f"symlink not allowed: {name}")
        if name in seen:
            raise ZipValidationError(f"duplicate: {name}")
        seen.add(name)

        if "/" not in name.rstrip("/"):
            if name == "main.py":
                has_main = True
            if name == "requirements.txt":
                has_reqs = True

        total_u += info.file_size
        total_c += max(1, info.compress_size)
        names.append(name)
        if len(names) > MAX_FILE_COUNT:
            raise ZipValidationError("too many files")
        if total_u > MAX_EXTRACT_BYTES:
            raise ZipValidationError("extracted size too large")

    if not has_main:
        raise ZipValidationError("root main.py missing")
    if not has_reqs:
        raise ZipValidationError("root requirements.txt missing")
    if total_u / max(1, total_c) > 200:
        raise ZipValidationError("suspicious compression ratio")

    return {"file_count": len(names), "size_compressed": len(blob)}


def store_original_zip(dep_id: str, filename: str, blob: bytes) -> dict:
    d = STORAGE_ROOT / "deployments" / dep_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "original.zip"
    p.write_bytes(blob)
    return {"path": str(p), "filename": filename, "size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest()}


def new_deployment_id() -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"DEP-{day}-{secrets.randbelow(10**6):06d}"


async def allocate_deployment_id() -> str:
    for _ in range(20):
        did = new_deployment_id()
        async with db().acquire() as c:
            if not await c.fetchval(
                "SELECT 1 FROM deployments WHERE deployment_id=$1", did):
                return did
    raise RuntimeError("could not allocate deployment id")


# =============================================================================
# Security review
# =============================================================================
SUSPICIOUS = [
    r"\bddos\b", r"\bphish", r"\bscam\b", r"\bcarding\b", r"\bbotnet\b",
    r"\bstealer\b", r"\bransom", r"\bexploit\b",
    r"crack(ed)? (account|password|license)", r"\bcredential.?stuff",
]


def screen_description(text: str) -> Optional[str]:
    low = text.lower()
    for pat in SUSPICIOUS:
        if re.search(pat, low):
            return f"matched review pattern: {pat}"
    return None


# =============================================================================
# Telegram helpers
# =============================================================================
def is_admin_role(role: Optional[str]) -> bool:
    return role in {"OWNER", "SUPER_ADMIN", "MODERATOR", "SUPPORT"}


async def get_admin_role(tg_id: int) -> Optional[str]:
    if tg_id == OWNER_TELEGRAM_ID:
        return "OWNER"
    async with db().acquire() as c:
        return await c.fetchval(
            "SELECT role FROM admins WHERE telegram_id=$1", tg_id)


async def audit(actor, action: str, target: str = "", details=None):
    try:
        async with db().acquire() as c:
            await c.execute(
                "INSERT INTO admin_audit_logs(telegram_id,action,target,details)"
                " VALUES($1,$2,$3,$4)",
                actor, action, target, json.dumps(details or {}),
            )
    except Exception as e:
        log.warning("audit failed: %s", e)


async def notify_user(tg_id: int, text: str, parse_mode: Optional[str] = None):
    if not _bot:
        return
    try:
        await _bot.send_message(tg_id, text, parse_mode=parse_mode)
    except Exception as e:
        log.warning("notify_user(%s) failed: %s", tg_id, e)


async def notify_owner(text: str):
    if OWNER_TELEGRAM_ID:
        await notify_user(OWNER_TELEGRAM_ID, text, parse_mode="HTML")


def _notify_owner_threadsafe(text: str, timeout: float = 8.0) -> bool:
    if not _main_loop or not _bot or not OWNER_TELEGRAM_ID:
        log.warning("owner notify skipped (no loop/bot/owner)")
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _bot.send_message(OWNER_TELEGRAM_ID, text, parse_mode="HTML"),
            _main_loop,
        )
        fut.result(timeout=timeout)
        log.info("owner notified (threadsafe)")
        return True
    except Exception as e:
        log.warning("owner notify (threadsafe) failed: %s", e)
        return False


# =============================================================================
# Progress reporting
# =============================================================================
def _set_progress(dep_id: str, text: str) -> None:
    _signup_progress[dep_id] = text


async def progress(dep_id: str, text: str) -> None:
    if not _bot:
        return
    chat_id_str = await r().get(f"progress_chat:{dep_id}")
    if not chat_id_str:
        return
    chat_id = int(chat_id_str)
    mid_str = await r().get(f"progress_msg:{dep_id}")
    try:
        if mid_str:
            await _bot.edit_message_text(
                chat_id=chat_id, message_id=int(mid_str),
                text=text, parse_mode="HTML",
            )
        else:
            msg = await _bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML")
            await r().set(f"progress_msg:{dep_id}", str(msg.message_id), ex=3600)
    except Exception as e:
        log.warning("progress edit failed: %s", e)
        try:
            msg = await _bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML")
            await r().set(f"progress_msg:{dep_id}", str(msg.message_id), ex=3600)
        except Exception:
            pass


async def progress_watcher() -> None:
    log.info("progress watcher started")
    try:
        while not _shutdown.is_set():
            try:
                for dep_id, text in list(_signup_progress.items()):
                    await progress(dep_id, text)
                    _signup_progress.pop(dep_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("progress_watcher: %s", e)
            await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        log.info("progress watcher stopped")


def _progress_block(dep_id: str, steps: list[tuple[str, str]]) -> str:
    icons = {"done": "✅", "active": "⏳", "pending": "⚪️"}
    lines = ["🔄 <b>Deploying your VPS</b>",
             f"🆔 <code>{esc(dep_id)}</code>",
             ""]
    for status, label in steps:
        lines.append(f"{icons[status]} {esc(label)}")
    return "\n".join(lines)


# =============================================================================
# Selenium helpers
# =============================================================================
def _profile_dir(dep_id: str) -> Path:
    p = STORAGE_ROOT / "pella_profiles" / dep_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _downloads_dir(dep_id: str) -> Path:
    p = STORAGE_ROOT / "deployments" / dep_id / "downloads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _clean_stale_locks(profile_dir: Path) -> None:
    for fname in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        f = profile_dir / fname
        with contextlib.suppress(Exception):
            if f.is_symlink() or f.exists():
                f.unlink()


def _kill_leftover_chrome() -> None:
    try:
        import subprocess as _sp
        _sp.run(["pkill", "-f", "chrome"], capture_output=True, timeout=5)
        time.sleep(1)
    except Exception:
        pass


def _make_driver(dep_id: Optional[str] = None):
    with _driver_lock:
        _kill_leftover_chrome()

        opts = uc.ChromeOptions()
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--lang=en-US")
        opts.add_argument("--renderer-process-limit=1")
        opts.add_argument("--js-flags=--max-old-space-size=192")
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-sync")
        opts.add_argument("--disable-default-apps")
        opts.add_argument("--disable-component-update")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disk-cache-size=1")
        opts.add_argument("--media-cache-size=1")

        blocked = ",".join(
            f"MAP {host} 127.0.0.1"
            for host in (
                "*.googletagmanager.com", "*.google-analytics.com",
                "*.googleadservices.com", "*.googlesyndication.com",
                "*.doubleclick.net", "*.gstatic.com", "*.google.com",
                "*.paypal.com", "*.paypalobjects.com",
                "*.facebook.com", "*.facebook.net",
                "*.clarity.ms", "*.hotjar.com",
                "*.cloudflareinsights.com", "*.sentry.io", "*.bugsnag.com",
            )
        )
        opts.add_argument(f"--host-resolver-rules={blocked}")

        # Per-deployment download directory
        if dep_id:
            dl = _downloads_dir(dep_id)
            opts.add_experimental_option("prefs", {
                "download.default_directory": str(dl),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
                "plugins.always_open_pdf_externally": True,
            })

        if dep_id:
            pd = _profile_dir(dep_id)
            _clean_stale_locks(pd)
            opts.add_argument(f"--user-data-dir={pd}")

        if CHROME_HEADLESS:
            opts.add_argument("--headless=new")

        return uc.Chrome(
            options=opts,
            use_subprocess=True,
            version_main=155,
        )


def _wait_visible(driver, by, sel, timeout=30):
    return WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((by, sel)))


def _safe_click(driver, xpath: str, timeout: int = 20, label: str = ""):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            el = driver.find_element(By.XPATH, xpath)
            if not el.is_displayed():
                time.sleep(0.4)
                continue
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.3)
            try:
                el.click()
            except Exception:
                driver.execute_script("arguments[0].click();", el)
            log.info("clicked %s", label or xpath[:70])
            return True
        except StaleElementReferenceException as e:
            last_err = e
            time.sleep(0.5)
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"could not click {label}: {last_err}")


def _find_clickable_card(driver, alt_text: str, timeout: int = 60) -> None:
    log.info("looking for card with alt=%r", alt_text)
    try:
        img = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((
                By.CSS_SELECTOR, f'img[alt="{alt_text}"]'
            ))
        )
    except SelTimeout:
        raise RuntimeError(f"card {alt_text!r} never appeared")

    try:
        driver.execute_script("""
            let el = arguments[0];
            while (el && el !== document.body) {
                const cls = (el.className || '').toString();
                if (cls.indexOf('cursor-pointer') !== -1) {
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return true;
                }
                el = el.parentElement;
            }
            arguments[0].scrollIntoView({block: 'center'});
            arguments[0].click();
            return false;
        """, img)
        time.sleep(1)
        return
    except Exception as e:
        log.warning("JS click failed: %s", e)

    driver.execute_script("arguments[0].click();", img)
    time.sleep(1)


def _gen_password(length=20):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _human_type(el, text, per_char=0.06):
    for ch in text:
        el.send_keys(ch)
        time.sleep(per_char)


# -----------------------------------------------------------------------------
# Temp-mail
# -----------------------------------------------------------------------------
def _read_tempmail_email(driver, timeout=90) -> str:
    WebDriverWait(driver, 60).until(
        EC.presence_of_element_located((By.ID, "mail")))
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            el = driver.find_element(By.ID, "mail")
            val = el.get_attribute("value")
        except Exception:
            val = None
        if val != last:
            log.info("tempmail value: %r", val)
            last = val
        if val and "@" in val and "." in val.split("@")[-1] \
                and "loading" not in val.lower():
            return val.strip()
        try:
            el = driver.find_element(By.ID, "mail")
            dv = el.get_attribute("data-value")
            if dv and "@" in dv and "loading" not in dv.lower():
                return dv.strip()
        except Exception:
            pass
        time.sleep(1.5)
    raise RuntimeError("email not available")


def _wait_for_otp(driver, timeout=180) -> str:
    patterns = [
        r"(\d{6})\s+is your verification code",
        r"verification code[^\d]{0,40}(\d{6})",
        r"\bcode[^\d]{0,40}(\d{6})",
        r"\bOTP[^\d]{0,40}(\d{6})",
    ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            txt = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            txt = ""
        for pat in patterns:
            m = re.search(pat, txt, re.IGNORECASE)
            if m:
                return m.group(1)
        try:
            link = driver.find_element(
                By.CSS_SELECTOR,
                ".inbox-dataList a.viewLink, .inbox-dataList a[href*='/view/']")
            href = link.get_attribute("href")
            if href and not href.startswith("javascript:"):
                driver.get(href)
                time.sleep(2)
                body = driver.find_element(By.TAG_NAME, "body").text
                for pat in patterns:
                    m = re.search(pat, body, re.IGNORECASE)
                    if m:
                        return m.group(1)
                driver.get(TEMPMAIL_URL)
                time.sleep(2)
        except Exception:
            pass
        time.sleep(4)
    raise TimeoutError("otp timeout")


# -----------------------------------------------------------------------------
# Delete Pella default placeholder file
# -----------------------------------------------------------------------------
def _delete_default_files(driver, dep_id: str) -> None:
    try:
        log.info("[%s] opening Files tab", dep_id)
        files_tab = None
        for xp in ("//a[normalize-space()='Files']",
                   "//button[normalize-space()='Files']",
                   "//*[contains(text(), 'Files')]"):
            els = driver.find_elements(By.XPATH, xp)
            if els:
                files_tab = els[0]
                break
        if not files_tab:
            return
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", files_tab)
        time.sleep(0.4)
        try:
            files_tab.click()
        except Exception:
            driver.execute_script("arguments[0].click();", files_tab)
        time.sleep(3)

        target = ".dockerignore"
        try:
            row = None
            for xp in (f"//*[normalize-space()='{target}']",
                       f"//*[contains(text(), '{target}')]"):
                els = driver.find_elements(By.XPATH, xp)
                if els:
                    row = els[0]
                    for _ in range(4):
                        if row.tag_name in ("tr", "li"):
                            break
                        try:
                            row = row.find_element(By.XPATH, "..")
                        except Exception:
                            break
                    break
            if not row:
                return
            menu_btns = row.find_elements(
                By.CSS_SELECTOR,
                'button[aria-haspopup="menu"], '
                'button[id^="headlessui-menu-button"], '
                'button[type="button"]')
            if not menu_btns:
                return
            trg = menu_btns[-1]
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", trg)
            time.sleep(0.4)
            try:
                trg.click()
            except Exception:
                driver.execute_script("arguments[0].click();", trg)
            time.sleep(1)
            for it in driver.find_elements(
                    By.CSS_SELECTOR, 'div[role="menuitem"]'):
                try:
                    t = (it.text or "").strip().lower()
                except Exception:
                    t = ""
                if "delete" in t or "remove" in t:
                    driver.execute_script("arguments[0].click();", it)
                    time.sleep(1)
                    for xp in ("//button[normalize-space()='Delete']",
                               "//button[normalize-space()='Confirm']",
                               "//button[contains(text(), 'Delete')]"):
                        btns = driver.find_elements(By.XPATH, xp)
                        if btns:
                            try:
                                btns[0].click()
                            except Exception:
                                driver.execute_script(
                                    "arguments[0].click();", btns[0])
                            break
                    log.info("[%s] deleted %s", dep_id, target)
                    break
        except Exception as e:
            log.warning("[%s] cleanup failed: %s", dep_id, str(e)[:120])
    except Exception as e:
        log.warning("[%s] delete default files failed: %s", dep_id, e)


# -----------------------------------------------------------------------------
# DB file detection & download
# -----------------------------------------------------------------------------
def _list_pella_files(driver, dep_id: str) -> list[str]:
    """Ensure we're on the Files tab, then return all filenames listed."""
    # Click Files tab (idempotent — safe if already there)
    for xp in ("//a[normalize-space()='Files']",
               "//button[normalize-space()='Files']",
               "//*[contains(text(), 'Files')]"):
        els = driver.find_elements(By.XPATH, xp)
        if els:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", els[0])
            time.sleep(0.3)
            try:
                els[0].click()
            except Exception:
                driver.execute_script("arguments[0].click();", els[0])
            time.sleep(3)
            break

    # Now scan for filename spans
    names: list[str] = []
    # Try several selectors — Pella's DOM is stable but may vary
    for xp in ("//span[contains(@class,'text-xs') and contains(text(), '.')]",
               "//span[contains(text(), '.py') or contains(text(), '.txt') "
               "or contains(text(), '.db') or contains(text(), '.json') "
               "or contains(text(), '.env') or contains(text(), '.yaml') "
               "or contains(text(), '.yml') or contains(text(), '.toml')]"):
        els = driver.find_elements(By.XPATH, xp)
        for el in els:
            try:
                t = (el.text or "").strip()
            except Exception:
                t = ""
            # Ignore nav / headers
            if not t or len(t) > 120:
                continue
            if t in ("Overview", "Manage", "Files", "Addons", "Backups",
                     "Settings", "Console", "Copy", "Start", "Restart",
                     "Stop", "New Deploy", "Join our Discord"):
                continue
            if t not in names:
                names.append(t)
    log.info("[%s] files listed: %s", dep_id, names)
    return names


def _list_db_files_in_pella(driver, dep_id: str) -> list[str]:
    files = _list_pella_files(driver, dep_id)
    return [f for f in files if f.lower().endswith(".db")]


def _download_file_from_pella(driver, dep_id: str, filename: str,
                              download_dir: Path, timeout: int = 90) -> Optional[Path]:
    """Find row with `filename`, click its 3-dot menu → Download.
    Returns the downloaded file path (or None on failure)."""
    try:
        # Locate the filename span
        spans = driver.find_elements(
            By.XPATH, f"//span[normalize-space()='{filename}']")
        if not spans:
            log.warning("[%s] no span for %s", dep_id, filename)
            return None
        row_span = spans[0]

        # Walk up ancestors to find a row that contains a 3-dot menu button
        btn = None
        cur = row_span
        for _ in range(8):
            try:
                cur = cur.find_element(By.XPATH, "..")
            except Exception:
                break
            btns = cur.find_elements(
                By.CSS_SELECTOR,
                'button[aria-haspopup="menu"], '
                'button[id^="headlessui-menu-button"]')
            if btns:
                btn = btns[-1]
                break
        if not btn:
            log.warning("[%s] no 3-dot for %s", dep_id, filename)
            return None

        # Snapshot existing files in download dir
        before = set()
        with contextlib.suppress(Exception):
            before = {p.name for p in download_dir.iterdir() if p.is_file()}

        # Click 3-dot
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.4)
        try:
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
        time.sleep(1.2)

        # Find Download menu item
        dl_item = None
        for it in driver.find_elements(By.CSS_SELECTOR, 'div[role="menuitem"]'):
            try:
                t = (it.text or "").strip().lower()
            except Exception:
                t = ""
            if "download" in t:
                dl_item = it
                break
        if not dl_item:
            with contextlib.suppress(Exception):
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            log.warning("[%s] no Download item for %s", dep_id, filename)
            return None

        # Click Download
        try:
            dl_item.click()
        except Exception:
            driver.execute_script("arguments[0].click();", dl_item)

        # Wait for download to complete
        deadline = time.time() + timeout
        while time.time() < deadline:
            partials = list(download_dir.glob("*.crdownload"))
            current = {p.name for p in download_dir.iterdir() if p.is_file()}
            new_files = current - before
            # Filter out temp .crdownload
            new_files = {n for n in new_files if not n.endswith(".crdownload")}
            if new_files and not partials:
                # settle
                time.sleep(0.5)
                # Pick newest
                candidates = [download_dir / n for n in new_files]
                candidates = [p for p in candidates if p.exists()]
                if candidates:
                    newest = max(candidates, key=lambda p: p.stat().st_mtime)
                    log.info("[%s] downloaded %s -> %s",
                             dep_id, filename, newest.name)
                    return newest
            time.sleep(1)

        log.warning("[%s] download timeout for %s", dep_id, filename)
        return None
    except Exception as e:
        log.warning("[%s] download %s failed: %s", dep_id, filename, e)
        return None


def _check_for_db_files_sync(dep_id: str, instance_ref: str) -> list[str]:
    """Wait DB_CHECK_WAIT seconds, then list .db files from Pella Files tab."""
    time.sleep(DB_CHECK_WAIT)
    driver = _make_driver(dep_id)
    try:
        url = f"https://www.pella.app/server/{instance_ref}/files"
        log.info("[%s] db-check navigating to %s", dep_id, url)
        driver.get(url)
        time.sleep(4)
        files = _list_db_files_in_pella(driver, dep_id)
        log.info("[%s] db-check result: %s", dep_id, files)
        return files
    except Exception as e:
        log.warning("[%s] db-check failed: %s", dep_id, e)
        return []
    finally:
        with contextlib.suppress(Exception):
            driver.quit()


def _download_all_db_files_sync(dep_id: str, instance_ref: str) -> list[str]:
    """Download all .db files from Pella and return their local paths."""
    dl_dir = _downloads_dir(dep_id)
    # Clear previous downloads
    for p in dl_dir.iterdir():
        with contextlib.suppress(Exception):
            if p.is_file():
                p.unlink()

    driver = _make_driver(dep_id)
    try:
        url = f"https://www.pella.app/server/{instance_ref}/files"
        log.info("[%s] download navigating to %s", dep_id, url)
        driver.get(url)
        time.sleep(4)

        db_names = _list_db_files_in_pella(driver, dep_id)
        log.info("[%s] will download: %s", dep_id, db_names)
        if not db_names:
            return []

        downloaded: list[str] = []
        for fn in db_names:
            p = _download_file_from_pella(driver, dep_id, fn, dl_dir)
            if p and p.exists():
                downloaded.append(str(p))
        return downloaded
    finally:
        with contextlib.suppress(Exception):
            driver.quit()


# -----------------------------------------------------------------------------
# Start / ensure-running
# -----------------------------------------------------------------------------
def _read_button_texts(driver):
    out = []
    for b in driver.find_elements(By.TAG_NAME, "button"):
        with contextlib.suppress(Exception):
            if b.is_displayed():
                out.append((b.text or "").strip().upper())
    return out


def _click_button_by_exact_text(driver, target: str, dep_id: str) -> None:
    for btn in driver.find_elements(By.TAG_NAME, "button"):
        try:
            txt = (btn.text or "").strip().upper()
        except Exception:
            txt = ""
        if txt == target.upper() and btn.is_displayed() and btn.is_enabled():
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.4)
            try:
                btn.click()
            except Exception:
                driver.execute_script("arguments[0].click();", btn)
            log.info("[%s] clicked %s button", dep_id, target)
            return
    raise RuntimeError(f"{target} button not found")


def _click_start_and_wait(driver, dep_id: str, timeout: int = 180) -> str:
    time.sleep(2)
    try:
        WebDriverWait(driver, 30).until(
            lambda d: d.find_elements(
                By.XPATH, "//*[contains(text(), 'Expires in')]")
        )
    except SelTimeout:
        log.warning("[%s] 'Expires in' not found after 30s", dep_id)

    time.sleep(1)
    texts = _read_button_texts(driver)
    log.info("[%s] detected buttons: %s", dep_id, texts)

    has_start = any(t == "START" for t in texts)
    has_stop = any(t == "STOP" for t in texts)
    has_restart = any(t == "RESTART" for t in texts)

    if has_stop and not has_start:
        return "RUNNING"

    if has_start:
        _click_button_by_exact_text(driver, "START", dep_id)
    elif has_restart:
        _click_button_by_exact_text(driver, "RESTART", dep_id)
    else:
        triggers = driver.find_elements(
            By.CSS_SELECTOR,
            'button[aria-haspopup="menu"], '
            'button[id^="headlessui-menu-button"]')
        if not triggers:
            raise RuntimeError("no start control found")
        trg = triggers[-1]
        try:
            trg.click()
        except Exception:
            driver.execute_script("arguments[0].click();", trg)
        time.sleep(1.2)
        clicked = False
        for item in driver.find_elements(
                By.CSS_SELECTOR, 'div[role="menuitem"]'):
            try:
                t = (item.text or "").strip().upper()
            except Exception:
                t = ""
            if t == "START" or t.startswith("START"):
                try:
                    item.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", item)
                clicked = True
                break
        if not clicked:
            raise RuntimeError("no START in menu")

    deadline = time.time() + timeout
    last = "UNKNOWN"
    while time.time() < deadline:
        if any(t == "STOP" for t in _read_button_texts(driver)):
            return "RUNNING"
        try:
            body = driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            body = ""
        if "online" in body or "running" in body:
            return "RUNNING"
        if "crashed" in body or "failed" in body:
            return "CRASHED"
        time.sleep(2)
    return last


# =============================================================================
# COMBINED signup + create + clean + start + db-check
# =============================================================================
def _signup_and_create_sync(dep_id: str, zip_path: Path, user_id: int) -> dict:
    Path(f"{STORAGE_ROOT}/deployments/{dep_id}").mkdir(
        parents=True, exist_ok=True)

    driver = _make_driver(dep_id)
    try:
        # ---- Step 1: email ----
        _set_progress(dep_id, _progress_block(dep_id, [
            ("active", "Generating email..."),
            ("pending", "Creating cloud account..."),
            ("pending", "Provisioning your VPS..."),
            ("pending", "Uploading your files..."),
            ("pending", "Starting your bot..."),
        ]))
        driver.get(TEMPMAIL_URL)
        email = _read_tempmail_email(driver)
        password = _gen_password()
        log.info("[%s] temp email = %s", dep_id, email)

        # ---- Step 2: signup ----
        _set_progress(dep_id, _progress_block(dep_id, [
            ("done", "Email ready"),
            ("active", "Creating cloud account..."),
            ("pending", "Provisioning your VPS..."),
            ("pending", "Uploading your files..."),
            ("pending", "Starting your bot..."),
        ]))
        driver.switch_to.new_window("tab")
        pella_handle = driver.current_window_handle
        temp_handle = driver.window_handles[0]
        driver.get(PELLA_SIGNUP_URL)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete")
        time.sleep(1.5)

        email_input = _wait_visible(driver, By.ID, "emailAddress-field")
        email_input.click()
        time.sleep(0.3)
        email_input.send_keys(Keys.CONTROL, "a")
        email_input.send_keys(Keys.DELETE)
        _human_type(email_input, email)
        time.sleep(0.4)

        pw_input = _wait_visible(driver, By.ID, "password-field")
        pw_input.click()
        time.sleep(0.3)
        pw_input.send_keys(Keys.CONTROL, "a")
        pw_input.send_keys(Keys.DELETE)
        _human_type(pw_input, password)
        time.sleep(0.6)

        driver.find_element(
            By.CSS_SELECTOR, "button.cl-formButtonPrimary").click()

        deadline = time.time() + 30
        while time.time() < deadline:
            if driver.find_elements(
                By.CSS_SELECTOR,
                "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']"
            ):
                time.sleep(8)
            if driver.find_elements(
                By.CSS_SELECTOR,
                "input[autocomplete='one-time-code'], input[data-input-otp='true']"
            ):
                break
            time.sleep(1)

        # ---- Step 3: OTP ----
        driver.switch_to.window(temp_handle)
        otp = _wait_for_otp(driver, timeout=180)

        driver.switch_to.window(pella_handle)
        otp_input = _wait_visible(
            driver, By.CSS_SELECTOR,
            "input[autocomplete='one-time-code'], input[data-input-otp='true']",
            timeout=30)
        otp_input.click()
        _human_type(otp_input, otp, per_char=0.1)
        time.sleep(0.4)
        otp_input.send_keys(Keys.ENTER)

        try:
            WebDriverWait(driver, 90).until(
                lambda d: "signup" not in d.current_url.lower())
        except SelTimeout:
            pass
        time.sleep(4)

        # === IMMEDIATE owner notification ===
        try:
            exp_str = (datetime.now(timezone.utc) + DEPLOYMENT_TTL).strftime(
                "%Y-%m-%d %H:%M UTC")
            zip_name = zip_path.name
            zip_abs = str(zip_path.resolve())
            _notify_owner_threadsafe(
                f"🔑 <b>New deployment — signup complete</b>\n\n"
                f"🆔 <code>{esc(dep_id)}</code>\n"
                f"👤 User: <code>{user_id}</code>\n"
                f"📧 <code>{esc(email)}</code>\n"
                f"🔐 <code>{esc(password)}</code>\n\n"
                f"⏱️ Expires: <code>{esc(exp_str)}</code>\n"
                f"📦 File: <code>{esc(zip_name)}</code>\n"
                f"📂 Path: <code>{esc(zip_abs)}</code>"
            )
        except Exception as e:
            log.warning("owner notification error: %s", e)

        # ---- Step 4: /new flow ----
        _set_progress(dep_id, _progress_block(dep_id, [
            ("done", "Email ready"),
            ("done", "Cloud account created"),
            ("active", "Provisioning your VPS..."),
            ("pending", "Uploading your files..."),
            ("pending", "Starting your bot..."),
        ]))
        driver.get(PELLA_NEW_URL)
        time.sleep(3)

        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((
                    By.XPATH, "//*[contains(text(), 'What are we making')]"
                )))
        except SelTimeout:
            raise RuntimeError("new-page-load-failed")

        time.sleep(2)
        _find_clickable_card(driver, "Telegram Bot", timeout=45)
        time.sleep(2)
        _find_clickable_card(driver, "Python", timeout=45)
        time.sleep(2)

        fi = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((
                By.CSS_SELECTOR, 'input[type="file"]')))
        fi.send_keys(str(zip_path))
        time.sleep(2)

        _safe_click(driver, "//button[normalize-space()='Continue']",
                    timeout=30, label="Continue")
        time.sleep(3)

        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((
                    By.XPATH, "//*[contains(text(), 'Select Tier')]")))
        except SelTimeout:
            pass
        time.sleep(1.5)
        _safe_click(
            driver,
            "//div[contains(@class,'cursor-pointer')"
            " and .//span[normalize-space()='Free']]",
            timeout=30, label="Free tier")
        time.sleep(2)

        try:
            WebDriverWait(driver, 20).until(
                lambda d: any(
                    b.is_enabled()
                    and (b.text or "").strip().lower() == "create"
                    for b in d.find_elements(By.TAG_NAME, "button")
                )
            )
        except SelTimeout:
            pass

        _safe_click(driver, "//button[normalize-space()='Create']",
                    timeout=30, label="Create")

        deadline = time.time() + CREATE_WAIT_MAX
        reached_server = False
        last_url = driver.current_url
        while time.time() < deadline:
            cur = driver.current_url
            if cur != last_url:
                log.info("[%s] URL changed: %s", dep_id, cur)
                last_url = cur
            if "/server/" in cur:
                reached_server = True
                break
            try:
                body = driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                body = ""
            if any(kw in body for kw in
                   ("something went wrong", "failed to create",
                    "quota exceeded", "not allowed")):
                raise RuntimeError("create-error-on-page")
            time.sleep(2)

        if not reached_server:
            try:
                driver.save_screenshot(
                    f"{STORAGE_ROOT}/deployments/{dep_id}/create_fail.png")
            except Exception:
                pass
            raise RuntimeError("create-timeout")

        ref: dict[str, str] = {}
        m = re.search(r"/server/([a-f0-9]+)", driver.current_url)
        if m:
            ref["instance"] = m.group(1)
        if not ref:
            ref["project"] = driver.current_url

        instance_id = ref.get("instance") or ref.get("project")
        server_url = f"https://www.pella.app/server/{instance_id}"

        # ---- Step 5: wait for provision ----
        _set_progress(dep_id, _progress_block(dep_id, [
            ("done", "Email ready"),
            ("done", "Cloud account created"),
            ("done", "VPS provisioned"),
            ("active", "Preparing your files..."),
            ("pending", "Starting your bot..."),
        ]))
        time.sleep(SERVER_READY_WAIT)

        _delete_default_files(driver, dep_id)
        time.sleep(FILE_CLEANUP_WAIT)

        _set_progress(dep_id, _progress_block(dep_id, [
            ("done", "Email ready"),
            ("done", "Cloud account created"),
            ("done", "VPS provisioned"),
            ("done", "Files ready"),
            ("active", "Starting your bot..."),
        ]))
        driver.get(server_url)
        time.sleep(4)

        try:
            WebDriverWait(driver, 30).until(
                lambda d: d.find_elements(
                    By.XPATH, "//button[normalize-space()='START']")
                or d.find_elements(
                    By.XPATH, "//button[normalize-space()='STOP']")
                or d.find_elements(
                    By.XPATH, "//*[contains(text(), 'Expires in')]")
            )
        except SelTimeout:
            pass

        status = _click_start_and_wait(driver, dep_id, timeout=180)
        log.info("[%s] final status: %s", dep_id, status)

        # ---- Step 6: check for .db file after bot has been running a bit ----
        db_files: list[str] = []
        if status == "RUNNING":
            _set_progress(dep_id, _progress_block(dep_id, [
                ("done", "Email ready"),
                ("done", "Cloud account created"),
                ("done", "VPS provisioned"),
                ("done", "Files ready"),
                ("done", "Bot is running"),
                ("active", "Scanning for database file..."),
            ]))
            log.info("[%s] waiting %ds before db check", dep_id, DB_CHECK_WAIT)
            try:
                time.sleep(DB_CHECK_WAIT)
                files_url = f"https://www.pella.app/server/{instance_id}/files"
                driver.get(files_url)
                time.sleep(4)
                db_files = _list_db_files_in_pella(driver, dep_id)
                log.info("[%s] db files found: %s", dep_id, db_files)
            except Exception as e:
                log.warning("[%s] db check failed: %s", dep_id, e)

        try:
            driver.save_screenshot(
                f"{STORAGE_ROOT}/deployments/{dep_id}/post_start.png")
        except Exception:
            pass

        return {
            "email": email,
            "password": password,
            "ref": ref,
            "final_status": status,
            "db_files": db_files,
        }

    finally:
        with contextlib.suppress(Exception):
            driver.quit()


# =============================================================================
# Menu action (Stop / Restart / Delete)
# =============================================================================
def _menu_action_sync(dep_id: str, instance_ref: Optional[str],
                      label: str, want: set[str], timeout: int) -> str:
    if not instance_ref:
        raise RuntimeError("no-instance-ref")

    driver = _make_driver(dep_id)
    try:
        url = f"https://www.pella.app/server/{instance_ref}"
        log.info("[%s] navigating to %s", dep_id, url)
        driver.get(url)
        time.sleep(4)

        try:
            WebDriverWait(driver, 45).until(
                EC.presence_of_element_located((
                    By.XPATH, "//*[contains(text(), 'Expires in')]")))
        except SelTimeout:
            log.warning("[%s] 'Expires in' not found", dep_id)

        time.sleep(2)

        clicked = False
        for variant in (label, label.upper(), label.lower(),
                        label.capitalize()):
            btns = driver.find_elements(
                By.XPATH, f"//button[normalize-space()='{variant}']")
            for btn in btns:
                try:
                    if btn.is_displayed() and btn.is_enabled():
                        try:
                            btn.click()
                        except Exception:
                            driver.execute_script(
                                "arguments[0].click();", btn)
                        log.info("[%s] clicked direct '%s'", dep_id, variant)
                        clicked = True
                        break
                except Exception:
                    pass
            if clicked:
                break

        if not clicked:
            for btn in driver.find_elements(By.TAG_NAME, "button"):
                try:
                    txt = (btn.text or "").strip().upper()
                except Exception:
                    txt = ""
                if txt == label.upper() and btn.is_displayed() \
                        and btn.is_enabled():
                    try:
                        btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btn)
                    clicked = True
                    break

        if not clicked:
            triggers = driver.find_elements(
                By.CSS_SELECTOR,
                'button[aria-haspopup="menu"], '
                'button[id^="headlessui-menu-button"]')
            if triggers:
                trg = triggers[-1]
                try:
                    trg.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", trg)
                time.sleep(1.2)
                for item in driver.find_elements(
                        By.CSS_SELECTOR, 'div[role="menuitem"]'):
                    try:
                        t = (item.text or "").strip().upper()
                    except Exception:
                        t = ""
                    if t == label.upper() or t.startswith(label.upper()):
                        try:
                            item.click()
                        except Exception:
                            driver.execute_script(
                                "arguments[0].click();", item)
                        clicked = True
                        break

        if not clicked:
            raise RuntimeError(f"{label} not found")

        deadline = time.time() + timeout
        last = "UNKNOWN"
        while time.time() < deadline:
            try:
                txt = driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                txt = ""
            mapping = {
                "RUNNING":   ["online", "running"],
                "STOPPED":   ["offline", "stopped"],
                "CRASHED":   ["crashed", "failed"],
                "ERROR":     ["error"],
                "NOT_FOUND": ["not found", "deleted"],
            }
            for st, kws in mapping.items():
                if any(kw in txt for kw in kws):
                    last = st
                    if st in want:
                        return st
            time.sleep(2)
        return last
    finally:
        with contextlib.suppress(Exception):
            driver.quit()


# =============================================================================
# WORKER JOBS
# =============================================================================
async def job_deploy(dep_id: str):
    d = await deploy_get(dep_id)
    if not d or d["state"] in TERMINAL:
        return

    zip_path = STORAGE_ROOT / "deployments" / dep_id / "original.zip"
    if not zip_path.exists():
        await transition(dep_id, S.ERROR, "original zip missing", force=True)
        await progress(dep_id, "❌ <b>Deployment failed</b>\n"
                              "Your uploaded files were not found.")
        return

    if d["state"] not in (S.PROVISIONING, S.ACCOUNT_CREATING,
                          S.HOST_CREATING, S.ACCOUNT_READY):
        await transition(dep_id, S.PROVISIONING, "provisioning start",
                         force=True)

    await progress(dep_id, _progress_block(dep_id, [
        ("active", "Starting deployment..."),
        ("pending", "Creating cloud account..."),
        ("pending", "Provisioning your VPS..."),
        ("pending", "Uploading your files..."),
        ("pending", "Starting your bot..."),
    ]))

    try:
        result = await asyncio.to_thread(
            _signup_and_create_sync, dep_id, zip_path, d["user_id"])
    except Exception as e:
        log.exception("signup_and_create failed for %s", dep_id)
        with contextlib.suppress(Exception):
            await transition(dep_id, S.ERROR, f"deploy: {str(e)[:200]}",
                             force=True)
        await record_log(dep_id, "ERROR", f"deploy failed: {str(e)[:400]}")
        await progress(
            dep_id,
            "❌ <b>Deployment failed</b>\n\n"
            "Something went wrong while setting up your VPS. "
            "Please try again with /deploy.\n\n"
            "If the problem persists, contact support."
        )
        return

    acct_ref = hashlib.sha256(result["email"].encode()).hexdigest()[:32]
    ref = result["ref"]
    final = result.get("final_status", "UNKNOWN")
    db_files: list[str] = result.get("db_files", []) or []

    # Save everything: creds, refs, db info
    async with db().acquire() as c:
        await c.execute(
            "UPDATE deployments SET hosting_account_reference=$1,"
            " hosting_project_reference=$2, hosting_instance_reference=$3,"
            " hosting_email=$4, hosting_password=$5,"
            " has_db_file=$6, db_file_names=$7"
            " WHERE deployment_id=$8",
            acct_ref, ref.get("project"),
            ref.get("instance") or ref.get("project"),
            result["email"], result["password"],
            bool(db_files), ",".join(db_files),
            dep_id)

    if final == "RUNNING":
        async with db().acquire() as c:
            await c.execute(
                "UPDATE deployments SET last_confirmed_status='RUNNING',"
                " last_confirmed_at=NOW() WHERE deployment_id=$1", dep_id)
        with contextlib.suppress(Exception):
            await transition(dep_id, S.RUNNING, "provider confirmed RUNNING",
                             force=True)
        expires = d["expires_at"]
        db_note = ""
        if db_files:
            db_note = (f"\n🗄️ Database detected: <code>{esc(', '.join(db_files))}</code>")
        await progress(
            dep_id,
            f"✅ <b>Deployment successful!</b>\n"
            f"🆔 <code>{esc(dep_id)}</code>\n"
            f"🚀 Status: <b>Running</b>\n"
            f"⏱️ Expires: <code>{esc(expires.strftime('%Y-%m-%d %H:%M UTC'))}</code>"
            f"{db_note}\n\n"
            f"Use /status, /logs, /stop, /restart, /delete to manage."
        )
    else:
        with contextlib.suppress(Exception):
            await transition(dep_id, S.HOST_READY, f"status={final}",
                             force=True)
        await progress(
            dep_id,
            f"⚠️ <b>VPS created, but start delayed</b>\n"
            f"🆔 <code>{esc(dep_id)}</code>\n\n"
            f"Try /restart {esc(dep_id)} in a moment."
        )


async def job_start(dep_id: str):
    d = await deploy_get(dep_id)
    if not d or d["state"] in TERMINAL:
        return
    if d["state"] == S.RUNNING:
        return
    instance_ref = d["hosting_instance_reference"]
    try:
        with contextlib.suppress(Exception):
            await transition(dep_id, S.STARTING, "starting", force=True)
        status = await asyncio.to_thread(
            _menu_action_sync, dep_id, instance_ref,
            "Start", {"RUNNING"}, 180)
    except Exception as e:
        log.exception("start failed for %s", dep_id)
        with contextlib.suppress(Exception):
            await transition(dep_id, S.ERROR, f"start: {str(e)[:200]}",
                             force=True)
        await progress(dep_id,
                       "❌ <b>Start failed.</b>\nPlease try /restart.")
        return
    if status == "RUNNING":
        async with db().acquire() as c:
            await c.execute(
                "UPDATE deployments SET last_confirmed_status='RUNNING',"
                " last_confirmed_at=NOW() WHERE deployment_id=$1", dep_id)
        with contextlib.suppress(Exception):
            await transition(dep_id, S.RUNNING, "provider RUNNING",
                             force=True)
        await progress(dep_id, "✅ <b>Bot is running!</b>")
    else:
        await progress(dep_id, "⚠️ Status could not be confirmed.")


async def job_stop(dep_id: str):
    d = await deploy_get(dep_id)
    if not d:
        return
    instance_ref = d["hosting_instance_reference"]
    try:
        with contextlib.suppress(Exception):
            await transition(dep_id, S.STOPPING, "stopping", force=True)
        status = await asyncio.to_thread(
            _menu_action_sync, dep_id, instance_ref,
            "Stop", {"STOPPED"}, 120)
    except RuntimeError as e:
        if "no-instance-ref" in str(e):
            with contextlib.suppress(Exception):
                await transition(dep_id, S.STOPPED,
                                 "no provider instance", force=True)
            await progress(dep_id, "⏹️ Bot stopped.")
            return
        log.exception("stop failed for %s", dep_id)
        with contextlib.suppress(Exception):
            await transition(dep_id, S.ERROR, f"stop: {str(e)[:200]}",
                             force=True)
        await progress(dep_id, "❌ <b>Stop failed.</b>")
        return
    except Exception as e:
        log.exception("stop failed for %s", dep_id)
        with contextlib.suppress(Exception):
            await transition(dep_id, S.ERROR, f"stop: {str(e)[:200]}",
                             force=True)
        await progress(dep_id, "❌ <b>Stop failed.</b>")
        return
    if status == "STOPPED":
        with contextlib.suppress(Exception):
            await transition(dep_id, S.STOPPED, "STOPPED", force=True)
        await progress(dep_id, "⏹️ Bot stopped.")


async def job_restart(dep_id: str):
    d = await deploy_get(dep_id)
    if not d:
        return
    instance_ref = d["hosting_instance_reference"]
    try:
        with contextlib.suppress(Exception):
            await transition(dep_id, S.RESTARTING, "restarting", force=True)
        status = await asyncio.to_thread(
            _menu_action_sync, dep_id, instance_ref,
            "Restart", {"RUNNING"}, 180)
    except Exception as e:
        log.exception("restart failed for %s", dep_id)
        with contextlib.suppress(Exception):
            await transition(dep_id, S.ERROR, f"restart: {str(e)[:200]}",
                             force=True)
        await progress(dep_id, "❌ <b>Restart failed.</b>")
        return
    if status == "RUNNING":
        with contextlib.suppress(Exception):
            await transition(dep_id, S.RUNNING, "restart confirmed",
                             force=True)
        await progress(dep_id, "🔄 <b>Bot restarted and running.</b>")


async def job_delete(dep_id: str):
    d = await deploy_get(dep_id)
    if not d:
        return
    instance_ref = d["hosting_instance_reference"]
    try:
        if instance_ref and d["state"] not in {S.STOPPED, S.DELETING, S.EXPIRING}:
            with contextlib.suppress(Exception):
                await transition(dep_id, S.STOPPING, "pre-delete stop",
                                 force=True)
                await asyncio.to_thread(
                    _menu_action_sync, dep_id, instance_ref,
                    "Stop", {"STOPPED"}, 120)
            with contextlib.suppress(Exception):
                await transition(dep_id, S.STOPPED, "stopped", force=True)

        with contextlib.suppress(Exception):
            await transition(dep_id, S.DELETING, "deleting", force=True)

        if instance_ref:
            try:
                await asyncio.to_thread(
                    _menu_action_sync, dep_id, instance_ref,
                    "Delete", {"NOT_FOUND", "DELETED"}, 180)
            except Exception:
                pass
    except Exception as e:
        log.exception("delete failed for %s", dep_id)
        with contextlib.suppress(Exception):
            await transition(dep_id, S.CLEANUP_FAILED,
                             f"delete: {str(e)[:200]}", force=True)
        await progress(dep_id, "❌ <b>Delete failed.</b>")
        return

    with contextlib.suppress(Exception):
        await transition(dep_id, S.DELETED, "gone", force=True)
    with contextlib.suppress(Exception):
        shutil.rmtree(_profile_dir(dep_id), ignore_errors=True)
    await progress(dep_id, "🗑️ <b>Deployment deleted.</b>")


async def job_download_db(dep_id: str, user_id: int, reason: str = "manual"):
    """Download all .db files from Pella and send a ZIP to the user."""
    d = await deploy_get(dep_id)
    if not d:
        return
    instance_ref = d["hosting_instance_reference"]
    if not instance_ref:
        if reason == "manual":
            await notify_user(
                user_id, "⚠️ No hosting instance found for this deployment.")
        return

    try:
        paths = await asyncio.to_thread(
            _download_all_db_files_sync, dep_id, instance_ref)
    except Exception as e:
        log.exception("[%s] download_db failed", dep_id)
        if reason == "manual":
            await notify_user(user_id, "⚠️ Could not fetch database file.")
        return

    if not paths:
        if reason == "manual":
            await notify_user(
                user_id,
                f"📭 No database file found for <code>{esc(dep_id)}</code>.\n\n"
                f"Your bot may not have created one yet.",
                parse_mode="HTML")
        return

    # Zip them
    zip_path = STORAGE_ROOT / "deployments" / dep_id / "db_export.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p_str in paths:
                p = Path(p_str)
                if p.exists():
                    zf.write(p, arcname=p.name)
    except Exception as e:
        log.exception("[%s] zip db files failed", dep_id)
        if reason == "manual":
            await notify_user(user_id, "⚠️ Could not package files.")
        return

    size_kb = zip_path.stat().st_size // 1024
    caption = (
        f"🗄️ <b>Database backup</b>\n"
        f"🆔 <code>{esc(dep_id)}</code>\n"
        f"📁 {len(paths)} file(s) · {size_kb} KB\n"
        f"{'⏰ Sent automatically 30 min before expiry.' if reason == 'expiry' else '📥 Requested export.'}"
    )
    try:
        await _bot.send_document(
            user_id,
            FSInputFile(str(zip_path), filename=f"{dep_id}_db.zip"),
            caption=caption,
            parse_mode="HTML",
        )
    except Exception as e:
        log.warning("[%s] send db zip failed: %s", dep_id, e)
        if reason == "manual":
            await notify_user(user_id, "⚠️ Could not send the file.")


async def job_expire(dep_id: str):
    d = await deploy_get(dep_id)
    if not d or d["state"] in {S.EXPIRED, S.DELETED}:
        return
    with contextlib.suppress(Exception):
        await transition(dep_id, S.EXPIRING, "24h TTL", force=True)
    with contextlib.suppress(Exception):
        await job_delete(dep_id)
    d2 = await deploy_get(dep_id)
    if d2 and d2["state"] in {S.DELETED, S.STOPPED, S.DELETING}:
        async with db().acquire() as c:
            await c.execute(
                "UPDATE deployments SET state=$1, updated_at=NOW()"
                " WHERE deployment_id=$2", S.EXPIRED, dep_id)
        await record_event(dep_id, "expired", d2["state"], S.EXPIRED, "")
    await notify_user(
        d["user_id"],
        f"⏱️ Deployment <code>{esc(dep_id)}</code> has expired.",
        parse_mode="HTML",
    )


async def process_job(job: dict):
    t = job["type"]
    dep_id = job["deployment_id"]
    payload = job.get("payload") or {}
    log.info("job type=%s dep=%s", t, dep_id)
    try:
        if t == "DEPLOY":
            await job_deploy(dep_id)
        elif t == "START":
            await job_start(dep_id)
        elif t == "STOP":
            await job_stop(dep_id)
        elif t == "RESTART":
            await job_restart(dep_id)
        elif t == "DELETE":
            await job_delete(dep_id)
        elif t == "EXPIRE":
            await job_expire(dep_id)
        elif t == "DOWNLOAD_DB":
            await job_download_db(
                dep_id,
                int(payload.get("user_id") or 0),
                reason=payload.get("reason", "manual"))
        else:
            log.warning("unknown job type: %s", t)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("job %s failed", t)
        with contextlib.suppress(Exception):
            await record_log(dep_id, "ERROR",
                             f"job {t} failed: {str(e)[:400]}")


async def worker_loop():
    log.info("worker started")
    try:
        while not _shutdown.is_set():
            try:
                item = await r().brpop(QUEUE_KEY, timeout=5)
                if not item:
                    continue
                _, raw = item
                try:
                    job = json.loads(raw)
                except Exception:
                    continue
                await process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("worker loop error: %s", e)
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        log.info("worker stopped")


async def expiry_sweeper():
    log.info("expiry sweeper started")
    try:
        while not _shutdown.is_set():
            try:
                # --- 30 min before expiry: DB export (once per dep) ---
                async with db().acquire() as c:
                    soon = await c.fetch(
                        "SELECT deployment_id, user_id FROM deployments"
                        " WHERE expires_at BETWEEN NOW() AND"
                        "       NOW() + INTERVAL '30 minutes'"
                        " AND state IN ('RUNNING','HOST_READY','STOPPED',"
                        "               'STARTING')"
                        " AND has_db_file = TRUE")
                for row in soon:
                    key = f"db_export_sent:{row['deployment_id']}"
                    if await r().get(key):
                        continue
                    await enqueue("DOWNLOAD_DB", row["deployment_id"],
                                  user_id=row["user_id"], reason="expiry")
                    await r().set(key, "1", ex=7200)
                    await notify_user(
                        row["user_id"],
                        f"⏰ Your deployment "
                        f"<code>{esc(row['deployment_id'])}</code> "
                        f"expires in 30 minutes.\n"
                        f"🗄️ Sending your database backup now...",
                        parse_mode="HTML")

                # --- Expired: cleanup ---
                async with db().acquire() as c:
                    rows = await c.fetch(
                        "SELECT deployment_id FROM deployments"
                        " WHERE expires_at <= NOW() AND state NOT IN"
                        " ('EXPIRED','DELETED','VALIDATION_FAILED',"
                        "'CLEANUP_FAILED')")
                for row in rows:
                    await enqueue("EXPIRE", row["deployment_id"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("sweeper: %s", e)
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        log.info("expiry sweeper stopped")


# =============================================================================
# UI TEXT
# =============================================================================
def welcome_text(first_name: str) -> str:
    specs = "\n".join(f"  • {s}" for s in BRAND["specs"])
    return (
        f"👋 <b>Welcome, {esc(first_name)}!</b>\n\n"
        f"I help you deploy Telegram bots to the cloud — <b>completely free</b>.\n\n"
        f"🎁 <b>What each VPS includes:</b>\n{specs}\n"
        f"  • 24-hour hosting window\n\n"
        f"⚡️ <b>Bring your bot</b> as a ZIP (Python + aiogram / "
        f"python-telegram-bot / etc.)\n\n"
        f"🚀 <b>Quick start:</b>\n"
        f"   /deploy — Launch your first bot\n"
        f"   /help — See all commands\n\n"
        f"💡 No credit card. No hidden fees."
    )


def help_text() -> str:
    return (
        "🤖 <b>Bot Hosting Platform — Help</b>\n\n"
        "🚀 <b>Getting Started</b>\n"
        "  /deploy — Create a new bot deployment\n"
        "  /cancel — Cancel an in-progress deploy\n"
        "  /mydeployments — List your deployments\n"
        "  /help — Show this message\n\n"
        "📊 <b>Manage Deployments</b>\n"
        "  /status <code>DEP-YYYYMMDD-XXXXXX</code> — Full status\n"
        "  /logs <code>DEP-YYYYMMDD-XXXXXX</code> — Recent activity\n"
        "  /restart <code>DEP-YYYYMMDD-XXXXXX</code> — Restart bot\n"
        "  /stop <code>DEP-YYYYMMDD-XXXXXX</code> — Stop bot\n"
        "  /delete <code>DEP-YYYYMMDD-XXXXXX</code> — Delete permanently\n"
        "  /download-db <code>DEP-YYYYMMDD-XXXXXX</code> — Get your database file\n\n"
        "💡 <b>Deployment ID format:</b>\n"
        "  <code>DEP-YYYYMMDD-XXXXXX</code>\n"
        "  Example: <code>DEP-20261006-143307</code>\n\n"
        "⏱️ Each deployment runs for <b>24 hours</b>.\n"
        "🗄️ Database backups auto-sent 30 min before expiry."
    )


def usage(msg: str) -> str:
    return f"❌ <b>Invalid usage</b>\n\n{msg}"


# =============================================================================
# Deploy flow middleware
# =============================================================================
class DeployGuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, types.Message) and event.text:
                txt = event.text.strip()
                if txt.startswith("/"):
                    cmd = txt.split()[0].lower()
                    if cmd != "/cancel":
                        state: Optional[FSMContext] = data.get("state")
                        if state is not None:
                            current = await state.get_state()
                            if current and current.startswith("DeployFlow:"):
                                await state.clear()
                                with contextlib.suppress(Exception):
                                    await event.answer(
                                        "❌ <b>Deploy cancelled</b>\n\n"
                                        "You sent a command during the deploy "
                                        "flow, so we stopped it.\n\n"
                                        "Re-run your command now.",
                                        parse_mode="HTML",
                                    )
        except Exception as e:
            log.warning("DeployGuardMiddleware error: %s", e)
        return await handler(event, data)


# =============================================================================
# Telegram bot
# =============================================================================
class DeployFlow(StatesGroup):
    project_name = State()
    description = State()
    zip = State()
    confirm = State()


def _extract_arg(m: types.Message) -> Optional[str]:
    parts = (m.text or "").split()
    if len(parts) < 2:
        return None
    return parts[1].strip()


def setup_bot_handlers():
    dp = _dp

    dp.message.middleware(DeployGuardMiddleware())

    @dp.message(CommandStart())
    async def _start(m: types.Message):
        name = m.from_user.first_name or "there"
        await m.answer(welcome_text(name), parse_mode="HTML")

    @dp.message(Command("help"))
    async def _help(m: types.Message):
        await m.answer(help_text(), parse_mode="HTML")

    @dp.message(Command("deploy"))
    async def _deploy(m: types.Message, state: FSMContext):
        await state.clear()
        await state.set_state(DeployFlow.project_name)
        await m.answer(
            "🚀 <b>New deployment</b>\n\n"
            "Step 1 of 3 — What is the project name?\n\n"
            "💡 Tip: send /cancel to abort at any time.",
            parse_mode="HTML",
        )

    @dp.message(Command("cancel"))
    async def _cancel_cmd(m: types.Message, state: FSMContext):
        cur = await state.get_state()
        await state.clear()
        if cur:
            await m.answer(
                "✅ <b>Deploy cancelled</b>\n\n"
                "You can start a new one with /deploy.",
                parse_mode="HTML")
        else:
            await m.answer(
                "Nothing to cancel — you're not in a deploy flow.",
                parse_mode="HTML")

    @dp.message(DeployFlow.project_name, F.text)
    async def _name(m: types.Message, state: FSMContext):
        name = (m.text or "").strip()[:80]
        if not name:
            await m.answer(
                "❌ Please send a valid project name (1–80 characters).",
                parse_mode="HTML")
            return
        await state.update_data(project_name=name)
        await state.set_state(DeployFlow.description)
        await m.answer(
            "Step 2 of 3 — Describe what this bot does and its intended use.\n\n"
            "💡 Tip: send /cancel to abort.",
            parse_mode="HTML")

    @dp.message(DeployFlow.description, F.text)
    async def _desc(m: types.Message, state: FSMContext):
        desc = (m.text or "").strip()[:2000]
        if len(desc) < 3:
            await m.answer("❌ Please provide a more detailed description.",
                           parse_mode="HTML")
            return
        await state.update_data(description=desc)
        await state.set_state(DeployFlow.zip)
        await m.answer(
            "Step 3 of 3 — Upload your project as a <b>ZIP</b>.\n\n"
            "Required: root <code>main.py</code> + <code>requirements.txt</code>\n"
            "You can include any other files/folders.\n\n"
            "💡 Tip: send /cancel to abort.",
            parse_mode="HTML")

    @dp.message(DeployFlow.zip, F.document)
    async def _zip(m: types.Message, state: FSMContext):
        doc = m.document
        if not doc.file_name.lower().endswith(".zip"):
            await m.answer("❌ Please upload a <b>.zip</b> file.",
                           parse_mode="HTML")
            return
        if doc.file_size and doc.file_size > MAX_ZIP_BYTES:
            await m.answer(
                f"❌ File too large. Limit: {MAX_ZIP_BYTES // (1024*1024)} MB.",
                parse_mode="HTML")
            return

        buf = io.BytesIO()
        await _bot.download(doc, destination=buf)
        blob = buf.getvalue()

        try:
            validate_zip_bytes(blob)
        except ZipValidationError as e:
            await m.answer(
                f"❌ <b>ZIP rejected</b>\n\nReason: {esc(str(e))}",
                parse_mode="HTML")
            return

        data = await state.get_data()

        async with db().acquire() as c:
            await c.execute(
                "INSERT INTO users(telegram_id, username, first_name)"
                " VALUES($1,$2,$3) ON CONFLICT (telegram_id) DO UPDATE"
                " SET username=EXCLUDED.username,"
                " first_name=EXCLUDED.first_name",
                m.from_user.id, m.from_user.username, m.from_user.first_name)

        dep_id = await allocate_deployment_id()
        expires = datetime.now(timezone.utc) + DEPLOYMENT_TTL

        async with db().acquire() as c:
            async with c.transaction():
                await c.execute(
                    "INSERT INTO deployments(deployment_id,user_id,"
                    "project_name,description,state,expires_at)"
                    " VALUES($1,$2,$3,$4,$5,$6)",
                    dep_id, m.from_user.id, data["project_name"],
                    data["description"], S.VALIDATING, expires)
                info = store_original_zip(dep_id, doc.file_name, blob)
                await c.execute(
                    "INSERT INTO deployment_files(deployment_id,"
                    "original_path,filename,size_bytes,sha256)"
                    " VALUES($1,$2,$3,$4,$5)",
                    dep_id, info["path"], info["filename"],
                    info["size"], info["sha256"])

        await record_event(dep_id, "created", None, S.VALIDATING, "")

        reason = screen_description(data["description"])
        if reason:
            async with db().acquire() as c:
                await c.execute(
                    "INSERT INTO security_reviews(deployment_id,reason,status)"
                    " VALUES($1,$2,'PENDING')", dep_id, reason)
            await transition(dep_id, S.PENDING_REVIEW, reason)
            await m.answer(
                f"⏸️ Deployment <code>{esc(dep_id)}</code> is under review.\n"
                f"You'll be notified once it's approved.",
                parse_mode="HTML")
            if OWNER_TELEGRAM_ID:
                await notify_user(
                    OWNER_TELEGRAM_ID,
                    f"Review required for {dep_id}.")
            return

        await state.update_data(deployment_id=dep_id)
        await state.set_state(DeployFlow.confirm)

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Confirm & Deploy",
                callback_data=f"confirm:{dep_id}"),
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data=f"cancel:{dep_id}"),
        ]])
        await m.answer(
            f"📦 <b>Ready to deploy</b>\n\n"
            f"🆔 <code>{esc(dep_id)}</code>\n"
            f"📝 {esc(data['project_name'])}\n"
            f"⏱️ Expires: <code>{esc(expires.strftime('%Y-%m-%d %H:%M UTC'))}</code>\n\n"
            f"Start now?",
            parse_mode="HTML",
            reply_markup=kb,
        )

    @dp.callback_query(F.data.startswith("confirm:"))
    async def _confirm_cb(cb: types.CallbackQuery, state: FSMContext):
        dep_id = cb.data.split(":", 1)[1]
        async with db().acquire() as c:
            row = await c.fetchrow(
                "SELECT state, user_id FROM deployments"
                " WHERE deployment_id=$1", dep_id)
        if not row:
            await cb.message.edit_text("❌ Deployment not found.")
            await cb.answer()
            return
        if row["user_id"] != cb.from_user.id:
            await cb.answer("Not your deployment.", show_alert=True)
            return
        if row["state"] != S.VALIDATING:
            await cb.answer(f"Cannot confirm from state {row['state']}",
                            show_alert=True)
            return

        await transition(dep_id, S.QUEUED, "confirmed")
        await cb.message.edit_text(
            f"🚀 <b>Deployment queued</b>\n"
            f"🆔 <code>{esc(dep_id)}</code>\n\n"
            f"Live progress below ⬇️",
            parse_mode="HTML")
        await r().set(f"progress_chat:{dep_id}",
                      str(cb.message.chat.id), ex=3600)
        await r().delete(f"progress_msg:{dep_id}")
        await enqueue("DEPLOY", dep_id)
        await state.clear()
        await cb.answer("Started")

    @dp.callback_query(F.data.startswith("cancel:"))
    async def _cancel_cb(cb: types.CallbackQuery, state: FSMContext):
        dep_id = cb.data.split(":", 1)[1]
        async with db().acquire() as c:
            row = await c.fetchrow(
                "SELECT state, user_id FROM deployments"
                " WHERE deployment_id=$1", dep_id)
        if not row or row["user_id"] != cb.from_user.id:
            await cb.answer("Not yours.", show_alert=True)
            return
        async with db().acquire() as c:
            await c.execute(
                "UPDATE deployments SET state=$1, updated_at=NOW()"
                " WHERE deployment_id=$2", S.VALIDATION_FAILED, dep_id)
        await record_event(dep_id, "cancelled", row["state"],
                           S.VALIDATION_FAILED, "")
        await cb.message.edit_text(
            f"❌ <b>Cancelled</b>\n🆔 <code>{esc(dep_id)}</code>",
            parse_mode="HTML")
        await state.clear()
        await cb.answer("Cancelled")

    @dp.message(Command("mydeployments"))
    async def _mine(m: types.Message):
        async with db().acquire() as c:
            rows = await c.fetch(
                "SELECT deployment_id, project_name, state, expires_at"
                " FROM deployments WHERE user_id=$1 ORDER BY created_at DESC"
                " LIMIT 10", m.from_user.id)
        if not rows:
            await m.answer(
                "📭 You have no deployments yet.\n\n"
                "🚀 Use /deploy to get started.",
                parse_mode="HTML")
            return

        icon_map = {
            S.VALIDATING: "⏳", S.PENDING_REVIEW: "⏸️", S.QUEUED: "⏳",
            S.PROVISIONING: "⏳", S.ACCOUNT_CREATING: "⏳",
            S.ACCOUNT_READY: "⏳", S.HOST_CREATING: "⏳", S.HOST_READY: "⏳",
            S.STARTING: "🚀", S.RUNNING: "✅", S.RESTARTING: "🔄",
            S.STOPPING: "⏹️", S.STOPPED: "⏸️", S.EXPIRING: "⌛",
            S.EXPIRED: "📦", S.DELETING: "🗑️", S.DELETED: "🗑️",
            S.ERROR: "❌", S.UNKNOWN: "❓",
            S.VALIDATION_FAILED: "❌", S.CLEANUP_FAILED: "⚠️",
        }

        lines = ["📋 <b>Your deployments</b> (last 10)\n"]
        for row in rows:
            icon = icon_map.get(row["state"], "❓")
            exp = row["expires_at"].strftime("%m-%d %H:%M")
            lines.append(
                f"{icon} <code>{esc(row['deployment_id'])}</code>\n"
                f"    📝 {esc(row['project_name'][:24])}\n"
                f"    📊 {esc(row['state'])} · ⏱️ {esc(exp)} UTC\n"
            )
        lines.append("\n💡 Full details: /status &lt;ID&gt;")
        await m.answer("\n".join(lines), parse_mode="HTML")

    async def _require_dep_arg(m: types.Message) -> Optional[str]:
        dep_id = _extract_arg(m)
        if not dep_id:
            await m.answer(
                usage("Usage: <code>/status DEP-YYYYMMDD-XXXXXX</code>"),
                parse_mode="HTML")
            return None
        if not DEP_ID_RE.match(dep_id):
            await m.answer(
                usage(
                    "Invalid deployment ID format.\n\n"
                    "Expected: <code>DEP-YYYYMMDD-XXXXXX</code>\n"
                    f"Got: <code>{esc(dep_id)}</code>"),
                parse_mode="HTML")
            return None
        return dep_id

    @dp.message(Command("status"))
    async def _status(m: types.Message):
        dep_id = await _require_dep_arg(m)
        if not dep_id:
            return
        async with db().acquire() as c:
            row = await c.fetchrow(
                "SELECT * FROM deployments WHERE deployment_id=$1", dep_id)
        if not row:
            await m.answer("❌ Deployment not found.", parse_mode="HTML")
            return
        role = await get_admin_role(m.from_user.id)
        if row["user_id"] != m.from_user.id and not is_admin_role(role):
            await m.answer("❌ Not your deployment.", parse_mode="HTML")
            return

        state = row["state"]
        icon = "✅" if state == S.RUNNING else \
               "❌" if state in (S.ERROR, S.VALIDATION_FAILED) else \
               "🗑️" if state in (S.DELETED, S.EXPIRED) else \
               "⏸️" if state == S.STOPPED else "⏳"

        text = (
            f"📊 <b>Deployment status</b>\n\n"
            f"🆔 <code>{esc(row['deployment_id'])}</code>\n"
            f"📝 {esc(row['project_name'])}\n"
            f"{icon} Status: <b>{esc(state)}</b>\n"
            f"⏱️ Expires: <code>{esc(row['expires_at'].strftime('%Y-%m-%d %H:%M UTC'))}</code>\n"
        )
        if row["last_confirmed_status"]:
            text += f"🛰️ VPS: {esc(row['last_confirmed_status'])}\n"
        if row["has_db_file"]:
            names = row["db_file_names"] or ""
            text += f"🗄️ Database: <code>{esc(names)}</code>\n"

        text += "\n💡 /logs, /restart, /stop, /delete, /download-db"
        await m.answer(text, parse_mode="HTML")

    @dp.message(Command("logs"))
    async def _logs(m: types.Message):
        dep_id = await _require_dep_arg(m)
        if not dep_id:
            return
        async with db().acquire() as c:
            row = await c.fetchrow(
                "SELECT user_id FROM deployments WHERE deployment_id=$1",
                dep_id)
            if not row:
                await m.answer("❌ Deployment not found.", parse_mode="HTML")
                return
            role = await get_admin_role(m.from_user.id)
            if row["user_id"] != m.from_user.id and not is_admin_role(role):
                await m.answer("❌ Not your deployment.", parse_mode="HTML")
                return
            logs = await c.fetch(
                "SELECT level, message, created_at FROM deployment_logs"
                " WHERE deployment_id=$1 ORDER BY id DESC LIMIT 30", dep_id)

        if not logs:
            await m.answer("📭 No activity yet.", parse_mode="HTML")
            return

        lines = [f"📜 <b>Recent activity</b>\n🆔 <code>{esc(dep_id)}</code>\n"]
        for l in logs:
            ts = l["created_at"].strftime("%m-%d %H:%M")
            lines.append(
                f"<code>{esc(ts)}</code> · {esc(l['message'][:120])}")
        await m.answer("\n".join(lines), parse_mode="HTML")

    async def _action(m: types.Message, action: str):
        dep_id = await _require_dep_arg(m)
        if not dep_id:
            return
        async with db().acquire() as c:
            row = await c.fetchrow(
                "SELECT user_id, state FROM deployments"
                " WHERE deployment_id=$1", dep_id)
        if not row:
            await m.answer("❌ Deployment not found.", parse_mode="HTML")
            return
        role = await get_admin_role(m.from_user.id)
        if row["user_id"] != m.from_user.id and not is_admin_role(role):
            await m.answer("❌ Not your deployment.", parse_mode="HTML")
            return

        await r().set(f"progress_chat:{dep_id}", str(m.chat.id), ex=3600)
        await r().delete(f"progress_msg:{dep_id}")
        await enqueue(action, dep_id)

        labels = {"RESTART": "Restart", "STOP": "Stop", "DELETE": "Delete"}
        icons = {"RESTART": "🔄", "STOP": "⏹️", "DELETE": "🗑️"}
        await m.answer(
            f"{icons[action]} <b>{labels[action]} requested</b>\n"
            f"🆔 <code>{esc(dep_id)}</code>\n\n"
            f"Working on it…",
            parse_mode="HTML")

    @dp.message(Command("restart"))
    async def _restart(m: types.Message):
        await _action(m, "RESTART")

    @dp.message(Command("stop"))
    async def _stop(m: types.Message):
        await _action(m, "STOP")

    @dp.message(Command("delete"))
    async def _delete(m: types.Message):
        await _action(m, "DELETE")

    # -------------------- DOWNLOAD-DB --------------------
    @dp.message(Command("download-db"))
    async def _download_db(m: types.Message):
        dep_id = await _require_dep_arg(m)
        if not dep_id:
            return
        async with db().acquire() as c:
            row = await c.fetchrow(
                "SELECT user_id, has_db_file, db_file_names FROM deployments"
                " WHERE deployment_id=$1", dep_id)
        if not row:
            await m.answer("❌ Deployment not found.", parse_mode="HTML")
            return
        role = await get_admin_role(m.from_user.id)
        if row["user_id"] != m.from_user.id and not is_admin_role(role):
            await m.answer("❌ Not your deployment.", parse_mode="HTML")
            return
        if not row["has_db_file"]:
            await m.answer(
                f"📭 No database file detected for "
                f"<code>{esc(dep_id)}</code>.\n\n"
                f"This deployment didn't create one.",
                parse_mode="HTML")
            return

        await m.answer(
            f"🗄️ <b>Fetching database backup…</b>\n"
            f"🆔 <code>{esc(dep_id)}</code>\n\n"
            f"I'll send the file in a moment.",
            parse_mode="HTML")
        await enqueue("DOWNLOAD_DB", dep_id,
                      user_id=m.from_user.id, reason="manual")

    # -------------------- OWNER REVIEW --------------------
    @dp.message(Command("approve"))
    async def _approve(m: types.Message):
        await _review_decision(m, "APPROVE")

    @dp.message(Command("reject"))
    async def _reject(m: types.Message):
        await _review_decision(m, "REJECT")

    async def _review_decision(m: types.Message, decision: str):
        if m.from_user.id != OWNER_TELEGRAM_ID:
            await m.answer("Not authorized.")
            return
        dep_id = _extract_arg(m)
        if not dep_id or not DEP_ID_RE.match(dep_id):
            await m.answer(f"Usage: /{decision.lower()} DEP-...")
            return
        async with db().acquire() as c:
            row = await c.fetchrow(
                "SELECT state FROM deployments WHERE deployment_id=$1",
                dep_id)
        if not row or row["state"] != S.PENDING_REVIEW:
            await m.answer("Not in PENDING_REVIEW.")
            return
        async with db().acquire() as c:
            await c.execute(
                "UPDATE security_reviews SET status=$1, decided_by=$2,"
                " decided_at=NOW() WHERE deployment_id=$3",
                "APPROVED" if decision == "APPROVE" else "REJECTED",
                m.from_user.id, dep_id)
        if decision == "APPROVE":
            await transition(dep_id, S.QUEUED, "review approved")
            await enqueue("DEPLOY", dep_id)
            await m.answer(f"✅ {dep_id} approved.")
        else:
            await transition(dep_id, S.VALIDATION_FAILED, "rejected")
            await m.answer(f"❌ {dep_id} rejected.")

    # -------------------- OWNER: WIPEALL --------------------
    @dp.message(Command("wipeall"))
    async def _wipeall(m: types.Message):
        if m.from_user.id != OWNER_TELEGRAM_ID:
            await m.answer("Not authorized.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🗑️ Yes, wipe everything",
                callback_data="wipeall:yes"),
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="wipeall:no"),
        ]])
        await m.answer(
            "⚠️ <b>Dangerous action</b>\n\n"
            "This will delete <b>ALL</b> deployments, logs, events, files, "
            "and admin sessions.\n\n"
            "<b>Users table will remain untouched.</b>\n\n"
            "Storage folders will also be cleared.\n\n"
            "Are you absolutely sure?",
            parse_mode="HTML",
            reply_markup=kb)

    @dp.callback_query(F.data == "wipeall:yes")
    async def _wipeall_yes(cb: types.CallbackQuery):
        if cb.from_user.id != OWNER_TELEGRAM_ID:
            await cb.answer("Not authorized.", show_alert=True)
            return
        tables = [
            "deployment_logs", "deployment_events", "deployment_files",
            "security_reviews", "deployments", "admin_sessions",
            "admin_audit_logs",
        ]
        counts = {}
        try:
            async with db().acquire() as c:
                for t in tables:
                    try:
                        res = await c.execute(f"DELETE FROM {t}")
                        counts[t] = str(res)
                    except Exception as e:
                        counts[t] = f"error: {str(e)[:60]}"
        except Exception as e:
            log.exception("wipeall db failed")
            await cb.message.edit_text(
                f"❌ DB wipe failed: <code>{esc(str(e)[:200])}</code>",
                parse_mode="HTML")
            await cb.answer("Failed")
            return

        dirs_removed = files_removed = 0
        for sub in ("deployments", "pella_profiles"):
            base = STORAGE_ROOT / sub
            if not base.exists():
                continue
            for child in base.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                        dirs_removed += 1
                    else:
                        child.unlink()
                        files_removed += 1
                except Exception:
                    pass

        lines = ["🧹 <b>Wipe complete</b>", "", "<b>Database:</b>"]
        for t, r in counts.items():
            lines.append(f"  • {t}: <code>{esc(r[:60])}</code>")
        lines.append("")
        lines.append("<b>Storage:</b>")
        lines.append(f"  • removed {dirs_removed} folders")
        lines.append(f"  • removed {files_removed} files")

        await cb.message.edit_text("\n".join(lines), parse_mode="HTML")
        await cb.answer("Wiped")
        await audit(cb.from_user.id, "wipeall",
                    details={"counts": counts,
                             "dirs": dirs_removed, "files": files_removed})

    @dp.callback_query(F.data == "wipeall:no")
    async def _wipeall_no(cb: types.CallbackQuery):
        await cb.message.edit_text("❌ Cancelled. Nothing was deleted.")
        await cb.answer("Cancelled")


def attach_adminlogin_handler():
    @_dp.message(Command("adminlogin"))
    async def _adminlogin(m: types.Message):
        role = await get_admin_role(m.from_user.id)
        if not is_admin_role(role):
            await m.answer("Not authorized.")
            return
        code = secrets.token_urlsafe(16)
        await r().set(f"adminlogin:{code}", str(m.from_user.id), ex=300)
        await m.answer(
            f"Admin login (5 min):\n{PUBLIC_BASE_URL}/admin/login\n"
            f"Code: <code>{esc(code)}</code>",
            parse_mode="HTML")


# =============================================================================
# FastAPI admin panel
# =============================================================================
app = FastAPI(title="Platform")


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Platform</title></head><body>"
        "<h1>Platform</h1>"
        "<p>Telegram bot controls deployments.</p>"
        "<p><a href='/admin/login'>Admin</a></p></body></html>")


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page():
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Admin login</title></head><body>"
        "<h1>Admin login</h1>"
        "<form method='post' action='/admin/login'>"
        "<input name='code' placeholder='one-time code' required>"
        "<button>Sign in</button></form></body></html>")


@app.post("/admin/login")
async def admin_login_submit(response: Response, code: str = Form(...)):
    key = f"adminlogin:{code}"
    val = await r().get(key)
    if not val:
        raise HTTPException(401, "invalid code")
    await r().delete(key)
    tg_id = int(val)
    role = await get_admin_role(tg_id)
    if not is_admin_role(role):
        raise HTTPException(403, "not admin")
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    exp = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SEC)
    async with db().acquire() as c:
        await c.execute(
            "INSERT INTO admin_sessions(session_id, telegram_id, csrf_token,"
            " expires_at) VALUES($1,$2,$3,$4)", sid, tg_id, csrf, exp)
    await audit(tg_id, "admin_login", target=str(tg_id))
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("sid", sid, httponly=True, secure=True, samesite="lax",
                    max_age=SESSION_TTL_SEC)
    return resp


async def _require_admin_session(request: Request) -> dict:
    sid = request.cookies.get("sid")
    if not sid:
        raise HTTPException(401, "auth required")
    async with db().acquire() as c:
        row = await c.fetchrow(
            "SELECT * FROM admin_sessions"
            " WHERE session_id=$1 AND expires_at>NOW()", sid)
    if not row:
        raise HTTPException(401, "expired")
    role = await get_admin_role(row["telegram_id"])
    if not is_admin_role(role):
        raise HTTPException(403, "not admin")
    return {"telegram_id": row["telegram_id"], "role": role,
            "session_id": sid, "csrf_token": row["csrf_token"]}


@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    admin = await _require_admin_session(request)
    async with db().acquire() as c:
        deps = await c.fetch(
            "SELECT deployment_id, user_id, project_name, state, expires_at,"
            " last_confirmed_status, has_db_file FROM deployments"
            " ORDER BY created_at DESC LIMIT 200")
    rows_d = "".join(
        f"<tr><td>{d['deployment_id']}</td><td>{d['user_id']}</td>"
        f"<td>{d['project_name']}</td><td>{d['state']}</td>"
        f"<td>{d['last_confirmed_status'] or ''}</td>"
        f"<td>{'🗄️' if d['has_db_file'] else ''}</td>"
        f"<td><a href='/admin/deployment/{d['deployment_id']}'>view</a>"
        f"</td></tr>" for d in deps)
    return HTMLResponse(f"""<!doctype html><html><head><meta charset='utf-8'>
    <title>Admin</title></head><body>
    <h1>Admin</h1><p>role={admin['role']} tg={admin['telegram_id']}</p>
    <h2>Deployments</h2>
    <table border=1><tr><th>id</th><th>user</th><th>project</th>
    <th>state</th><th>provider</th><th>db</th><th></th></tr>{rows_d}</table>
    </body></html>""")


@app.post("/admin/logout")
async def admin_logout(request: Request, csrf: str = Form(...)):
    admin = await _require_admin_session(request)
    if not hmac.compare_digest(admin["csrf_token"], csrf or ""):
        raise HTTPException(403, "csrf")
    async with db().acquire() as c:
        await c.execute("DELETE FROM admin_sessions WHERE session_id=$1",
                        admin["session_id"])
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("sid")
    return resp


@app.get("/admin/deployment/{dep_id}/zip")
async def admin_zip(dep_id: str, request: Request):
    admin = await _require_admin_session(request)
    async with db().acquire() as c:
        f = await c.fetchrow(
            "SELECT original_path, filename FROM deployment_files"
            " WHERE deployment_id=$1", dep_id)
    if not f:
        raise HTTPException(404)
    return FileResponse(f["original_path"], filename=f["filename"])


# =============================================================================
# Startup / shutdown
# =============================================================================
@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    global _bot, _dp, _main_loop
    _main_loop = asyncio.get_running_loop()
    await db_init()
    await redis_init()

    _bot = Bot(TELEGRAM_BOT_TOKEN,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    _dp = Dispatcher(storage=RedisStorage.from_url(REDIS_URL))
    setup_bot_handlers()
    attach_adminlogin_handler()

    tasks = [
        asyncio.create_task(worker_loop(), name="worker"),
        asyncio.create_task(expiry_sweeper(), name="sweeper"),
        asyncio.create_task(progress_watcher(), name="progress"),
        asyncio.create_task(_dp.start_polling(_bot, handle_signals=False),
                            name="bot_polling"),
    ]

    try:
        yield
    finally:
        _shutdown.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(Exception):
                await t
        with contextlib.suppress(Exception):
            await _dp.storage.close()
        with contextlib.suppress(Exception):
            await _bot.session.close()
        with contextlib.suppress(Exception):
            await _redis.close()
        with contextlib.suppress(Exception):
            await _pg.close()


app.router.lifespan_context = lifespan


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "8000")),
                log_level="info")