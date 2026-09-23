"""Verdict policy: deterministic guardrails between the model's draft and the final result.

This is what makes the output auditable. Nothing here trusts the model:
  * every cited quote must be found verbatim (modulo whitespace/case) in the document;
  * `cumple` / `no_cumple` need at least one verified quote, otherwise `revisar`;
  * a failed check on an externally-verifiable reference only stands if a public source
    confirmed that reference; contradicted, unclear, or unavailable means `revisar`.

The policy can only ever downgrade a verdict to `revisar`, never upgrade one.
"""

from __future__ import annotations

from .compare import Claim, Draft
from .ingest import Document
from .models import Evidence, ExternalStatus, ExternalVerification, Rule, RuleResult, Verdict

_QUOTE_PREVIEW = 80


def _resolve_claims(doc: Document, claims: list[Claim]) -> tuple[list[Evidence], list[str]]:
    evidence: list[Evidence] = []
    unverified: list[str] = []
    seen: set[tuple[int | None, int | None]] = set()
    for claim in claims:
        ev = doc.evidence_for(claim.quote, fact=claim.fact)
        if ev is None:
            unverified.append(claim.quote)
            continue
        key = (ev.start, ev.end)
        if key not in seen:
            seen.add(key)
            evidence.append(ev)
    evidence.sort(key=lambda e: e.start or 0)
    return evidence, unverified


def _preview(quote: str) -> str:
    quote = " ".join(quote.split())
    return quote if len(quote) <= _QUOTE_PREVIEW else quote[: _QUOTE_PREVIEW - 1] + "…"


def finalize(
    rule: Rule,
    draft: Draft,
    doc: Document,
    external: ExternalVerification | None = None,
) -> RuleResult:
    verdict = draft.verdict
    notes: list[str] = []

    evidence, unverified = _resolve_claims(doc, draft.claims)
    if unverified:
        verdict = Verdict.REVISAR
        shown = "; ".join(f"“{_preview(q)}”" for q in unverified)
        notes.append(f"Cited evidence could not be verified in the document: {shown}.")
    elif verdict in (Verdict.CUMPLE, Verdict.NO_CUMPLE) and not evidence:
        verdict = Verdict.REVISAR
        notes.append("No verifiable evidence from the document supports this verdict.")

    if draft.check_failed and rule.external_check is not None:
        if external is None:
            verdict = Verdict.REVISAR
            notes.append("Confirming the vault reference publicly was required but not performed.")
        elif external.status is ExternalStatus.CONFIRMED:
            notes.append("The vault reference was confirmed by a public source.")
        elif external.status is ExternalStatus.CONTRADICTED:
            verdict = Verdict.REVISAR
            notes.append(
                "A public source contradicts the vault reference, which may be out of date."
            )
        elif external.status is ExternalStatus.UNAVAILABLE:
            verdict = Verdict.REVISAR
            notes.append(
                f"Public verification of the vault reference was unavailable. {external.rationale}"
            )
        else:
            verdict = Verdict.REVISAR
            notes.append(
                "The vault reference could not be confirmed against a clear public source."
            )

    rationale = " ".join([draft.rationale.strip(), *notes]).strip()
    return RuleResult(
        rule_id=rule.id,
        title=rule.title,
        severity=rule.severity,
        verdict=verdict,
        rationale=rationale,
        evidence=evidence,
        facts=draft.facts,
        external=external,
    )
