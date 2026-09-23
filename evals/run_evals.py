"""Run the sample documents against a LIVE model and compare with evals/expected.yaml.

    uv run python evals/run_evals.py [--case msa_01] [--env-file PATH]

Needs LLM_BASE_URL / LLM_MODEL (and optionally TAVILY_API_KEY) in the environment or .env.
Exit code is 1 if any verdict is outside the accepted set; missing expected quotes are warnings
(the model may cite a different, equally valid passage).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from sentinel.config import ConfigError, Settings, get_settings
from sentinel.engine import Engine
from sentinel.ingest import ingest_path
from sentinel.vault import VaultRegistry

ROOT = Path(__file__).resolve().parents[1]


def accepted(expect: dict) -> list[str]:
    verdict = expect["verdict"]
    return verdict if isinstance(verdict, list) else [verdict]


def norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", help="only run cases whose sample path contains this text")
    parser.add_argument(
        "--env-file", type=Path, help="read settings from this file (default ./.env)"
    )
    args = parser.parse_args()

    cases = yaml.safe_load((ROOT / "evals" / "expected.yaml").read_text())["cases"]
    if args.case:
        cases = [c for c in cases if args.case in c["sample"]]
    if not cases:
        print("no matching cases", file=sys.stderr)
        return 1

    settings = Settings(_env_file=args.env_file) if args.env_file else get_settings()
    try:
        engine = Engine.from_settings(settings)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    registry = VaultRegistry(ROOT / "vaults")

    failures = warnings = total = 0
    for case in cases:
        doc = ingest_path(ROOT / case["sample"], max_chars=settings.max_document_chars)
        report = engine.review(doc, registry.get(case["vault"]))
        print(f"\n{case['sample']}  ->  {case['vault']}")
        results = {r.rule_id: r for r in report.results}
        for rule_id, expect in case["expect"].items():
            total += 1
            result = results[rule_id]
            ok = result.verdict.value in accepted(expect)
            failures += not ok
            print(
                f"  {'PASS' if ok else 'FAIL'}  {rule_id:28} got {result.verdict.value:10} "
                f"expected {'/'.join(accepted(expect))}"
            )
            cited = [norm(e.quote) for e in result.evidence]
            for quote in expect.get("quotes", []):
                if not any(norm(quote) in c for c in cited):
                    warnings += 1
                    print(f"        warn: expected evidence not cited: “{quote}”")
            if not ok:
                print(f"        rationale: {result.rationale}")

    print(f"\n{total - failures}/{total} verdicts as expected; {warnings} evidence warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
