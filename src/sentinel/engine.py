"""The review pipeline: ingest -> compare (per rule) -> verify (if needed) -> decide."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .audit import AuditLog
from .compare import evaluate_rule
from .config import Settings
from .ingest import Document
from .llm import LLM, OpenAICompatibleLLM
from .models import ReviewReport, Rule, RuleResult, Vault, Verdict
from .policy import finalize
from .verify import SearchBudget, SearchClient, TavilySearch, verify_reference

log = logging.getLogger(__name__)


class Engine:
    """Domain-agnostic: all domain knowledge comes from the vault."""

    def __init__(
        self,
        llm: LLM,
        search: SearchClient | None = None,
        audit: AuditLog | None = None,
        *,
        max_searches: int = 5,
        max_workers: int = 4,
    ):
        self.llm = llm
        self.search = search
        self.audit = audit
        self.max_searches = max_searches
        self.max_workers = max_workers

    @classmethod
    def from_settings(cls, settings: Settings) -> Engine:
        """Wire the real model client, optional Tavily search, and audit log from config."""
        search = TavilySearch(settings.tavily_api_key) if settings.tavily_api_key else None
        log.info("LLM endpoint host: %s", settings.llm_host)
        return cls(
            OpenAICompatibleLLM.from_settings(settings),
            search,
            AuditLog(settings.audit_log_path),
            max_searches=settings.max_searches_per_review,
            max_workers=settings.max_workers,
        )

    def review(self, doc: Document, vault: Vault) -> ReviewReport:
        budget = SearchBudget(self.max_searches)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            results = list(
                pool.map(lambda rule: self._review_rule(doc, vault, rule, budget), vault.rules)
            )
        report = ReviewReport(
            vault_id=vault.id,
            vault_title=vault.title,
            document=doc.info(),
            model=self.llm.model,
            results=results,
        )
        if self.audit:
            self.audit.record(report)
        return report

    def _review_rule(
        self, doc: Document, vault: Vault, rule: Rule, budget: SearchBudget
    ) -> RuleResult:
        try:
            draft = evaluate_rule(self.llm, doc, rule)
            external = None
            if draft.check_failed and rule.external_check is not None:
                external = verify_reference(
                    llm=self.llm,
                    search=self.search,
                    budget=budget,
                    vault=vault,
                    rule=rule,
                    doc=doc,
                )
            return finalize(rule, draft, doc, external)
        except Exception as exc:  # fail closed: one broken rule must not sink or fake a review
            log.exception("rule %s failed", rule.id)
            return RuleResult(
                rule_id=rule.id,
                title=rule.title,
                severity=rule.severity,
                verdict=Verdict.REVISAR,
                rationale=f"Automated review of this rule failed ({type(exc).__name__}: {exc}); "
                "a human should review it.",
            )
