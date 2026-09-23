from __future__ import annotations

import pytest

from sentinel.compare import Claim, Draft
from sentinel.ingest import ingest_bytes
from sentinel.models import ExternalStatus, ExternalVerification, Rule, Verdict
from sentinel.policy import finalize

DOC = ingest_bytes(b"Tobacco use: No\nCotinine (urine): POSITIVE\nSum insured: 3,000,000", "a.txt")

CRITERION = Rule.model_validate({"id": "r", "title": "R", "criterion": "c"})
EXTERNAL_RULE = Rule.model_validate(
    {
        "id": "ext",
        "title": "Ext",
        "references": {"limit": 2_000_000},
        "facts": [{"name": "s", "type": "number", "description": "d"}],
        "check": "s < limit",
        "external_check": {"statement": "x", "query_template": "q {year}"},
    }
)


def draft(verdict, *quotes, check_failed=False):
    return Draft(verdict, "Model says so.", [Claim(q) for q in quotes], check_failed=check_failed)


def ext(status):
    return ExternalVerification(query="q 2026", status=status, rationale="r")


def test_verified_evidence_keeps_verdict_and_locates_quotes():
    res = finalize(
        CRITERION, draft(Verdict.NO_CUMPLE, "tobacco use: no", "COTININE (urine): positive"), DOC
    )
    assert res.verdict is Verdict.NO_CUMPLE
    assert [e.quote for e in res.evidence] == ["Tobacco use: No", "Cotinine (urine): POSITIVE"]
    assert all(e.page == 1 and e.start is not None for e in res.evidence)


def test_fabricated_quote_downgrades_to_revisar_even_if_others_are_real():
    res = finalize(
        CRITERION, draft(Verdict.NO_CUMPLE, "Tobacco use: No", "Applicant smokes daily"), DOC
    )
    assert res.verdict is Verdict.REVISAR
    assert "could not be verified" in res.rationale and "Applicant smokes daily" in res.rationale


@pytest.mark.parametrize("verdict", [Verdict.CUMPLE, Verdict.NO_CUMPLE])
def test_conclusive_verdict_without_evidence_becomes_revisar(verdict):
    res = finalize(CRITERION, draft(verdict), DOC)
    assert res.verdict is Verdict.REVISAR
    assert "No verifiable evidence" in res.rationale


def test_revisar_without_evidence_stays_revisar_and_is_not_annotated_as_downgrade():
    res = finalize(CRITERION, draft(Verdict.REVISAR), DOC)
    assert res.verdict is Verdict.REVISAR
    assert "No verifiable evidence" not in res.rationale


def test_duplicate_quotes_are_deduplicated():
    res = finalize(CRITERION, draft(Verdict.CUMPLE, "Tobacco use: No", "tobacco use: no"), DOC)
    assert len(res.evidence) == 1


def test_policy_never_upgrades():
    res = finalize(CRITERION, draft(Verdict.REVISAR, "Tobacco use: No"), DOC)
    assert res.verdict is Verdict.REVISAR


def failed_check(verdict=Verdict.NO_CUMPLE):
    return draft(verdict, "Sum insured: 3,000,000", check_failed=True)


def test_failed_check_stands_only_when_reference_confirmed():
    res = finalize(EXTERNAL_RULE, failed_check(), DOC, ext(ExternalStatus.CONFIRMED))
    assert res.verdict is Verdict.NO_CUMPLE
    assert "confirmed by a public source" in res.rationale
    assert res.external.status is ExternalStatus.CONFIRMED


@pytest.mark.parametrize(
    "status", [ExternalStatus.CONTRADICTED, ExternalStatus.UNCLEAR, ExternalStatus.UNAVAILABLE]
)
def test_failed_check_downgrades_when_reference_not_confirmed(status):
    res = finalize(EXTERNAL_RULE, failed_check(), DOC, ext(status))
    assert res.verdict is Verdict.REVISAR


def test_unavailable_verification_surfaces_the_actual_reason():
    external = ExternalVerification(
        status=ExternalStatus.UNAVAILABLE, rationale="No TAVILY_API_KEY configured."
    )
    res = finalize(EXTERNAL_RULE, failed_check(), DOC, external)
    assert res.verdict is Verdict.REVISAR
    assert "unavailable" in res.rationale and "No TAVILY_API_KEY configured." in res.rationale


def test_failed_check_without_any_external_result_is_revisar():
    res = finalize(EXTERNAL_RULE, failed_check(), DOC, None)
    assert res.verdict is Verdict.REVISAR and "not performed" in res.rationale


def test_passing_check_needs_no_external_confirmation():
    res = finalize(EXTERNAL_RULE, draft(Verdict.CUMPLE, "Sum insured: 3,000,000"), DOC, None)
    assert res.verdict is Verdict.CUMPLE
