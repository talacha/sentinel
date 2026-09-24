"""Shared test setup."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Give every test its own audit/users/overrides directory.

    The app keeps `users.json` and `overrides.json` next to the audit log. Tests that do not set
    their own audit path would otherwise share `./audit/` in the repository and leak state (saved
    users, overrides) from one test into the next.
    """
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "isolated-state" / "audit.jsonl"))


@pytest.fixture(autouse=True)
def cheap_scrypt(request, monkeypatch):
    """Password hashing is deliberately slow; use a cheap cost so the suite stays fast.

    Tests marked `real_scrypt` keep the production parameters. Verification reads its parameters
    from the stored hash, so hashes made at either cost verify correctly.
    """
    if "real_scrypt" not in request.keywords:
        monkeypatch.setattr("sentinel.users._SCRYPT_N", 2**10)
