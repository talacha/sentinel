"""The bundled documents a visitor can review without supplying a file of their own."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sentinel.api import create_app
from sentinel.config import Settings
from sentinel.engine import Engine
from sentinel.llm import FakeLLM
from sentinel.samples import load_samples
from sentinel.vault import VaultRegistry

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
VAULTS = ROOT / "vaults"


@pytest.fixture
def client():
    settings = Settings(_env_file=None, vaults_dir=VAULTS, samples_dir=SAMPLES)
    engine = Engine(FakeLLM(lambda *a, **k: {}), search=None)
    return TestClient(create_app(settings, engine, VaultRegistry(VAULTS), ui_dir=None))


def test_endpoint_serves_the_text_of_every_shipped_sample(client):
    body = client.get("/v1/samples").json()
    assert body, "no samples were served"
    for sample in body:
        on_disk = (SAMPLES / sample["path"]).read_text(encoding="utf-8")
        assert sample["text"] == on_disk, f"{sample['id']} does not match the file on disk"


def test_every_sample_names_a_shipped_vault(client):
    vault_ids = {v["id"] for v in client.get("/v1/vaults").json()}
    for sample in client.get("/v1/samples").json():
        assert sample["vault_id"] in vault_ids, sample["id"]


def test_manifest_covers_every_file_in_the_samples_directory():
    """A new sample file that nobody wired up should fail loudly, not stay invisible."""
    listed = {s.path for s in load_samples(SAMPLES)}
    on_disk = {str(p.relative_to(SAMPLES)) for p in SAMPLES.rglob("*.txt")}
    assert on_disk - listed == set(), "sample file(s) missing from the manifest"


def test_samples_are_served_without_a_path_parameter(client):
    """Content is inlined by the collection endpoint: no caller input reaches the filesystem."""
    assert all(
        "{" not in route.path for route in client.app.routes if route.path.startswith("/v1/samples")
    )


def test_a_missing_samples_directory_is_not_an_error(tmp_path):
    settings = Settings(_env_file=None, vaults_dir=VAULTS, samples_dir=tmp_path / "absent")
    engine = Engine(FakeLLM(lambda *a, **k: {}), search=None)
    app = TestClient(create_app(settings, engine, VaultRegistry(VAULTS), ui_dir=None))
    resp = app.get("/v1/samples")
    assert resp.status_code == 200
    assert resp.json() == []
