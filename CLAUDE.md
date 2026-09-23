# Sentinel Core

Self-hosted compliance checker: document + vault (YAML rules) in, rule-by-rule verdict
(`cumple` / `no_cumple` / `revisar`) with cited evidence out. See `README.md` for the product
and architecture.

## Commands

```bash
uv sync                          # install (Python >=3.11)
uv run pytest                    # tests (no network; FakeLLM + mocked Tavily)
uv run ruff check . && uv run ruff format --check .
uv run sentinel review <file> --vault <vault_id>
uv run uvicorn sentinel.api:app  # API + web UI on :8000
```

## Invariants (do not break these)

- **Document text never leaves the perimeter.** It goes only to the LLM at `LLM_BASE_URL`.
  The only outbound public request is a Tavily query built from a vault `query_template` with
  vault params / `{year}` placeholders (`verify.py`). Never interpolate document content into it.
- **Never guess.** `policy.py` downgrades any `cumple`/`no_cumple` to `revisar` when its
  evidence quotes can't be found verbatim in the document, when facts are missing, or when the
  model output is malformed. Keep that logic in code, not in prompts.
- **Deterministic where possible.** Numeric/threshold logic is a vault `check` expression
  evaluated in code, not LLM arithmetic.
- **No default LLM endpoint.** `LLM_BASE_URL` must be set explicitly.
- The engine is domain-agnostic: domain knowledge lives only in `vaults/*.yaml`.
- The audit log never contains document text.

## Layout

`src/sentinel/` engine (models, vault, ingest, llm, extract, compare, verify, policy, engine,
audit, api, cli) · `ui/` static UI · `vaults/` · `samples/` synthetic docs (no real PII/PHI) ·
`tests/` · `evals/` · `deploy/`.
