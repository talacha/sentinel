"""The client app's bundled samples must match the repo's samples, and the API must serve it."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from sentinel.api import create_app
from sentinel.config import Settings
from sentinel.vault import VaultRegistry

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"
REPO_SAMPLES = {p.name: p for p in (ROOT / "samples").rglob("*.txt")}


def test_bundled_samples_are_byte_identical_to_the_repo_samples():
    bundled = sorted((CLIENT / "samples").glob("*.txt"))
    assert bundled, "the client must ship at least one sample"
    for path in bundled:
        original = REPO_SAMPLES[path.name]  # KeyError here means the sample no longer exists
        assert path.read_bytes() == original.read_bytes(), f"{path.name} drifted from {original}"


def test_sample_manifest_points_at_real_files_and_real_vaults():
    manifest = json.loads((CLIENT / "samples" / "index.json").read_text())
    vaults = set(VaultRegistry(ROOT / "vaults").ids())
    assert manifest
    for entry in manifest:
        assert (CLIENT / "samples" / entry["file"]).is_file()
        assert entry["vault"] in vaults
        assert entry["label"]


def test_the_shipped_config_is_safe_by_default():
    config = (CLIENT / "config.js").read_text()
    assert 'apiBase: ""' in config
    assert "allowApiParam: false" in config  # a crafted ?api= link must not redirect uploads


def test_the_api_serves_the_client_app(tmp_path):
    settings = Settings(_env_file=None, audit_log_path=tmp_path / "audit" / "a.jsonl")
    client = TestClient(create_app(settings, ui_dir=None, client_dir=CLIENT))
    page = client.get("/app/")
    assert page.status_code == 200 and "Sentinel" in page.text
    for asset in ("app.js", "styles.css", "config.js", "samples/index.json"):
        assert client.get(f"/app/{asset}").status_code == 200, asset
