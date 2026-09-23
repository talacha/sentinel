"""Command line: `sentinel review <file> --vault <id>` and `sentinel vaults`.

Exit codes: 0 every rule `cumple`; 2 at least one `no_cumple` or `revisar`; 1 error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, Settings, get_settings
from .engine import Engine
from .ingest import IngestError, ingest_path
from .models import ReviewReport, Verdict
from .vault import VaultError, VaultRegistry

_LABEL = {
    Verdict.CUMPLE: "CUMPLE    ",
    Verdict.NO_CUMPLE: "NO_CUMPLE ",
    Verdict.REVISAR: "REVISAR   ",
}


def render_report(report: ReviewReport) -> str:
    lines = [
        f"{report.vault_title}  ({report.vault_id})",
        f"document: {report.document.filename}  sha256:{report.document.sha256[:12]}…  "
        f"{report.document.pages} page(s)",
        "summary: " + ", ".join(f"{k}={v}" for k, v in report.summary.items()),
        "",
    ]
    for r in report.results:
        lines.append(f"[{_LABEL[r.verdict]}] {r.title}  ({r.rule_id})")
        lines.append(f"    {r.rationale}")
        for e in r.evidence:
            where = f"p.{e.page}" if e.page else "?"
            lines.append(f"    evidence ({where}): “{' '.join(e.quote.split())}”")
        if r.external:
            lines.append(f"    external: {r.external.status.value}; query: {r.external.query}")
            for s in r.external.sources:
                lines.append(f"      - [{s.role}] {s.url}")
        lines.append("")
    return "\n".join(lines)


def _cmd_review(args: argparse.Namespace, settings: Settings) -> int:
    registry = VaultRegistry(args.vaults_dir or settings.vaults_dir)
    vault = registry.get(args.vault)
    doc = ingest_path(args.file, max_chars=settings.max_document_chars)
    report = Engine.from_settings(settings).review(doc, vault)
    print(report.model_dump_json(indent=2) if args.json else render_report(report))
    return 0 if all(r.verdict is Verdict.CUMPLE for r in report.results) else 2


def _cmd_vaults(args: argparse.Namespace, settings: Settings) -> int:
    for v in VaultRegistry(args.vaults_dir or settings.vaults_dir):
        print(f"{v.id}\t{len(v.rules)} rules\t{v.title}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel", description="Self-hosted compliance checker.")
    parser.add_argument("--vaults-dir", type=Path, help="directory of vault YAML files")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="review a document against a vault")
    review.add_argument("file", type=Path)
    review.add_argument("--vault", required=True, help="vault id (see `sentinel vaults`)")
    review.add_argument("--json", action="store_true", help="print the full JSON report")
    review.set_defaults(func=_cmd_review)

    vaults = sub.add_parser("vaults", help="list available vaults")
    vaults.set_defaults(func=_cmd_vaults)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args, get_settings())
    except (ConfigError, VaultError, IngestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
