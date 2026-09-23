# Running Sentinel Core locally

This gets you from a fresh clone to a working review. Start with the no-GPU check in section 3,
then pick a real model backend in section 4. To host it for a team, see
[deploying on Nebius](../deploy/README.md).

## 1. Prerequisites

- **Git**
- **Python 3.11 or newer.** Tested on 3.11, 3.12 (the Docker image), and 3.14. If you have
  none, `uv` can download one for you (below).
- **[uv](https://docs.astral.sh/uv/)**, recommended, or plain `pip` (see 2b).
- macOS or Linux. Windows is untested: use WSL or Docker.
- Optional: Docker (for `docker compose`) and a [Tavily](https://tavily.com) API key (for scoped
  verification).

No Node.js, database, or system libraries are needed. The web UI is static files.

## 2. Install dependencies

Run everything from the repository root: `.env`, `vaults/`, and `audit/` are resolved relative to
the current directory.

### 2a. With uv (recommended)

Install uv if you do not have it (`uv --version` to check):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # macOS / Linux, official installer
brew install uv                                     # or, on macOS with Homebrew
pip install --user uv                               # or, with any existing Python
```

Then:

```bash
git clone https://github.com/talacha/sentinel.git
cd sentinel
git checkout worktree-sentinel-core-build    # only until the feature branch is merged
uv sync
```

`uv sync` creates `.venv/` in the repository, installs the exact dependency versions pinned in
`uv.lock` (the ones the tests ran against), and installs the `sentinel` command in editable mode.
It also installs the dev tools (pytest, ruff, fpdf2); add `--no-dev` for a runtime-only install.
If your Python is older than 3.11, run `uv python install 3.12` first and uv will use it.

Run commands with `uv run <command>` (no activation needed), or activate the environment with
`source .venv/bin/activate` and drop the `uv run` prefix.

### 2b. With plain pip (no uv)

```bash
git clone https://github.com/talacha/sentinel.git
cd sentinel
git checkout worktree-sentinel-core-build    # only until the feature branch is merged
python3 -m venv .venv
source .venv/bin/activate
pip install -e .                  # the app and its runtime dependencies
pip install pytest ruff fpdf2     # optional: what you need to run the tests and lint
```

Then run commands without the `uv run` prefix, for example `sentinel vaults`,
`uvicorn sentinel.api:app`, and `python -m pytest`. pip picks the latest compatible dependency
versions instead of the pinned `uv.lock`; the suite passes that way on Python 3.14, but if
something breaks, use uv or Docker.

### 2c. With Docker (no local Python)

`docker compose up --build` builds and runs the app without installing anything else; see
[section 6](#6-run-it-in-docker). You still need a model endpoint.

### Check the install

```bash
uv run pytest             # ~218 tests, no network, a few seconds (pip: python -m pytest)
uv run sentinel vaults    # (pip: sentinel vaults)
```

`sentinel vaults` should list `health_patient`, `insurance_life`, and `legal_contract`. The rest
of this guide writes `uv run <command>`; if you used pip, drop the `uv run` prefix.

To update later: `git pull && uv sync`.

## 3. Try it without a GPU (2 minutes)

`scripts/stub_llm.py` is a canned OpenAI-compatible server. It is **not a model**: it has fixed
answers for one file (`samples/insurance/life_underwriting_01.txt` with the `insurance_life`
vault). It checks your install and the whole request path (HTTP client, parsing, engine, API,
UI) without a GPU or an API key.

Terminal 1:

```bash
uv run python scripts/stub_llm.py          # listens on 127.0.0.1:8799
```

Terminal 2:

```bash
export LLM_BASE_URL=http://127.0.0.1:8799/v1 LLM_MODEL=stub
uv run sentinel review samples/insurance/life_underwriting_01.txt --vault insurance_life
```

You should see `summary: cumple=3, no_cumple=1, revisar=2`: the smoker declaration contradicted
by the cotinine result (`no_cumple`), the income multiple and the threshold (`revisar`). The
threshold rule reports `external: unavailable` because no Tavily key is set; the verdict is
`revisar` either way.

For the web UI, start the app in terminal 2 and open <http://localhost:8000>:

```bash
uv run uvicorn sentinel.api:app
```

Choose "Life insurance underwriting", upload `samples/insurance/life_underwriting_01.txt`, and
run the review. Any other document or vault gets `revisar` from the stub, which is the stub's
limit, not Sentinel's.

## 4. Use a real model

Sentinel needs an OpenAI-compatible chat endpoint. There is no default, on purpose.

| Backend | Use it for | `LLM_BASE_URL` |
| --- | --- | --- |
| **Your dedicated vLLM on Nebius**, reached through an SSH tunnel | Real documents, from your laptop | `http://127.0.0.1:8000/v1` |
| **Nebius Token Factory** (shared) | Development with **synthetic documents only** | `https://api.tokenfactory.nebius.com/v1/` |
| **Local vLLM** on your own NVIDIA GPU | Real documents, fully offline | `http://127.0.0.1:8000/v1` |
| Anything else OpenAI-compatible | Not tested | your URL |

### 4a. Your Nebius GPU through an SSH tunnel

Set up the GPU first ([deploy/README.md](../deploy/README.md), path A). Then forward its private
vLLM port to your machine so it never needs to be exposed:

```bash
ssh -N -L 8000:127.0.0.1:8000 user@<gpu-vm-ip>
```

```bash
# .env
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=nemotron-3-nano                # the --served-model-name you used
LLM_API_KEY=<the VLLM_API_KEY you set>
```

### 4b. Nebius Token Factory (development only)

Token Factory's shared endpoints are multi-tenant. Use them only with synthetic documents such as
the ones in `samples/`, never with client data.

```bash
# .env
LLM_BASE_URL=https://api.tokenfactory.nebius.com/v1/
LLM_API_KEY=<your Token Factory key>
LLM_MODEL=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B
```

The model id must match the catalog exactly, capitalization included. The short form
`nvidia/nemotron-3-nano`, which other providers use, is **not** in Token Factory's catalog and
fails with `404 The model ... does not exist`. This id was verified against a live catalog on
2026-09-23, but catalogs change, so list yours:

```bash
curl -s https://api.tokenfactory.nebius.com/v1/models \
  -H "Authorization: Bearer $LLM_API_KEY" | jq -r '.data[].id' | grep -i nemotron
```

Then run `uv run sentinel preflight`. It checks that the key works, that the model id is served,
and that a real request returns valid JSON, before you find out in the middle of a review.

If the server rejects `response_format`, Sentinel logs a warning and falls back to prompt-only
JSON. Every reply is still validated locally. Set `LLM_STRUCTURED_MODE=prompt` to skip the
attempt.

### 4c. Local vLLM

Needs a Linux machine with an 80 GB-class NVIDIA GPU (the model card lists H100-80GB and A100).
Follow the serve command in [deploy/README.md](../deploy/README.md#2-serve-nemotron-with-vllm),
then use `LLM_BASE_URL=http://127.0.0.1:8000/v1`. A Mac laptop is not a practical host for this
model; use 4a.

### Optional: Tavily verification

```bash
# .env
TAVILY_API_KEY=tvly-...
```

Without a key, rules that need a public source resolve to `revisar` and say why. With one, the
only thing that leaves your machine is the templated query shown in the report and audit log.

## 5. Review documents

```bash
cp .env.example .env         # then edit it (see section 4)

# CLI
uv run sentinel review path/to/file.pdf --vault insurance_life
uv run sentinel review path/to/file.pdf --vault legal_contract --json > report.json

# Web UI + API
uv run uvicorn sentinel.api:app          # http://localhost:8000
curl -F vault_id=insurance_life -F file=@path/to/file.pdf http://localhost:8000/v1/reviews
```

CLI exit codes: `0` every rule `cumple`, `2` at least one `no_cumple` or `revisar`, `1` error.
`http://localhost:8000/healthz` shows which LLM host document text will be sent to and whether
verification is configured.

Try the other bundled samples: `samples/legal/msa_01.txt` with `legal_contract`, and
`samples/health/patient_file_01.txt` with `health_patient`. To compare live verdicts against the
expected ones:

```bash
uv run python evals/run_evals.py             # all cases; needs a real model, not the stub
uv run python evals/run_evals.py --case msa  # only cases whose sample path contains "msa"
```

Where things are written: the audit log (hashes, verdicts, offsets, outbound queries; never
document text) goes to `audit/audit.jsonl`.

## 6. Run it in Docker

```bash
cp .env.example .env         # LLM_BASE_URL must be reachable from inside the container
docker compose up --build
```

Open <http://localhost:8000>. Inside the container, `127.0.0.1` is the container itself: on Docker
Desktop reach a host service (the stub, or an SSH tunnel) with `http://host.docker.internal:<port>/v1`.
On Linux, make the audit directory writable by the container user (uid 10001):

```bash
mkdir -p audit && sudo chown 10001 audit
```

Vaults are mounted read-only from `./vaults`; restart the container after editing one.

## 7. Add your own vault or rules

Copy a file in `vaults/`, change the `id`, and edit the rules (format in the main
[reference](reference.md#vaults)). Vaults are validated when they load, so a mistake fails with the
file and field name:

```bash
uv run sentinel vaults
```

The server reads vaults once, on first use: restart it after editing a vault.

Write rules so that a `cumple` or `no_cumple` can point at a passage in the document, because a
verdict without a verifiable quote becomes `revisar`. Keep real client data out of the repository.

## 8. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `503 Missing required setting(s): LLM_BASE_URL, LLM_MODEL` | No endpoint configured. Set both in `.env` or the environment. |
| Every rule is `revisar` with "the endpoint has no model named ..." (a 404) | The model id in `LLM_MODEL` is not in the endpoint's catalog. Ids are case-sensitive: on Token Factory use `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, not `nvidia/nemotron-3-nano`. Run `uv run sentinel preflight` (or open `/status` and run the live checks) for the list of real ids. Restart after editing `.env`; the admin console can change it without a restart. |
| Every rule is `revisar` with "Automated review of this rule failed" | The model call failed. The rationale has the error; check `LLM_BASE_URL`, the key, and that the server is up (`curl $LLM_BASE_URL/models`). |
| "LLM returned an empty reply (output truncated during reasoning; raise LLM_MAX_TOKENS)" | Reasoning consumed the token budget. Raise `LLM_MAX_TOKENS`. |
| Warning: "server rejected structured output mode" | The endpoint does not support `response_format`. Sentinel switched to prompt-only JSON; nothing to do. |
| Many `revisar` with "quote was not found in the document" | The model paraphrased instead of quoting. Expected occasionally; a weaker model does it more. |
| "no extractable text found" | Scanned or image-only PDF. There is no OCR; export text or use a text file. |
| Threshold rule says `external: unavailable` | No `TAVILY_API_KEY`, or the per-review search budget is used up. The reason is in the rationale. |
| `vault ... invalid` on startup or `sentinel vaults` | The message names the file and field; fix the YAML. |
| Docker: cannot write `audit/audit.jsonl` | Linux permissions on the bind mount; see section 6. |
