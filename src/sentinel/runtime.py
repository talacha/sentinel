"""Runtime configuration: environment settings plus super-admin overrides persisted to disk.

Overrides go through the same validation as `.env`, are applied atomically (all or nothing), and
are saved with owner-only permissions so they survive a restart. Secrets are write-only: they are
accepted and stored but never returned or logged.

Deliberately NOT editable at runtime: file paths (`vaults_dir`, `audit_log_path`), CORS origins,
and the admin credentials. Those stay environment-only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from .config import Settings

log = logging.getLogger(__name__)


class OverrideError(ValueError):
    """An override was rejected; `errors` maps field name to a message (never echoing secrets)."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    group: str
    help: str
    secret: bool = False
    editable: bool = True


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "llm_base_url",
        "Model endpoint URL",
        "Model",
        "Where document text is sent. Changing this changes who processes your documents.",
    ),
    FieldSpec(
        "llm_model", "Model id", "Model", "Exactly as the endpoint lists it (case-sensitive)."
    ),
    FieldSpec("llm_api_key", "Model API key", "Model", "Write-only.", secret=True),
    FieldSpec(
        "llm_reasoning", "Reasoning mode", "Model", "Use the model's reasoning mode for judgements."
    ),
    FieldSpec(
        "llm_temperature_reasoning",
        "Reasoning temperature",
        "Model",
        "Sampling temperature when reasoning is on.",
    ),
    FieldSpec(
        "llm_max_tokens",
        "Max output tokens",
        "Model",
        "Budget per model call; reasoning needs headroom.",
    ),
    FieldSpec("llm_timeout_seconds", "Request timeout (s)", "Model", "Per model request."),
    FieldSpec(
        "llm_structured_mode",
        "Structured output mode",
        "Model",
        "How JSON is requested from the server.",
    ),
    FieldSpec(
        "tavily_api_key",
        "Tavily API key",
        "Search",
        "Write-only. Empty disables live verification.",
        secret=True,
    ),
    FieldSpec(
        "max_searches_per_review", "Searches per review", "Search", "Hard cap on outbound searches."
    ),
    FieldSpec("max_upload_mb", "Max upload (MB)", "Limits", "Largest accepted upload."),
    FieldSpec(
        "max_document_chars",
        "Max document characters",
        "Limits",
        "Longest extracted text accepted.",
    ),
    FieldSpec(
        "max_workers", "Concurrent rules", "Limits", "Rules reviewed in parallel per document."
    ),
    FieldSpec(
        "max_reviews_per_hour",
        "Reviews per hour",
        "Limits",
        "Global cap across all visitors; 0 = unlimited.",
    ),
    FieldSpec("access_user", "Visitor username", "Access", "Login for the review pages and API."),
    FieldSpec(
        "access_password",
        "Visitor password",
        "Access",
        "Write-only. Empty turns the visitor login off.",
        secret=True,
    ),
    # Shown for information; changing them needs a restart.
    FieldSpec("vaults_dir", "Vaults directory", "Environment", "Environment only.", editable=False),
    FieldSpec(
        "audit_log_path", "Audit log path", "Environment", "Environment only.", editable=False
    ),
    FieldSpec(
        "cors_allow_origins",
        "Allowed browser origins",
        "Environment",
        "Environment only.",
        editable=False,
    ),
)

BY_NAME = {f.name: f for f in FIELDS}
EDITABLE = frozenset(f.name for f in FIELDS if f.editable)
SECRETS = frozenset(f.name for f in FIELDS if f.secret)
# Changing any of these means the review engine (model client, search client) must be rebuilt.
ENGINE_FIELDS = frozenset(
    {
        "llm_base_url",
        "llm_model",
        "llm_api_key",
        "llm_reasoning",
        "llm_temperature_reasoning",
        "llm_max_tokens",
        "llm_timeout_seconds",
        "llm_structured_mode",
        "tavily_api_key",
        "max_searches_per_review",
        "max_workers",
    }
)


def _kind_and_limits(name: str) -> dict[str, Any]:
    info = Settings.model_fields[name]
    annotation = info.annotation
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) is typing.Literal:
        return {"kind": "choice", "choices": list(args)}
    base = next((a for a in (args or (annotation,)) if a is not type(None)), annotation)
    limits: dict[str, Any] = {}
    for meta in info.metadata:
        for key in ("ge", "le"):
            if getattr(meta, key, None) is not None:
                limits["min" if key == "ge" else "max"] = getattr(meta, key)
    if base is bool:
        return {"kind": "bool"}
    if base is int:
        return {"kind": "int", **limits}
    if base is float:
        return {"kind": "float", **limits}
    return {"kind": "text"}


def _friendly_error(err: dict[str, Any]) -> str:
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    if kind == "greater_than_equal":
        return f"must be at least {ctx.get('ge')}"
    if kind == "less_than_equal":
        return f"must be at most {ctx.get('le')}"
    if kind in {"int_parsing", "int_from_float"}:
        return "must be a whole number"
    if kind == "float_parsing":
        return "must be a number"
    if kind in {"bool_parsing", "bool_type"}:
        return "must be true or false"
    if kind == "literal_error":
        return "must be one of: " + ", ".join(
            str(a) for a in typing.get_args(Settings.model_fields[err["loc"][0]].annotation)
        )
    return str(err.get("msg", "invalid value"))  # pydantic's message; never includes the input


class RuntimeConfig:
    """The live settings: `base` (environment) with admin overrides applied on top."""

    def __init__(self, base: Settings, path: Path | None = None):
        self.base = base
        self.path = path
        self._lock = threading.RLock()
        self.load_error: str | None = None
        self.overrides: dict[str, Any] = self._load()
        try:
            self._settings = self._merge(self.overrides)
        except OverrideError as exc:
            self.load_error = f"saved overrides ignored (invalid): {exc}"
            log.error(self.load_error)
            self.overrides = {}
            self._settings = base

    @property
    def settings(self) -> Settings:
        return self._settings

    # -- persistence -------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self.path is None or not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw = data["overrides"]
            if not isinstance(raw, dict):
                raise TypeError("overrides must be an object")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.load_error = f"overrides file unreadable, ignored: {type(exc).__name__}"
            log.error("%s (%s)", self.load_error, self.path)
            return {}
        return {k: v for k, v in raw.items() if k in EDITABLE}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps({"version": 1, "overrides": self.overrides}, indent=2)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, self.path)  # atomic; the new file keeps the 0600 mode

    # -- applying ----------------------------------------------------------------------

    def _merge(self, overrides: dict[str, Any]) -> Settings:
        try:
            return Settings.model_validate({**self.base.model_dump(), **overrides})
        except ValidationError as exc:
            errors: dict[str, str] = {}
            for err in exc.errors():
                errors[str(err["loc"][0])] = _friendly_error(err)
            raise OverrideError(errors) from None

    def apply(self, set_values: dict[str, Any], clear: list[str] | None = None) -> list[str]:
        """Validate and apply a change; returns the names that actually changed."""
        clear = clear or []
        with self._lock:
            errors: dict[str, str] = {}
            for name in [*set_values, *clear]:
                if name not in BY_NAME:
                    errors[name] = "unknown setting"
                elif name not in EDITABLE:
                    errors[name] = "cannot be changed at runtime (set it in the environment)"
            for name in set_values:
                if name in SECRETS and not set_values[name]:
                    errors[name] = "empty; use clear to remove a secret"
                if name == "llm_base_url" and not _valid_url(set_values[name]):
                    errors[name] = "must be a full http(s) URL such as https://host/v1"
            if errors:
                raise OverrideError(errors)

            candidate = {**self.overrides, **set_values}
            for name in clear:
                candidate.pop(name, None)
            new_settings = self._merge(candidate)  # raises OverrideError; nothing applied yet

            changed = sorted(
                name
                for name in EDITABLE
                if getattr(new_settings, name) != getattr(self._settings, name)
            )
            self.overrides, self._settings = candidate, new_settings
            self.load_error = None
            self._save()
            return changed

    # -- presentation ------------------------------------------------------------------

    def describe(self) -> list[dict[str, Any]]:
        """Every setting with its metadata and current value. Secrets are never included."""
        out = []
        for spec in FIELDS:
            value = getattr(self._settings, spec.name)
            item: dict[str, Any] = {
                "name": spec.name,
                "label": spec.label,
                "group": spec.group,
                "help": spec.help,
                "editable": spec.editable,
                "secret": spec.secret,
                "overridden": spec.name in self.overrides,
                **_kind_and_limits(spec.name),
            }
            if spec.secret:
                item["is_set"] = bool(value)
                item["length"] = len(value) if value else 0
            else:
                item["value"] = str(value) if isinstance(value, Path) else value
            out.append(item)
        return out


def _valid_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
