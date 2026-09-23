"""Offline consistency checks between the shipped vaults, samples, and eval expectations."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sentinel.ingest import ingest_path
from sentinel.models import Verdict
from sentinel.vault import VaultRegistry

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = VaultRegistry(ROOT / "vaults")
CASES = yaml.safe_load((ROOT / "evals" / "expected.yaml").read_text())["cases"]


def test_three_domains_ship_and_load():
    assert set(REGISTRY.ids()) == {"insurance_life", "legal_contract", "health_patient"}


def test_every_vault_is_domain_data_only():
    # The engine is domain-agnostic: vaults differ only in rules, never in structure.
    for vault in REGISTRY:
        assert vault.rules
        for rule in vault.rules:
            assert (rule.check is None) != (rule.criterion is None)


def test_external_checks_only_use_vault_params_and_year():
    for vault in REGISTRY:
        allowed = set(vault.params) | {"year"}
        for rule in vault.rules:
            if rule.external_check:
                assert rule.external_check.placeholders() <= allowed


@pytest.mark.parametrize("case", CASES, ids=lambda c: Path(c["sample"]).stem)
def test_expectations_match_vault_and_sample(case):
    vault = REGISTRY.get(case["vault"])
    doc = ingest_path(ROOT / case["sample"])
    # Every rule in the vault has an expectation, and no expectation names a missing rule.
    assert set(case["expect"]) == {r.id for r in vault.rules}
    for rule_id, expect in case["expect"].items():
        verdicts = expect["verdict"] if isinstance(expect["verdict"], list) else [expect["verdict"]]
        assert {Verdict(v) for v in verdicts}  # valid verdict names
        for quote in expect.get("quotes", []):
            assert doc.locate(quote) is not None, f"{rule_id}: {quote!r} not in {case['sample']}"


def test_samples_are_marked_synthetic():
    for case in CASES:
        head = (ROOT / case["sample"]).read_text()[:400].lower()
        assert "synthetic sample" in head


# ------------------------------------------------------------ shipped vaults through the engine
# A scripted model supplies the facts a real model would read from each sample; the shipped
# vault YAML (check expressions, references, on_fail, external_check) runs for real.

FACTS = {
    "life_underwriting_01": {
        "sum_insured": (3000000, "Sum insured requested: MXN 3,000,000"),
        "declared_income": (150000, "Annual income: MXN 150,000"),
        "financial_questionnaire_attached": (False, "Financial questionnaire attached: No"),
        "age": (45, "Age: 45"),
    },
    "life_underwriting_02_clean": {
        "sum_insured": (900000, "Sum insured requested: MXN 900,000"),
        "declared_income": (720000, "Annual income: MXN 720,000"),
        "financial_questionnaire_attached": (False, "Financial questionnaire attached: No"),
        "age": (35, "Age: 35"),
    },
    "msa_01": {
        "governing_law": ("State of Texas", "governed by the laws of the State of Texas"),
        "liability_cap": (50000, "shall not exceed USD 50,000"),
        "annual_fees": (120000, "Annual fees: USD 120,000"),
        "late_interest_pct_annual": (18, "Late payments accrue interest at 18% per annum"),
        "notice_days": (15, "upon 15 days' written notice"),
    },
    "patient_file_01": {
        "admission_date": ("2026-05-02", "Admission date: 2026-05-02"),
        "discharge_date": ("2026-05-06", "Discharge date: 2026-05-06"),
        "daily_mme": (180, "Total daily MME: 180"),
    },
}


def _accepted(expect):
    return expect["verdict"] if isinstance(expect["verdict"], list) else [expect["verdict"]]


def oracle(case, vault):
    from sentinel.llm import FakeLLM

    facts = FACTS[Path(case["sample"]).stem]
    by_title = {r.title: r.id for r in vault.rules}

    def handler(system, user, schema, reasoning):
        name = schema.__name__
        if name == "ExtractionOut":
            return {
                "facts": [
                    {"name": n, "found": True, "value": v, "quote": q}
                    for n, (v, q) in facts.items()
                ]
            }
        if name == "JudgementOut":
            title = next(t for t in by_title if f"Rule: {t}\n" in user)
            expect = case["expect"][by_title[title]]
            verdict = _accepted(expect)[0]
            return {
                "verdict": verdict,
                "rationale": "scripted",
                "evidence": expect.get("quotes", []) if verdict != "revisar" else [],
            }
        if name == "SourceJudgement":
            return {"status": "unclear", "rationale": "no clear source", "supporting_urls": []}
        raise AssertionError(name)

    return FakeLLM(handler)


@pytest.mark.parametrize("case", CASES, ids=lambda c: Path(c["sample"]).stem)
def test_shipped_vault_and_sample_produce_expected_verdicts_offline(case):
    from sentinel.engine import Engine
    from sentinel.verify import FakeSearch

    vault = REGISTRY.get(case["vault"])
    doc = ingest_path(ROOT / case["sample"])
    search = FakeSearch([])
    report = Engine(oracle(case, vault), search).review(doc, vault)

    for result in report.results:
        expect = case["expect"][result.rule_id]
        assert result.verdict.value in _accepted(expect), f"{result.rule_id}: {result.rationale}"

    # Every search that left the perimeter came from a vault template (no document content).
    for q in search.queries:
        assert not any(w in q["query"].lower() for w in ("mendoza", "texas", "amoxicillin"))


def test_insurance_sample_matches_the_readme_story_offline():
    from sentinel.engine import Engine
    from sentinel.verify import FakeSearch

    case = next(c for c in CASES if c["sample"].endswith("life_underwriting_01.txt"))
    vault = REGISTRY.get("insurance_life")
    doc = ingest_path(ROOT / case["sample"])
    report = Engine(oracle(case, vault), FakeSearch([])).review(doc, vault)
    by = {r.rule_id: r for r in report.results}
    assert by["smoker-consistency"].verdict is Verdict.NO_CUMPLE
    assert by["income-multiple"].verdict is Verdict.REVISAR
    assert by["enhanced-review-threshold"].verdict is Verdict.REVISAR
    assert by["enhanced-review-threshold"].external is not None
    assert report.summary == {"cumple": 3, "no_cumple": 1, "revisar": 2}
