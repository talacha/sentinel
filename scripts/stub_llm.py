"""A canned OpenAI-compatible server for trying Sentinel without a GPU or an API key.

    uv run python scripts/stub_llm.py [port]        # default port 8799

Then point Sentinel at it:

    LLM_BASE_URL=http://127.0.0.1:8799/v1  LLM_MODEL=stub

This is NOT a model. It returns fixed, hand-written answers for ONE file:
`samples/insurance/life_underwriting_01.txt` with the `insurance_life` vault. It exists to check
your install and the HTTP wiring (client, parsing, engine, API, UI). Any other document or vault
gets `revisar` answers or empty facts. Use a real model for real reviews.
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

FACTS = {
    "sum_insured": (3000000, "Sum insured requested: MXN 3,000,000"),
    "declared_income": (150000, "Annual income: MXN 150,000"),
    "financial_questionnaire_attached": (False, "Financial questionnaire attached: No"),
    "age": (45, "Age: 45"),
}

JUDGEMENTS = {
    "Tobacco declaration is consistent with lab results": (
        "no_cumple",
        "Declared non-smoker, but the cotinine test is positive.",
        ["Tobacco use: No", "Cotinine (urine): POSITIVE"],
    ),
    "Beneficiaries are designated and shares total 100%": (
        "cumple",
        "Two beneficiaries with shares 60% + 40% = 100%.",
        ["Laura Rivera Soto (spouse) - 60%", "Diego Mendoza Rivera (son) - 40%"],
    ),
    "Application is signed and dated by the applicant": (
        "cumple",
        "Signed and dated.",
        ["Applicant signature: Carlos Mendoza Rivera", "Date signed: 2026-08-02"],
    ),
}


def answer(system: str, user: str) -> str:
    """Pick a canned reply from the JSON schema Sentinel embeds in its system prompt."""
    schema = system.replace(" ", "")
    if '"title":"ExtractionOut"' in schema:
        facts = [{"name": n, "found": True, "value": v, "quote": q} for n, (v, q) in FACTS.items()]
        return json.dumps({"facts": facts})
    if '"title":"JudgementOut"' in schema:
        match = re.search(r"Rule: (.*)\n", user)
        verdict, why, quotes = JUDGEMENTS.get(
            match.group(1) if match else "",
            ("revisar", "The stub has no answer for this rule.", []),
        )
        # Real reasoning models may emit a <think> block; Sentinel must cope with it.
        return "<think>weighing the evidence...</think>\n" + json.dumps(
            {"verdict": verdict, "rationale": why, "evidence": quotes}
        )
    # SourceJudgement: the stub never confirms anything from the web.
    return json.dumps({"status": "unclear", "rationale": "No clear source.", "supporting_urls": []})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = answer(body["messages"][0]["content"], body["messages"][1]["content"])
        payload = json.dumps(
            {
                "id": "stub",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "stub"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def make_server(port: int = 8799) -> HTTPServer:
    return HTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print(f"stub LLM on http://127.0.0.1:{port}/v1  (canned answers, not a model)")
    make_server(port).serve_forever()
