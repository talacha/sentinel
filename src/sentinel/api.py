"""FastAPI app: review endpoint, vault listing, health, admin console, and the static web apps."""

from __future__ import annotations

import base64
import binascii
import logging
import math
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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
from .users import (
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    UserError,
    UserStore,
    generate_password,
)
from .vault import (
    MAX_VAULT_BYTES,
    VaultConflict,
    VaultError,
    VaultRegistry,
    VaultUnavailable,
    parse_vault,
)

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UI_DIR = _ROOT / "ui"
DEFAULT_CLIENT_DIR = _ROOT / "client"

# Pages that are only a shell: they contain no data and log in with an explicit header.
_PUBLIC_SHELLS = {"/status", "/admin", "/admin/config", "/admin/users", "/admin/vaults"}
# Earlier URLs, kept as redirects so bookmarks and links keep working.
_LEGACY_SHELLS = {"/admin/user", "/admin/vault"}


def _is_shell(path: str) -> bool:
    path = path.rstrip("/") or "/"
    return (
        path in _PUBLIC_SHELLS
        or path in _LEGACY_SHELLS
        or path.startswith(("/admin/vaults/", "/admin/vault/"))
    )


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


class VaultSave(BaseModel):
    """A new version of a vault, edited from `base_version` (used to refuse stale saves)."""

    yaml: str
    base_version: int
    note: str = ""


class VaultValidate(BaseModel):
    yaml: str


class UserCreate(BaseModel):
    """A new user. Exactly one of `password` (chosen by the admin) or `generate`."""

    username: str
    role: Literal["admin", "visitor"] = "visitor"
    password: str | None = None
    generate: bool = False


class UserUpdate(BaseModel):
    """Change a user's role and/or password (at most one of `password` and `generate`)."""

    role: Literal["admin", "visitor"] | None = None
    password: str | None = None
    generate: bool = False


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
    vaults_overlay: Path | None = None,
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
                # Edits are stored in a writable overlay next to the audit log; the shipped
                # files (often mounted read-only) are never modified.
                state["registry"] = VaultRegistry(
                    base.vaults_dir,
                    overlay=vaults_overlay or base.vaults_overlay_dir,
                )
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
        if request.method == "OPTIONS" or path == "/healthz" or _is_shell(path):
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
        try:
            registry = get_registry()
            edited = [v for v in registry.ids() if registry.info(v).source == "edited"]
            if registry.errors:
                report.add("vault edits", "warn", "; ".join(registry.errors))
            elif edited:
                report.add(
                    "vault edits",
                    "pass",
                    f"{len(edited)} edited in the console: {', '.join(edited)}",
                )
        except VaultError:
            pass  # a broken shipped vault directory is already reported by the vaults check
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

    def user_error(exc: UserError | PasswordPolicyError) -> HTTPException:
        if isinstance(exc, PasswordPolicyError):
            return HTTPException(422, {"errors": {"password": str(exc)}})
        return HTTPException(409 if exc.conflict else 422, {"errors": {exc.field: str(exc)}})

    def user_payload(
        username: str, generated: str | None, was_generated: bool, status: int = 200
    ) -> JSONResponse:
        payload: dict[str, Any] = {
            "user": users.describe(username),
            "generated": was_generated,
        }
        if generated is not None:
            payload["password"] = generated  # shown once, never stored in the clear
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    def audit_event(name: str, **fields: Any) -> None:
        try:
            audit.event(name, **fields)
        except OSError:
            log.exception("could not write the %s audit event", name)

    @app.post("/v1/admin/users", status_code=201, dependencies=[Depends(admin_write)])
    def admin_create_user(body: UserCreate, request: Request) -> JSONResponse:
        """Add a user, with a chosen password or a generated one (returned once)."""
        if body.generate == (body.password is not None):
            raise HTTPException(422, {"errors": {"password": "provide a password or generate"}})
        password = generate_password() if body.generate else str(body.password)
        try:
            users.create(body.username, body.role, password)
        except (UserError, PasswordPolicyError) as exc:
            raise user_error(exc) from exc
        audit_event(
            "user_created",
            admin=request.state.user.username,
            username=body.username,
            role=body.role,
            generated=body.generate,
        )
        return user_payload(
            body.username, password if body.generate else None, body.generate, status=201
        )

    @app.put("/v1/admin/users/{username}", dependencies=[Depends(admin_write)])
    def admin_update_user(username: str, body: UserUpdate, request: Request) -> JSONResponse:
        """Change a user's role and/or password, all or nothing. Nobody can change their own role,
        and the last admin, the last visitor, and the environment-defined users keep theirs."""
        if users.get(username) is None:
            raise HTTPException(404, "unknown user")
        if body.generate and body.password is not None:
            raise HTTPException(422, {"errors": {"password": "provide a password or generate"}})
        password = generate_password() if body.generate else body.password
        try:
            change = users.update(
                username,
                role=body.role,
                password=password,
                actor=request.state.user.username,
            )
        except (UserError, PasswordPolicyError) as exc:
            raise user_error(exc) from exc
        audit_event(
            "user_updated",
            admin=request.state.user.username,
            username=username,
            role_from=change.role_from,
            role_to=change.role_to,
            password_reset=change.password_changed,
            generated=body.generate,
        )
        return user_payload(username, password if body.generate else None, body.generate)

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

    # ------------------------------------------------------------------ vault admin API
    def vault_summary(vault_id: str) -> dict[str, Any]:
        registry = get_registry()
        vault, info = registry.get(vault_id), registry.info(vault_id)
        return {
            "id": vault.id,
            "title": vault.title,
            "description": vault.description,
            "language": vault.language,
            "rule_count": len(vault.rules),
            "version": info.version,
            "source": info.source,
            "updated_by": info.updated_by,
            "updated_at": info.updated_at,
            "note": info.note,
            "shipped_changed": info.shipped_changed,
        }

    def require_vault(vault_id: str) -> VaultRegistry:
        registry = get_registry()
        if vault_id not in registry.ids():
            raise HTTPException(404, f"unknown vault {vault_id!r}")
        return registry

    @app.get("/v1/admin/vaults")
    def admin_vaults() -> dict:
        registry = get_registry()
        return {
            "vaults": [vault_summary(v) for v in registry.ids()],
            "editable": registry.editable,
            "problems": registry.errors,
        }

    @app.get("/v1/admin/vaults/{vault_id}")
    def admin_vault(vault_id: str, version: int | None = None) -> dict:
        """One vault: its YAML (current, or an earlier `version`), rules, and version history."""
        registry = require_vault(vault_id)
        try:
            text = registry.text(vault_id, version)
        except VaultError as exc:
            raise HTTPException(404, str(exc)) from exc
        info = registry.info(vault_id)
        try:  # an old version might not validate under today's rules; still show its text
            rules = [
                {
                    "id": r.id,
                    "title": r.title,
                    "severity": r.severity,
                    "kind": "check" if r.check is not None else "criterion",
                    "external_check": r.external_check is not None,
                }
                for r in parse_vault(text, vault_id).rules
            ]
        except VaultError:
            rules = []
        return {
            **vault_summary(vault_id),
            "yaml": text,
            "viewing_version": version or info.version,
            "is_current": version is None or version == info.version,
            "rules": rules,
            "history": [
                {
                    "version": h.version,
                    "saved_by": h.saved_by,
                    "saved_at": h.saved_at,
                    "note": h.note,
                    "sha256": h.sha256[:12],
                }
                for h in reversed(registry.history(vault_id))
            ],
            "editable": registry.editable,
        }

    @app.post("/v1/admin/vaults/{vault_id}/validate", dependencies=[Depends(admin_write)])
    def admin_validate_vault(vault_id: str, body: VaultValidate) -> dict:
        """Check YAML without saving it: the same validation a save would apply."""
        require_vault(vault_id)
        try:
            vault = parse_vault(body.yaml, vault_id)
            if vault.id != vault_id:
                raise VaultError(
                    "the vault id cannot be changed",
                    [{"path": "id", "message": f"must stay {vault_id!r}"}],
                )
        except VaultError as exc:
            raise HTTPException(422, {"errors": exc.errors}) from exc
        return {"ok": True, "title": vault.title, "rule_count": len(vault.rules)}

    @app.put("/v1/admin/vaults/{vault_id}", dependencies=[Depends(admin_write)])
    def admin_save_vault(vault_id: str, body: VaultSave, request: Request) -> dict:
        """Save a new version of a vault. Validated like a shipped file; applies immediately."""
        registry = require_vault(vault_id)
        if len(body.yaml.encode("utf-8")) > MAX_VAULT_BYTES:
            raise HTTPException(
                413, {"errors": [{"path": "", "message": "the vault is too large"}]}
            )
        previous = registry.info(vault_id).version
        try:
            saved = registry.save(
                vault_id,
                body.yaml,
                saved_by=request.state.user.username,
                base_version=body.base_version,
                note=body.note,
            )
        except VaultConflict as exc:
            raise HTTPException(409, {"message": str(exc), "current": exc.current}) from exc
        except VaultUnavailable as exc:
            raise HTTPException(501, {"message": str(exc)}) from exc
        except VaultError as exc:
            raise HTTPException(422, {"errors": exc.errors}) from exc
        try:  # what changed, by hash: the vault text itself is not copied into the audit log
            audit.event(
                "vault_edit",
                admin=request.state.user.username,
                vault_id=vault_id,
                version=saved.version,
                previous_version=previous,
                sha256=saved.sha256,
                note=saved.note,
            )
        except OSError:
            log.exception("could not write the vault_edit audit event")
        return {**vault_summary(vault_id), "saved_version": saved.version}

    # ------------------------------------------------------------------ pages
    if ui_dir is not None and ui_dir.is_dir():
        app.mount("/static", StaticFiles(directory=ui_dir), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(ui_dir / "index.html")

        def console() -> FileResponse:
            return FileResponse(ui_dir / "console.html", headers={"Cache-Control": "no-store"})

        # One shell serves every console page: the overview, status, configuration, users, and
        # vaults (list, view, edit). The page reads its own URL to decide what to show.
        for page in (
            "/status",
            "/admin",
            "/admin/config",
            "/admin/users",
            "/admin/vaults",
            "/admin/vaults/{vault_id}",
            "/admin/vaults/{vault_id}/edit",
        ):
            app.add_api_route(page, console, include_in_schema=False)

        def moved(request: Request, target: str) -> RedirectResponse:
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(target + query, status_code=307)

        @app.get("/admin/user", include_in_schema=False)
        def legacy_users(request: Request) -> RedirectResponse:
            return moved(request, "/admin/users")

        @app.get("/admin/vault", include_in_schema=False)
        def legacy_vaults(request: Request) -> RedirectResponse:
            return moved(request, "/admin/vaults")

        @app.get("/admin/vault/{rest:path}", include_in_schema=False)
        def legacy_vault(rest: str, request: Request) -> RedirectResponse:
            return moved(request, "/admin/vaults/" + quote(rest.lstrip("/"), safe="/"))

    if client_dir is not None and client_dir.is_dir():
        app.mount("/app", StaticFiles(directory=client_dir, html=True), name="client")

    return app


app = create_app()
