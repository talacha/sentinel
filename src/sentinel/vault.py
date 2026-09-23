"""Load and validate vaults (YAML rule files) from disk."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import Vault


class VaultError(ValueError):
    """A vault file is missing, unreadable, or invalid."""


def load_vault(path: Path | str) -> Vault:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VaultError(f"vault file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise VaultError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise VaultError(f"{path}: expected a mapping at the top level")
    try:
        return Vault.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise VaultError(f"{path}: {details}") from exc


class VaultRegistry:
    """All `*.yaml` vaults in a directory, keyed by vault id."""

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self._vaults: dict[str, Vault] = {}
        self.reload()

    def reload(self) -> None:
        vaults: dict[str, Vault] = {}
        for path in sorted(self.directory.glob("*.yaml")):
            vault = load_vault(path)
            if vault.id in vaults:
                raise VaultError(f"{path}: duplicate vault id {vault.id!r}")
            vaults[vault.id] = vault
        self._vaults = vaults

    def ids(self) -> list[str]:
        return list(self._vaults)

    def get(self, vault_id: str) -> Vault:
        try:
            return self._vaults[vault_id]
        except KeyError:
            raise VaultError(
                f"unknown vault {vault_id!r}; available: {', '.join(self._vaults) or 'none'}"
            ) from None

    def __iter__(self):
        return iter(self._vaults.values())

    def __len__(self) -> int:
        return len(self._vaults)
