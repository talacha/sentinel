"""FastAPI app: review endpoint, vault listing, health, and the static web UI."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import ConfigError, Settings, get_settings
from .engine import Engine
from .ingest import IngestError, ingest_bytes
from .models import ReviewReport
from .vault import VaultError, VaultRegistry

log = logging.getLogger(__name__)

DEFAULT_UI_DIR = Path(__file__).resolve().parents[2] / "ui"


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    registry: VaultRegistry | None = None,
    ui_dir: Path | None = DEFAULT_UI_DIR,
) -> FastAPI:
    """Build the app. The engine is created lazily so the app can start (and report what is
    misconfigured on /healthz) before an inference endpoint is set."""
    settings = settings or get_settings()
    app = FastAPI(title="Sentinel Core", version=__version__)
    state: dict[str, object] = {"engine": engine, "registry": registry}
    lock = threading.Lock()

    def get_registry() -> VaultRegistry:
        with lock:
            if state["registry"] is None:
                state["registry"] = VaultRegistry(settings.vaults_dir)
            return state["registry"]  # type: ignore[return-value]

    def get_engine() -> Engine:
        with lock:
            if state["engine"] is None:
                state["engine"] = Engine.from_settings(settings)
            return state["engine"]  # type: ignore[return-value]

    @app.get("/healthz")
    def healthz() -> dict:
        try:
            vaults = len(get_registry())
        except VaultError as exc:
            vaults, vault_error = 0, str(exc)
        else:
            vault_error = None
        return {
            "status": "ok",
            "version": __version__,
            "llm_configured": bool(settings.llm_base_url and settings.llm_model),
            "llm_host": settings.llm_host,
            "llm_model": settings.llm_model,
            "search_configured": bool(settings.tavily_api_key),
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
        try:
            vault = get_registry().get(vault_id)
        except VaultError as exc:
            raise HTTPException(404, str(exc)) from exc

        limit = settings.max_upload_mb * 1024 * 1024
        data = file.file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"file exceeds the {settings.max_upload_mb} MB upload limit")
        try:
            doc = ingest_bytes(
                data, file.filename or "upload", max_chars=settings.max_document_chars
            )
        except IngestError as exc:
            raise HTTPException(400, str(exc)) from exc

        try:
            eng = get_engine()
        except ConfigError as exc:
            raise HTTPException(503, str(exc)) from exc
        return eng.review(doc, vault)

    if ui_dir is not None and ui_dir.is_dir():
        app.mount("/static", StaticFiles(directory=ui_dir), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(ui_dir / "index.html")

    return app


app = create_app()
