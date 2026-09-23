"""Core data models: vault rules, evidence, and review reports."""

from __future__ import annotations

import ast
import re
import string
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

# Functions a rule `check` expression may call. Kept deliberately tiny.
CHECK_FUNCTIONS: frozenset[str] = frozenset({"abs", "min", "max", "round", "len"})

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class Verdict(StrEnum):
    CUMPLE = "cumple"
    NO_CUMPLE = "no_cumple"
    REVISAR = "revisar"


class FactType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"


# --------------------------------------------------------------------------- vault


class FactSpec(BaseModel):
    """A fact the engine must extract from the document for a rule."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: FactType = FactType.TEXT
    description: str
    optional: bool = False

    @model_validator(mode="after")
    def _valid_name(self) -> FactSpec:
        if not self.name.isidentifier():
            raise ValueError(f"fact name {self.name!r} must be a valid identifier")
        return self


class ExternalCheck(BaseModel):
    """Scoped public-source confirmation of a vault reference (one search at most)."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(description="The public fact a source must support.")
    query_template: str = Field(
        description="Search query. Placeholders may only be vault params or {year}."
    )
    allowed_domains: list[str] = Field(default_factory=list)

    def placeholders(self) -> set[str]:
        return {
            field
            for _, field, _, _ in string.Formatter().parse(self.query_template)
            if field is not None
        }


class Rule(BaseModel):
    """One vault rule. Exactly one of `check` (deterministic) or `criterion` (LLM-judged)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str = ""
    severity: Literal["low", "medium", "high"] = "medium"
    facts: list[FactSpec] = Field(default_factory=list)
    references: dict[str, float | int | str] = Field(
        default_factory=dict,
        description="Vault-held constants (e.g. a threshold) usable in `check`.",
    )
    check: str | None = Field(
        default=None, description="Boolean expression over facts and references."
    )
    criterion: str | None = Field(
        default=None, description="Natural-language criterion judged by the model."
    )
    on_fail: Verdict = Verdict.NO_CUMPLE
    external_check: ExternalCheck | None = None

    @model_validator(mode="after")
    def _validate(self) -> Rule:
        if not _ID_RE.match(self.id):
            raise ValueError(f"rule id {self.id!r} must match {_ID_RE.pattern}")
        if (self.check is None) == (self.criterion is None):
            raise ValueError(f"rule {self.id!r}: set exactly one of `check` or `criterion`")
        if self.on_fail == Verdict.CUMPLE:
            raise ValueError(f"rule {self.id!r}: on_fail cannot be `cumple`")

        fact_names = [f.name for f in self.facts]
        if len(fact_names) != len(set(fact_names)):
            raise ValueError(f"rule {self.id!r}: duplicate fact names")
        overlap = set(fact_names) & set(self.references)
        if overlap:
            raise ValueError(f"rule {self.id!r}: names both fact and reference: {sorted(overlap)}")

        if self.check is not None:
            if not self.facts:
                raise ValueError(f"rule {self.id!r}: `check` rules must declare facts")
            self._validate_check(self.check, set(fact_names) | set(self.references))
        elif self.facts:
            raise ValueError(f"rule {self.id!r}: `criterion` rules must not declare facts")
        if self.external_check is not None and self.check is None:
            raise ValueError(
                f"rule {self.id!r}: `external_check` needs a `check` (it verifies the reference)"
            )
        return self

    def _validate_check(self, expr: str, names: set[str]) -> None:
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"rule {self.id!r}: invalid check expression: {exc.msg}") from exc
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        bad_calls = called - CHECK_FUNCTIONS
        if bad_calls:
            raise ValueError(
                f"rule {self.id!r}: function(s) not allowed in check: {sorted(bad_calls)}"
            )
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - called
        unknown = used - names
        if unknown:
            raise ValueError(
                f"rule {self.id!r}: check uses undefined name(s) {sorted(unknown)}; "
                "declare them as facts or references"
            )


class Vault(BaseModel):
    """A company's reference policy: a set of rules plus egress-safe query parameters."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str = ""
    language: str = "en"
    params: dict[str, str] = Field(
        default_factory=dict,
        description="Static values allowed in external query templates (e.g. jurisdiction).",
    )
    rules: list[Rule]

    @model_validator(mode="after")
    def _validate(self) -> Vault:
        if not _ID_RE.match(self.id):
            raise ValueError(f"vault id {self.id!r} must match {_ID_RE.pattern}")
        if not self.rules:
            raise ValueError(f"vault {self.id!r} has no rules")
        ids = [r.id for r in self.rules]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"vault {self.id!r}: duplicate rule ids {dupes}")
        allowed = set(self.params) | {"year"}
        for rule in self.rules:
            if rule.external_check is None:
                continue
            unknown = rule.external_check.placeholders() - allowed
            if unknown:
                raise ValueError(
                    f"rule {rule.id!r}: query_template uses placeholder(s) {sorted(unknown)} "
                    f"that are not vault params; allowed: {sorted(allowed)}"
                )
        return self


# --------------------------------------------------------------------------- results


class Evidence(BaseModel):
    """A verbatim quote from the document, located by page and character offsets."""

    quote: str
    page: int | None = None
    start: int | None = None
    end: int | None = None
    fact: str | None = None


class ExtractedFact(BaseModel):
    name: str
    value: Any | None = None
    found: bool = False
    quote: str | None = None


class Source(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""


class ExternalStatus(StrEnum):
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    UNCLEAR = "unclear"
    UNAVAILABLE = "unavailable"  # no API key, budget exhausted, or search error


class ExternalVerification(BaseModel):
    query: str | None = None
    status: ExternalStatus
    rationale: str = ""
    sources: list[Source] = Field(default_factory=list)


class RuleResult(BaseModel):
    rule_id: str
    title: str
    severity: str = "medium"
    verdict: Verdict
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    external: ExternalVerification | None = None


class DocumentInfo(BaseModel):
    filename: str
    sha256: str
    pages: int
    chars: int


class ReviewReport(BaseModel):
    vault_id: str
    vault_title: str
    document: DocumentInfo
    model: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    results: list[RuleResult]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> dict[str, int]:
        counts = {v.value: 0 for v in Verdict}
        for r in self.results:
            counts[r.verdict.value] += 1
        return counts
