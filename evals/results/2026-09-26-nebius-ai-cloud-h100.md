# Live eval run — dedicated Nebius AI Cloud GPU

- Date: 2026-09-26
- Model: `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16`, served with vLLM 0.30.0
- Infra: Nebius AI Cloud, `gpu-h100-sxm` / `1gpu-16vcpu-200gb`, single-tenant VM (`sentinel-gpu`), vLLM bound to `127.0.0.1:8000`, reached via SSH tunnel — not Token Factory, not shared.
- Command: `uv run python evals/run_evals.py`

## Result

```
24/24 verdicts as expected; 1 evidence warning(s)
```

Full per-rule output in the command log (not reproduced here to avoid duplicating `evals/expected.yaml`'s content). One evidence warning: `smoker-consistency` on `life_underwriting_01.txt` — the model's cited quote differs slightly from the expected quote ("Tobacco use: No" not cited verbatim), but the verdict itself (`no_cumple`) matched. Worth a look, not a failure.

This closes the gap flagged in `deploy/README.md` §6 ("Not verified against a self-hosted vLLM on a Nebius GPU") and in `README.md`'s Status section — the engine now has a corroborated run against the actual dedicated-GPU architecture the product claims, not just Token Factory.
