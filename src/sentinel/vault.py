"""Load, validate, and version vaults (YAML rule files).

Shipped vaults are read-only files in `vaults/`. An admin can edit a vault at runtime: each save
is validated exactly like a shipped file and stored as a new numbered version in a writable
*overlay* directory (one folder per vault). The overlay takes precedence over the shipped file,
which is never modified. Version 1 is always the shipped file; overlay versions start at 2.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from .models import Vault

log = logging.getLogger(__name__)

MAX_VAULT_BYTES = 256 * 1024
MAX_NOTE_CHARS = 200


class VaultError(ValueError):
    """A vault file is missing, unreadable, or invalid.

    `errors` is a list of `{"path", "message"}` (plus `line`/`column` for YAML syntax errors),
    for an editor to point at the problem.
    """

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.errors = errors or [{"path": "", "message": message}]


class VaultConflict(VaultError):
    """Someone else saved a newer version since the editor loaded this one."""

    def __init__(self, current: int):
        super().__init__(f"the vault changed: version {current} is now current")
        self.current = current


class VaultUnavailable(VaultError):
    """Editing is not configured (no writable overlay directory)."""


def parse_vault(text: str, label: str = "vault") -> Vault:
    """Parse and validate vault YAML. `label` prefixes error messages, for example a file path."""
    if len(text.encode("utf-8")) > MAX_VAULT_BYTES:
        raise VaultError(f"{label}: vault is larger than {MAX_VAULT_BYTES // 1024} KB")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        error: dict[str, Any] = {"path": "", "message": f"invalid YAML: {exc}"}
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            error.update(line=mark.line + 1, column=mark.column + 1)
            error["message"] = f"invalid YAML: {getattr(exc, 'problem', exc)}"
        raise VaultError(f"{label}: invalid YAML: {exc}", [error]) from exc
    if not isinstance(raw, dict):
        raise VaultError(f"{label}: expected a mapping at the top level")
    try:
        return Vault.model_validate(raw)
    except ValidationError as exc:
        errors = [
            {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
        ]
        details = "; ".join(f"{e['path']}: {e['message']}" for e in errors)
        raise VaultError(f"{label}: {details}", errors) from exc


def load_vault(path: Path | str) -> Vault:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VaultError(f"vault file not found: {path}") from exc
    return parse_vault(text, str(path))


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class VersionInfo:
    version: int
    saved_by: str | None  # None for the shipped version
    saved_at: str | None
    note: str
    sha256: str


@dataclass(frozen=True)
class VaultInfo:
    version: int
    source: Literal["shipped", "edited"]
    updated_by: str | None
    updated_at: str | None
    note: str
    shipped_changed: bool  # the shipped file changed since this vault was first edited


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class VaultRegistry:
    """All `*.yaml` vaults in a directory, keyed by vault id, plus optional edited versions."""

    def __init__(self, directory: Path | str, overlay: Path | str | None = None):
        self.directory = Path(directory)
        self.overlay = Path(overlay) if overlay is not None else None
        self.errors: list[str] = []  # problems found in the overlay (it never blocks startup)
        self._lock = threading.RLock()
        self._vaults: dict[str, Vault] = {}
        self._texts: dict[str, str] = {}
        self._shipped: dict[str, str] = {}
        self._history: dict[str, list[VersionInfo]] = {}
        self._info: dict[str, VaultInfo] = {}
        self._pinned: dict[str, str] = {}  # sha256 of the shipped file when first edited
        self.reload()

    # -- loading -----------------------------------------------------------------------

    def reload(self) -> None:
        vaults: dict[str, Vault] = {}
        texts: dict[str, str] = {}
        shipped: dict[str, str] = {}
        history: dict[str, list[VersionInfo]] = {}
        info: dict[str, VaultInfo] = {}
        pinned: dict[str, str] = {}
        errors: list[str] = []

        for path in sorted(self.directory.glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            vault = parse_vault(text, str(path))
            if vault.id in vaults:
                raise VaultError(f"{path}: duplicate vault id {vault.id!r}")
            vaults[vault.id], texts[vault.id], shipped[vault.id] = vault, text, text
            history[vault.id] = [VersionInfo(1, None, None, "Shipped version", _sha256(text))]
            info[vault.id] = VaultInfo(1, "shipped", None, None, "", False)

        if self.overlay is not None and self.overlay.is_dir():
            for folder in sorted(p for p in self.overlay.iterdir() if p.is_dir()):
                try:
                    self._load_overlay(folder, vaults, texts, shipped, history, info, pinned)
                except (OSError, ValueError, KeyError, IndexError, VaultError) as exc:
                    errors.append(f"{folder.name}: saved edits ignored ({exc})")
                    log.error("vault overlay %s ignored: %s", folder, exc)

        with self._lock:
            self._vaults, self._texts, self._shipped = vaults, texts, shipped
            self._history, self._info, self._pinned = history, info, pinned
            self.errors = errors

    def _load_overlay(self, folder, vaults, texts, shipped, history, info, pinned) -> None:
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
        versions = [
            VersionInfo(
                int(v["version"]),
                v.get("saved_by"),
                v.get("saved_at"),
                v.get("note", ""),
                v["sha256"],
            )
            for v in index["versions"]
        ]
        current = versions[-1]
        text = (folder / f"v{current.version}.yaml").read_text(encoding="utf-8")
        vault = parse_vault(text, f"{folder.name} v{current.version}")
        if vault.id != folder.name:
            raise VaultError(f"vault id {vault.id!r} does not match its folder")
        vid = vault.id
        pin = index.get("shipped_sha256")
        vaults[vid], texts[vid] = vault, text
        history[vid] = [*history.get(vid, []), *versions]
        if pin:
            pinned[vid] = pin
        shipped_changed = bool(vid in shipped and pin and _sha256(shipped[vid]) != pin)
        info[vid] = VaultInfo(
            current.version,
            "edited",
            current.saved_by,
            current.saved_at,
            current.note,
            shipped_changed,
        )

    # -- reading -----------------------------------------------------------------------

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._vaults)

    def get(self, vault_id: str) -> Vault:
        with self._lock:
            try:
                return self._vaults[vault_id]
            except KeyError:
                raise VaultError(
                    f"unknown vault {vault_id!r}; available: {', '.join(self._vaults) or 'none'}"
                ) from None

    def info(self, vault_id: str) -> VaultInfo:
        self.get(vault_id)
        with self._lock:
            return self._info[vault_id]

    def history(self, vault_id: str) -> list[VersionInfo]:
        self.get(vault_id)
        with self._lock:
            return list(self._history[vault_id])

    def text(self, vault_id: str, version: int | None = None) -> str:
        """The YAML of the current version, or of an earlier `version`."""
        self.get(vault_id)
        with self._lock:
            current = self._info[vault_id].version
            if version is None or version == current:
                return self._texts[vault_id]
            if version == 1 and vault_id in self._shipped:
                return self._shipped[vault_id]
        if self.overlay is None or version is None or version < 2:
            raise VaultError(f"vault {vault_id!r} has no version {version}")
        try:
            return (self.overlay / vault_id / f"v{version}.yaml").read_text(encoding="utf-8")
        except OSError:
            raise VaultError(f"vault {vault_id!r} has no version {version}") from None

    @property
    def editable(self) -> bool:
        return self.overlay is not None

    # -- editing -----------------------------------------------------------------------

    def save(
        self, vault_id: str, text: str, *, saved_by: str, base_version: int, note: str = ""
    ) -> VersionInfo:
        """Validate `text` and store it as the next version. All or nothing."""
        if self.overlay is None:
            raise VaultUnavailable("editing is not available on this deployment")
        with self._lock:
            self.get(vault_id)
            current = self._info[vault_id].version
            if base_version != current:
                raise VaultConflict(current)
            vault = parse_vault(text, vault_id)
            if vault.id != vault_id:
                raise VaultError(
                    f"the vault id cannot be changed (expected {vault_id!r}, found {vault.id!r})",
                    [{"path": "id", "message": f"must stay {vault_id!r}"}],
                )
            version = current + 1
            folder = self.overlay / vault_id
            if self.overlay.resolve() not in folder.resolve().parents:
                raise VaultError("invalid vault id")
            now = datetime.now(UTC).isoformat(timespec="seconds")
            entry = {
                "version": version,
                "saved_by": saved_by,
                "saved_at": now,
                "note": note.strip()[:MAX_NOTE_CHARS],
                "sha256": _sha256(text),
            }
            index_path = folder / "index.json"
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                index = {"versions": []}
            if vault_id in self._shipped:
                index.setdefault("shipped_sha256", _sha256(self._shipped[vault_id]))
            index["versions"].append(entry)
            _atomic_write(folder / f"v{version}.yaml", text)  # the version first, then the index
            _atomic_write(index_path, json.dumps(index, indent=2))

            self._vaults[vault_id] = vault
            self._texts[vault_id] = text
            pin = index.get("shipped_sha256")
            self._pinned[vault_id] = pin or ""
            info = VaultInfo(
                version,
                "edited",
                saved_by,
                now,
                entry["note"],
                bool(vault_id in self._shipped and pin and _sha256(self._shipped[vault_id]) != pin),
            )
            self._info[vault_id] = info
            saved = VersionInfo(version, saved_by, now, entry["note"], entry["sha256"])
            self._history[vault_id].append(saved)
            return saved

    def __iter__(self):
        with self._lock:
            return iter(list(self._vaults.values()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._vaults)
