"""FastAPI app: review endpoint, vault listing, health, admin console, and the static web apps."""

from __future__ import annotations

import base64
import binascii
import logging
import math
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware

from . import __version__, preflight
from .audit import AuditLog
from .config import ConfigError, Settings, get_settings
from .engine import Engine
from .ingest import IngestError, ingest_bytes
from .limits import FailureThrottle, HourlyLimiter
from .models import ReviewReport
from .runtime import BY_NAME, ENGINE_FIELDS, SECRETS, OverrideError, RuntimeConfig
from .users import MIN_PASSWORD_LENGTH, PasswordPolicyError, UserStore, generate_password
from .vault import VaultError, VaultRegistry

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UI_DIR = _ROOT / "ui"
DEFAULT_CLIENT_DIR = _ROOT / "client"

# Pages that are only a shell: they contain no data and log in with an explicit header.
_PUBLIC_SHELLS = {"/status", "/admin", "/admin/user"}


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    scheme, _, param = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(param.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else None


class ConfigUpdate(BaseModel):
    set: dict[str, Any] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)


class PasswordReset(BaseModel):
    """Exactly one of `password` (chosen by the admin) or `generate` (a random one)."""

    password: str | None = None
    generate: bool = False


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    registry: VaultRegistry | None = None,
    ui_dir: Path | None = DEFAULT_UI_DIR,
    client_dir: Path | None = DEFAULT_CLIENT_DIR,
    limiter: HourlyLimiter | None = None,
    overrides_path: Path | None = None,
    preflight_deps: dict[str, Any] | None = None,
    admin_throttle: FailureThrottle | None = None,
    users_path: Path | None = None,
) -> FastAPI:
    """Build the app. The engine is created lazily so the app can start (and report what is
    misconfigured on /healthz) before an inference endpoint is set.

    `preflight_deps` lets tests inject fake http/llm/search clients for the live status checks."""
    base = settings or get_settings()
    runtime = RuntimeConfig(base, overrides_path or base.audit_log_path.parent / "overrides.json")
    app = FastAPI(title="Sentinel Core", version=__version__)
    state: dict[str, object] = {"engine": engine, "registry": registry}
    lock = threading.Lock()
    live_lock = threading.Lock()
    audit = AuditLog(base.audit_log_path)
    limiter = limiter or HourlyLimiter(runtime.settings.max_reviews_per_hour)
    admin_throttle = admin_throttle or FailureThrottle()
    # The environment defines the first two users (admin, then the visitor login); they follow it
    # until an admin resets their password in the console.
    users = UserStore(users_path or base.audit_log_path.parent / "users.json")
    users.sync_env(
        admin_user=base.admin_user,
        admin_password=base.admin_password,
        visitor_user=base.access_user,
        visitor_password=base.access_password,
    )

    def cfg() -> Settings:
        return runtime.settings

    def get_registry() -> VaultRegistry:
        with lock:
            if state["registry"] is None:
                state["registry"] = VaultRegistry(base.vaults_dir)
            return state["registry"]  # type: ignore[return-value]

    def get_engine() -> Engine:
        with lock:
            if state["engine"] is None:
                state["engine"] = Engine.from_settings(cfg())
            return state["engine"]  # type: ignore[return-value]

    # ------------------------------------------------------------------ login middleware
    # /healthz stays reachable (container health checks, and so clients can learn whether a login
    # is needed) but only reveals configuration to authorized callers. The /status and /admin
    # pages are shells with no data; their API (/v1/admin/*) needs the admin login.
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path == "/healthz" or path in _PUBLIC_SHELLS:
            return await call_next(request)
        admin_scope = path.startswith("/v1/admin")
        if admin_scope:
            if not cfg().admin_password:
                return JSONResponse(
                    {"detail": "The admin console is disabled. Set ADMIN_PASSWORD to enable it."},
                    status_code=403,
                )
            # Checked before any password hashing, so guessing cannot be used to burn CPU.
            wait = admin_throttle.blocked_for()
            if wait is not None:
                return JSONResponse(
                    {"detail": "Too many failed admin sign-ins. Try again later."},
                    status_code=429,
                    headers={"Retry-After": str(math.ceil(wait))},
                )
        creds = _basic_credentials(request)
        # Password hashing is CPU-heavy on purpose: keep it off the event loop.
        user = await run_in_threadpool(users.authenticate, *creds) if creds else None
        request.state.user = user
        if admin_scope:
            if user is not None and user.role == "admin":
                admin_throttle.reset()
                return await call_next(request)
            if request.headers.get("authorization"):  # a wrong guess, not just a missing login
                admin_throttle.record_failure()
            return JSONResponse({"detail": "admin authentication required"}, status_code=401)
        if user is not None or not users.login_required():
            return await call_next(request)
        headers = {}
        # Only navigations get the browser's native login prompt; API calls from a page do not,
        # so a client app can show its own login form.
        if "text/html" in request.headers.get("accept", ""):
            headers["WWW-Authenticate"] = 'Basic realm="Sentinel", charset="UTF-8"'
        return JSONResponse({"detail": "authentication required"}, status_code=401, headers=headers)

    # Added after the login middleware so it is outermost: preflights and 401s carry CORS headers.
    if base.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=base.cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            expose_headers=["Retry-After"],
            max_age=600,
        )

    # ------------------------------------------------------------------ public API
    @app.get("/healthz")
    def healthz(request: Request) -> dict:
        s = cfg()
        login_required = users.login_required()
        public = {
            "status": "ok",
            "auth_required": login_required,
            "max_upload_mb": s.max_upload_mb,
        }
        creds = _basic_credentials(request)
        user = users.authenticate(*creds) if creds else None
        if login_required and user is None:
            return public
        try:
            vaults = len(get_registry())
        except VaultError as exc:
            vaults, vault_error = 0, str(exc)
        else:
            vault_error = None
        return {
            **public,
            "version": __version__,
            "llm_configured": bool(s.llm_base_url and s.llm_model),
            "llm_host": s.llm_host,
            "llm_model": s.llm_model,
            "search_configured": bool(s.tavily_api_key),
            "vaults": vaults,
            "vault_error": vault_error,
        }

    @app.get("/v1/vaults")
    def list_vaults() -> list[dict]:
        try:
            registry = get_registry()
        except VaultError as exc:
            raise HTTPException(500, f"vault directory is invalid: {exc}") from exc
        return [
            {
                "id": v.id,
                "title": v.title,
                "description": v.description,
                "language": v.language,
                "rules": [{"id": r.id, "title": r.title, "severity": r.severity} for r in v.rules],
            }
            for v in registry
        ]

    @app.post("/v1/reviews", response_model=ReviewReport)
    def create_review(
        vault_id: str = Form(...),
        file: UploadFile = File(...),
    ) -> ReviewReport:
        s = cfg()
        try:
            vault = get_registry().get(vault_id)
        except VaultError as exc:
            raise HTTPException(404, str(exc)) from exc

        limit = s.max_upload_mb * 1024 * 1024
        data = file.file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"file exceeds the {s.max_upload_mb} MB upload limit")
        try:
            doc = ingest_bytes(data, file.filename or "upload", max_chars=s.max_document_chars)
        except IngestError as exc:
            raise HTTPException(400, str(exc)) from exc

        try:
            eng = get_engine()
        except ConfigError as exc:
            raise HTTPException(503, str(exc)) from exc

        # Count only reviews that reach the model, so bad uploads do not use up the allowance.
        wait = limiter.try_acquire()
        if wait is not None:
            raise HTTPException(
                429,
                "The review limit for this deployment has been reached. Please try again later.",
                headers={"Retry-After": str(math.ceil(wait))},
            )
        return eng.review(doc, vault)

    # ------------------------------------------------------------------ admin API
    def admin_header(request: Request) -> None:
        """Every admin call needs a custom header, so a page on another site cannot trigger it."""
        if request.headers.get("x-sentinel-admin") != "1":
            raise HTTPException(403, "missing X-Sentinel-Admin header")

    def admin_write(request: Request) -> None:
        admin_header(request)
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "expected application/json")

    def status_payload(live: bool) -> dict:
        s = cfg()
        report = preflight.run(
            s,
            {},
            None,
            live=live,
            public=False,
            include_env_file=False,
            **(preflight_deps or {}),
        )
        listed = users.list()
        admins = sum(1 for u in listed if u["role"] == "admin")
        if users.load_error:
            report.add("users", "warn", users.load_error)
        else:
            report.add(
                "users",
                "pass",
                f"{len(listed)} user(s): {admins} admin, {len(listed) - admins} visitor; "
                f"visitor login {'required' if users.login_required() else 'OFF'}",
            )
        if runtime.load_error:
            report.add("admin overrides", "warn", runtime.load_error)
        elif runtime.overrides:
            report.add(
                "admin overrides",
                "pass",
                f"{len(runtime.overrides)} overridden: {', '.join(sorted(runtime.overrides))}",
            )
        return {
            "ready": report.ok,
            "live": live,
            "checked_at": datetime.now(UTC).isoformat(),
            "counts": {k: report.count(k) for k in ("pass", "warn", "fail", "skip")},
            "checks": [
                {"group": c.group, "name": c.name, "status": c.status, "detail": c.detail}
                for c in report.checks
            ],
        }

    @app.get("/v1/admin/status")
    def admin_status() -> dict:
        """Static checks only: no network calls, so it is free and instant."""
        return status_payload(live=False)

    @app.post("/v1/admin/status/live", dependencies=[Depends(admin_header)])
    def admin_status_live() -> dict:
        """Also contacts the model endpoint and Tavily (a few tokens and one search)."""
        if not live_lock.acquire(blocking=False):
            raise HTTPException(409, "a live check is already running")
        try:
            return status_payload(live=True)
        finally:
            live_lock.release()

    @app.get("/v1/admin/config")
    def admin_config() -> dict:
        return {"fields": runtime.describe(), "persisted": runtime.path is not None}

    @app.put("/v1/admin/config", dependencies=[Depends(admin_write)])
    def admin_update_config(update: ConfigUpdate, request: Request) -> dict:
        try:
            changed = runtime.apply(update.set, update.clear)
        except OverrideError as exc:
            raise HTTPException(422, {"errors": exc.errors}) from exc
        s = cfg()
        with lock:
            if ENGINE_FIELDS & set(changed):
                state["engine"] = None  # rebuilt lazily from the new settings
        limiter.limit = s.max_reviews_per_hour

        # Record what changed by name. Secret values and full URLs never reach the log.
        values: dict[str, Any] = {}
        for name in changed:
            if name in SECRETS:
                continue
            value = getattr(s, name)
            values[name] = urlparse(value).hostname if name == "llm_base_url" and value else value
        admin = (_basic_credentials(request) or ("?", ""))[0]
        try:
            audit.event(
                "config_change",
                admin=admin,
                changed=changed,
                cleared=[n for n in update.clear if n in BY_NAME],
                values=values,
                secrets_changed=sorted(SECRETS & set(changed)),
            )
        except OSError:
            log.exception("could not write the config_change audit event")
        return {"changed": changed, "fields": runtime.describe()}

    @app.get("/v1/admin/users")
    def admin_users() -> dict:
        """The authorized users: the environment-defined admin and visitor first. Never hashes."""
        return {"users": users.list(), "min_password_length": MIN_PASSWORD_LENGTH}

    @app.post("/v1/admin/users/{username}/password", dependencies=[Depends(admin_write)])
    def admin_reset_password(username: str, body: PasswordReset, request: Request) -> JSONResponse:
        """Reset a user's password: choose one, or generate a random one (returned once)."""
        if users.get(username) is None:
            raise HTTPException(404, "unknown user")
        if body.generate == (body.password is not None):
            raise HTTPException(422, {"errors": {"password": "provide a password or generate"}})
        password = generate_password() if body.generate else str(body.password)
        try:
            users.reset_password(username, password)
        except PasswordPolicyError as exc:
            raise HTTPException(422, {"errors": {"password": str(exc)}}) from exc
        try:
            audit.event(
                "user_password_reset",
                admin=request.state.user.username,
                username=username,
                generated=body.generate,
            )
        except OSError:
            log.exception("could not write the user_password_reset audit event")
        payload: dict[str, Any] = {"username": username, "generated": body.generate}
        if body.generate:
            payload["password"] = password  # shown once, never stored in the clear
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ pages
    if ui_dir is not None and ui_dir.is_dir():
        app.mount("/static", StaticFiles(directory=ui_dir), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(ui_dir / "index.html")

        def console() -> FileResponse:
            return FileResponse(ui_dir / "console.html", headers={"Cache-Control": "no-store"})

        app.add_api_route("/status", console, include_in_schema=False)
        app.add_api_route("/admin", console, include_in_schema=False)
        app.add_api_route("/admin/user", console, include_in_schema=False)

    if client_dir is not None and client_dir.is_dir():
        app.mount("/app", StaticFiles(directory=client_dir, html=True), name="client")

    return app


app = create_app()
