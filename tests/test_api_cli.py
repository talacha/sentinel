from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient
from fpdf import FPDF

from sentinel import cli
from sentinel.api import create_app
from sentinel.config import Settings
from sentinel.engine import Engine
from sentinel.llm import FakeLLM
from sentinel.vault import VaultRegistry

VAULT = {
    "id": "demo",
    "title": "Demo vault",
    "description": "Two rules.",
    "rules": [
        {
            "id": "smoker",
            "title": "Tobacco declaration",
            "severity": "high",
            "criterion": "Declaration must match lab results.",
        },
        {
            "id": "income",
            "title": "Income multiple",
            "facts": [
                {"name": "sum_insured", "type": "number", "description": "sum"},
                {"name": "income", "type": "number", "description": "income"},
            ],
            "check": "sum_insured <= 15 * income",
            "on_fail": "revisar",
        },
    ],
}

TEXT = "Tobacco use: No\nCotinine: POSITIVE\nSum insured: 1,000,000\nIncome: 500,000\n"


def handler(system, user, schema, reasoning):
    if schema.__name__ == "JudgementOut":
        return {
            "verdict": "no_cumple",
            "rationale": "Conflict.",
            "evidence": ["Tobacco use: No", "Cotinine: POSITIVE"],
        }
    return {
        "facts": [
            {
                "name": "sum_insured",
                "found": True,
                "value": 1000000,
                "quote": "Sum insured: 1,000,000",
            },
            {"name": "income", "found": True, "value": 500000, "quote": "Income: 500,000"},
        ]
    }


@pytest.fixture
def vault_dir(tmp_path):
    d = tmp_path / "vaults"
    d.mkdir()
    (d / "demo.yaml").write_text(yaml.safe_dump(VAULT))
    return d


@pytest.fixture
def client(vault_dir, tmp_path):
    settings = Settings(_env_file=None, vaults_dir=vault_dir, max_upload_mb=1)
    engine = Engine(FakeLLM(handler), search=None)
    return TestClient(create_app(settings, engine, VaultRegistry(vault_dir), ui_dir=None))


def post(client, data=None, vault_id="demo", name="app.txt"):
    data = TEXT.encode() if data is None else data
    return client.post("/v1/reviews", data={"vault_id": vault_id}, files={"file": (name, data)})


def test_review_endpoint_returns_report(client):
    resp = post(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["vault_id"] == "demo"
    assert body["summary"] == {"cumple": 1, "no_cumple": 1, "revisar": 0}
    smoker = next(r for r in body["results"] if r["rule_id"] == "smoker")
    assert smoker["verdict"] == "no_cumple" and smoker["evidence"][0]["page"] == 1


def test_review_accepts_pdf(client):
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    pdf.add_page()
    pdf.multi_cell(0, 8, TEXT)
    resp = post(client, bytes(pdf.output()), name="app.pdf")
    assert resp.status_code == 200
    assert resp.json()["summary"]["no_cumple"] == 1


def test_unknown_vault_is_404_and_lists_available(client):
    resp = post(client, vault_id="nope")
    assert resp.status_code == 404 and "demo" in resp.json()["detail"]


def test_empty_document_is_400(client):
    resp = post(client, b"   ")
    assert resp.status_code == 400 and "no extractable text" in resp.json()["detail"]


def test_oversized_upload_is_413(client):
    resp = post(client, b"x" * (1024 * 1024 + 10))
    assert resp.status_code == 413


def test_missing_llm_config_is_503_not_a_crash(vault_dir, monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    settings = Settings(_env_file=None, vaults_dir=vault_dir)
    client = TestClient(create_app(settings, None, VaultRegistry(vault_dir), ui_dir=None))
    resp = post(client)
    assert resp.status_code == 503 and "LLM_BASE_URL" in resp.json()["detail"]
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["llm_configured"] is False


def test_healthz_reports_host_not_secrets(vault_dir):
    settings = Settings(
        _env_file=None,
        vaults_dir=vault_dir,
        llm_base_url="https://gpu.internal:8000/v1",
        llm_model="m",
        llm_api_key="super-secret",
        tavily_api_key="tvly-secret",
    )
    client = TestClient(create_app(settings, None, VaultRegistry(vault_dir), ui_dir=None))
    raw = client.get("/healthz").text
    body = json.loads(raw)
    assert body["llm_host"] == "gpu.internal:8000" and body["search_configured"] is True
    assert "super-secret" not in raw and "tvly-secret" not in raw


def test_vault_listing(client):
    vaults = client.get("/v1/vaults").json()
    assert vaults[0]["id"] == "demo"
    assert [r["id"] for r in vaults[0]["rules"]] == ["smoker", "income"]


def test_ui_is_served(vault_dir):
    from pathlib import Path

    ui = Path(__file__).resolve().parents[1] / "ui"
    settings = Settings(_env_file=None, vaults_dir=vault_dir)
    client = TestClient(create_app(settings, None, VaultRegistry(vault_dir), ui_dir=ui))
    index = client.get("/")
    assert index.status_code == 200 and "Sentinel Core" in index.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(vault_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(
        Engine, "from_settings", classmethod(lambda cls, s: Engine(FakeLLM(handler)))
    )
    monkeypatch.setenv("VAULTS_DIR", str(vault_dir))
    cli.get_settings.cache_clear()
    doc = tmp_path / "app.txt"
    doc.write_text(TEXT)
    yield doc
    cli.get_settings.cache_clear()


def test_cli_review_prints_verdicts_and_exits_2_on_findings(cli_env, capsys):
    code = cli.main(["review", str(cli_env), "--vault", "demo"])
    out = capsys.readouterr().out
    assert code == 2
    assert "NO_CUMPLE" in out and "Tobacco use: No" in out and "summary:" in out


def test_cli_json_output_is_a_full_report(cli_env, capsys):
    cli.main(["review", str(cli_env), "--vault", "demo", "--json"])
    assert json.loads(capsys.readouterr().out)["vault_id"] == "demo"


def test_cli_reports_errors_with_exit_1(cli_env, capsys):
    assert cli.main(["review", str(cli_env), "--vault", "nope"]) == 1
    assert "unknown vault" in capsys.readouterr().err
    assert cli.main(["review", "/does/not/exist.txt", "--vault", "demo"]) == 1


def test_cli_lists_vaults(cli_env, capsys):
    assert cli.main(["vaults"]) == 0
    assert "demo" in capsys.readouterr().out
