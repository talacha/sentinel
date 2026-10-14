"""Synthetic documents bundled so a visitor can run a real review without supplying a file.

They are served inline by one collection endpoint: no identifier from the caller ever reaches
the filesystem. The texts are illustrative and contain no real personal or health data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Sample:
    id: str
    vault_id: str
    path: str
    label: str
    note: str
    text: str = ""


# Every .txt under the samples directory must appear here (tests/test_samples.py enforces it).
_MANIFEST: tuple[Sample, ...] = (
    Sample(
        id="insurance-issues",
        vault_id="insurance_life",
        path="insurance/life_underwriting_01.txt",
        label="Life application with findings",
        note="A non-smoker declaration contradicted by a positive lab result.",
    ),
    Sample(
        id="insurance-clean",
        vault_id="insurance_life",
        path="insurance/life_underwriting_02_clean.txt",
        label="Life application, nothing to flag",
        note="The same policy and rules, on a file that holds up.",
    ),
    Sample(
        id="legal-msa",
        vault_id="legal_contract",
        path="legal/msa_01.txt",
        label="Vendor services agreement",
        note="A master services agreement with several clauses missing.",
    ),
    Sample(
        id="health-inpatient",
        vault_id="health_patient",
        path="health/patient_file_01.txt",
        label="Inpatient file",
        note="An admission record with a medication charted against a documented allergy.",
    ),
)


def load_samples(directory: Path) -> list[Sample]:
    """Read the bundled samples, skipping any the deployment did not ship (e.g. a slim image)."""
    found = []
    for entry in _MANIFEST:
        file = directory / entry.path
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        found.append(Sample(**{**entry.__dict__, "text": text}))
    return found
