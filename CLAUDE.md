# Sentinel Core

Self-hosted compliance checker: document + vault (YAML rules) in, rule-by-rule verdict
(`cumple` / `no_cumple` / `revisar`) with cited evidence out. See `README.md` for the product,
architecture, and verification status.

## Commands

```bash
uv sync                          # install (Python >=3.11)
uv run pytest                    # tests (no network; FakeLLM + fake search + mocked HTTP)
uv run ruff check . && uv run ruff format --check .
uv run sentinel vaults
uv run sentinel review <file> --vault <vault_id> [--json]
uv run uvicorn sentinel.api:app  # API + web UI on :8000
uv run python scripts/stub_llm.py  # canned OpenAI-compatible server: no-GPU install check only
uv run python evals/run_evals.py # LIVE model + samples vs evals/expected.yaml (needs .env)
docker compose up --build        # app only; the model runs on your dedicated GPU
```

## Invariants (do not break these)

- **Document text never leaves the perimeter.** It goes only to the LLM at `LLM_BASE_URL`.
  The only outbound public request is a Tavily query built from a vault `query_template` with
  vault params / `{year}` placeholders (`verify.py`). Never interpolate document content into it.
  The model that judges search results sees public snippets only, never the document.
- **Never guess.** `policy.py` downgrades any `cumple`/`no_cumple` to `revisar` when its
  evidence quotes can't be found in the document, when evidence is missing, or when an
  externally-verifiable reference wasn't confirmed. It can only downgrade, never upgrade.
  Keep that logic in code, not in prompts.
- **Deterministic where possible.** Numeric/threshold logic is a vault `check` expression
  evaluated in code, not LLM arithmetic. Facts count only if their quote is in the document.
- **Fail closed.** A rule that errors becomes `revisar`; it must not sink or fake a review.
- **No default LLM endpoint.** `LLM_BASE_URL` must be set explicitly.
- The engine is domain-agnostic: domain knowledge lives only in `vaults/*.yaml`.
- The audit log never contains document text, quotes, filenames, or rationales.

## Gotchas

- Nemotron 3 Nano reasoning is a per-request chat-template kwarg
  (`chat_template_kwargs.enable_thinking`): off for extraction, on for judgement. Servers
  without a reasoning parser may emit `<think>` blocks; `llm.extract_json` strips them.
- If a server rejects `response_format`, `OpenAICompatibleLLM` falls back to prompt-only JSON
  (sticky) and still validates locally with pydantic.
- Vault `check` expressions are validated at load time (declared names, allowed functions
  only). `external_check` requires a `check` and only runs when that check fails.
- Sample documents and vaults are synthetic/illustrative; keep real PII/PHI out of the repo.
- Editing `evals/expected.yaml` or a sample: `tests/test_shipped_vaults.py` checks every
  expected quote exists in its sample and every vault rule has an expectation.

## Layout

`src/sentinel/` engine (models, vault, ingest, llm, extract, compare, verify, policy, engine,
audit, api, cli) · `ui/` static UI · `vaults/` · `samples/` · `tests/` · `evals/` · `scripts/` ·
`docs/running-locally.md` · `deploy/README.md` (Nebius; commands from vendor docs, not run on Nebius).
The API/UI have no authentication: keep them on localhost/VPN or behind an authenticating proxy.
