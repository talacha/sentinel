"""Keeps `scripts/stub_llm.py` honest: the local-run docs promise it reproduces the README story."""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest

from sentinel.engine import Engine
from sentinel.ingest import ingest_path
from sentinel.llm import OpenAICompatibleLLM
from sentinel.models import Verdict
from sentinel.vault import VaultRegistry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stub_url():
    spec = importlib.util.spec_from_file_location("stub_llm", ROOT / "scripts" / "stub_llm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.make_server(0)  # port 0: let the OS pick a free one
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def test_stub_reproduces_the_documented_insurance_outcomes_over_http(stub_url):
    llm = OpenAICompatibleLLM(stub_url, "stub")
    vault = VaultRegistry(ROOT / "vaults").get("insurance_life")
    doc = ingest_path(ROOT / "samples" / "insurance" / "life_underwriting_01.txt")

    report = Engine(llm, search=None).review(doc, vault)
    by = {r.rule_id: r for r in report.results}

    assert by["smoker-consistency"].verdict is Verdict.NO_CUMPLE
    assert by["income-multiple"].verdict is Verdict.REVISAR
    assert by["enhanced-review-threshold"].verdict is Verdict.REVISAR
    assert report.summary == {"cumple": 3, "no_cumple": 1, "revisar": 2}
