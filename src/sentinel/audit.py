"""Append-only JSONL audit log.

Records what was reviewed (by hash), what was decided, and every query that left the
perimeter. It never contains document text, quotes, filenames, or free-text rationales:
evidence is recorded as page and character offsets only.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .models import ReviewReport


def audit_record(report: ReviewReport) -> dict:
    return {
        "ts": report.created_at.isoformat(),
        "event": "review",
        "vault_id": report.vault_id,
        "document_sha256": report.document.sha256,
        "pages": report.document.pages,
        "chars": report.document.chars,
        "model": report.model,
        "summary": report.summary,
        "rules": [
            {
                "rule_id": r.rule_id,
                "verdict": r.verdict.value,
                "evidence": [{"page": e.page, "start": e.start, "end": e.end} for e in r.evidence],
                "external": (
                    {
                        "query": r.external.query,
                        "status": r.external.status.value,
                        "sources": [s.url for s in r.external.sources],
                    }
                    if r.external
                    else None
                ),
            }
            for r in report.results
        ],
    }


class AuditLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, report: ReviewReport) -> None:
        line = json.dumps(audit_record(report), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
