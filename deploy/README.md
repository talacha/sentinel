# Deploying Sentinel Core on Nebius

Sentinel has two parts: the **app** (this repo: API, web UI, engine) and a **model server** that
speaks the OpenAI chat API. The data-isolation promise depends on the model server being a
dedicated, single-tenant deployment you control. This guide sets that up on Nebius and connects
the app to it.

> **What is and isn't verified.** The Nebius, vLLM, and Hugging Face commands below were checked
> against their official documentation on 2026-09-23. **None of it has been run on Nebius by the
> author of this repo**: no GPU has been provisioned and no live Nemotron or Tavily call has been
> made. Things I could not confirm are marked **(unverified)**. Read [Section 6](#6-verified-vs-not)
> before relying on this for real data.

## 1. Choose a path

| | A. GPU VM (recommended) | B. Serverless AI endpoint | C. Token Factory dedicated endpoint |
| --- | --- | --- | --- |
| What you manage | A VM: OS, vLLM, the app | A container spec | Nothing but the endpoint |
| Isolation | Your VM; vLLM bound to localhost | Your endpoint, private option | "Isolated deployment" per Nebius docs |
| Model | Exactly `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` | Same, in a custom image | Only if Nebius offers a template (check) |
| Effort | Highest, most control | Medium | Lowest |
| Status in this guide | Fully spelled out | Adapted from a docs example | API flow from docs |

**Recommended topology (path A):** run vLLM and Sentinel on the same GPU VM. vLLM listens on
`127.0.0.1:8000` and Sentinel on `127.0.0.1:8080`, so neither is reachable from the network. You
reach the UI from your laptop through an SSH tunnel. Document text then never crosses a network
you did not set up yourself.

You will need: a Nebius account and project with quota for an H100-class GPU, an SSH key pair,
and optionally a [Tavily](https://tavily.com) key. Event credits (Token Factory and AI Cloud
credits from the Builders & Brews page) may cover a few hours; check current pricing at
<https://nebius.com/prices>.

## 2. Serve Nemotron with vLLM

### Path A: on a Nebius GPU VM

#### A.1 Install and authenticate the Nebius CLI

```bash
curl -sSL https://storage.eu-north1.nebius.cloud/cli/install.sh | bash
exec -l $SHELL
nebius version
nebius profile create        # interactive: accept the defaults, log in in the browser, pick your project
nebius profile list
```

Install `jq` too (`brew install jq` or `sudo apt-get install jq`); the commands below use it.

#### A.2 Create the VM

The Nebius compute quickstart's 1-GPU inference VM is the template. The model card lists the
H100-80GB and A100 as supported; the BF16 weights are about 60 GB, so use an 80 GB GPU. The boot
disk is 200 GiB here (the docs' example uses 50) to hold the weights and cache.

```bash
export SUBNET_ID=$(nebius vpc subnet list --format jsonpath='{.items[0].metadata.id}')

cat > cloud-init.yaml <<'EOF'
#cloud-config
users:
  - name: user
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - ssh-ed25519 AAAA...your-public-key...
EOF

export VM_ID=$(nebius compute instance create \
  --name sentinel-gpu \
  --resources-platform gpu-h100-sxm \
  --resources-preset 1gpu-16vcpu-200gb \
  --boot-disk-managed-disk-name sentinel-gpu-disk \
  --boot-disk-managed-disk-type network_ssd \
  --boot-disk-managed-disk-size-gibibytes 200 \
  --boot-disk-managed-disk-block-size-bytes 4096 \
  --boot-disk-managed-disk-source-image-family-image-family ubuntu24.04-cuda13.0 \
  --boot-disk-attach-mode READ_WRITE \
  --cloud-init-user-data "$(cat cloud-init.yaml)" \
  --network-interfaces "[{\"name\": \"eth0\", \"subnet_id\": \"$SUBNET_ID\", \"ip_address\": {}, \"public_ip_address\": {}}]" \
  --format jsonpath='{.metadata.id}')

export VM_IP=$(nebius compute instance get --id $VM_ID --format json \
  | jq -r '.status.network_interfaces[0].public_ip_address.address | split("/")[0]')
echo "$VM_IP"
```

The platform, preset, and image names come from Nebius's compute quickstart; the docs did not show
how to list them, so if a name is rejected check the Nebius console for what your project offers.
The public IP is only for SSH. Restrict who can reach port 22 using Nebius's network settings
(not covered here). vLLM and Sentinel bind to localhost, so they are not exposed by it.

#### A.3 Connect and check the GPU

```bash
ssh user@$VM_IP
nvidia-smi                   # should show one H100 (80 GB)
```

#### A.4 Install and start vLLM

The model card requires `vllm>=0.12.0`, `--trust-remote-code`, and NVIDIA's reasoning-parser
plugin. The plugin is a single Python file from the model repo.

```bash
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip
python3 -m venv ~/vllm-env && source ~/vllm-env/bin/activate
pip install -U 'vllm>=0.12.0'

wget -O ~/nano_v3_reasoning_parser.py \
  https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16/resolve/main/nano_v3_reasoning_parser.py

# The key Sentinel will use as LLM_API_KEY. Save it somewhere safe.
export VLLM_API_KEY=$(openssl rand -hex 32); echo "$VLLM_API_KEY"

vllm serve nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
  --served-model-name nemotron-3-nano \
  --host 127.0.0.1 --port 8000 \
  --api-key "$VLLM_API_KEY" \
  --trust-remote-code \
  --max-num-seqs 8 \
  --tensor-parallel-size 1 \
  --max-model-len 262144 \
  --reasoning-parser-plugin ~/nano_v3_reasoning_parser.py \
  --reasoning-parser nano_v3
```

The first start downloads about 60 GB and loads it onto the GPU; expect several minutes. From a
second SSH session:

```bash
curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $VLLM_API_KEY"
```

Notes:

- These flags are the ones on NVIDIA's model card and in vLLM's Nemotron 3 Nano recipe, minus the
  tool-calling flags, which Sentinel does not use. The recipe pins the container image
  `vllm/vllm-openai:v0.28.0`; check the current recipe at
  <https://recipes.vllm.ai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16> for updates.
- A search result suggested newer vLLM versions ship a built-in `nemotron_v3` reasoning parser that
  makes the plugin unnecessary. **(unverified)**: the model card and recipe still use the plugin and
  vLLM's published parser list shows no Nemotron entry. Check `vllm serve --help` on your version.
- `--max-model-len 262144` is the card's default. Sentinel's default cap (300,000 characters) fits
  well inside it; lower it if you hit GPU memory errors.
- Sentinel toggles reasoning per request with `chat_template_kwargs.enable_thinking` (off for
  extraction, on for judgement). NVIDIA suggests `temperature=1.0, top_p=1.0` with reasoning on,
  which Sentinel does by default (`LLM_TEMPERATURE_REASONING`, `LLM_MAX_TOKENS`).
- If the download is gated, log in with `huggingface-cli login` first. **(unverified)** whether it is.

Run it as a service so it survives logout and reboot. Put `VLLM_API_KEY=<key>` in
`/etc/vllm.env` (`sudo chmod 600 /etc/vllm.env`), then create `/etc/systemd/system/vllm.service`:

```ini
[Unit]
Description=vLLM (Nemotron 3 Nano)
After=network-online.target

[Service]
User=user
EnvironmentFile=/etc/vllm.env
ExecStart=/home/user/vllm-env/bin/vllm serve nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
  --served-model-name nemotron-3-nano --host 127.0.0.1 --port 8000 --api-key ${VLLM_API_KEY} \
  --trust-remote-code --max-num-seqs 8 --tensor-parallel-size 1 --max-model-len 262144 \
  --reasoning-parser-plugin /home/user/nano_v3_reasoning_parser.py --reasoning-parser nano_v3
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now vllm
journalctl -u vllm -f          # watch the model load
```

Continue at [Section 3](#3-run-sentinel-against-it).

### Path B: Serverless AI endpoint

Nebius Serverless AI runs a container on a GPU and gives it an address and a token. vLLM's docs
show the pattern with a small model; this adapts it. **Adapted, not run. (unverified)**

The plugin file must exist inside the container, so build a small image on top of vLLM's and push
it to a registry your endpoint can pull from:

```dockerfile
FROM vllm/vllm-openai:v0.28.0
ADD https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16/resolve/main/nano_v3_reasoning_parser.py /opt/nano_v3_reasoning_parser.py
```

Then create the endpoint. The documented example needs a project with Serverless Endpoints
permission and quota, and a subnet in `eu-north1` with outbound access to Docker Hub and Hugging
Face:

```bash
export PROJECT_ID=<project-id>  SUBNET_ID=<subnet-id>
export AUTH_TOKEN=$(openssl rand -hex 32)      # this is Sentinel's LLM_API_KEY

nebius ai endpoint create \
  --parent-id "$PROJECT_ID" --subnet-id "$SUBNET_ID" \
  --name sentinel-nemotron \
  --image <your-registry>/vllm-nemotron:latest \
  --container-command vllm \
  --args "serve nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 --served-model-name nemotron-3-nano --host 0.0.0.0 --port 8000 --trust-remote-code --max-model-len 65536 --reasoning-parser-plugin /opt/nano_v3_reasoning_parser.py --reasoning-parser nano_v3" \
  --platform gpu-h100-sxm --preset 1gpu-16vcpu-200gb \
  --container-port 8000/http \
  --auth token --token "$AUTH_TOKEN" \
  --disk-size 250Gi --shm-size 16Gi \
  --public=false --preemptible=false
```

Confirm which platforms and presets Serverless AI offers in your region; the docs example uses an
L40S for a small model. With `--public=false` the endpoint has no public URL; find its address in
`nebius ai endpoint get "$ENDPOINT_ID" --format json` (the docs read HTTPS URLs from
`.status.public_endpoints[]` for public endpoints; the private shape is **unverified**). Sentinel then
needs to run in the same network to reach it. Delete it when finished: `nebius ai endpoint delete "$ENDPOINT_ID"`.

Sentinel's settings for this path: `LLM_BASE_URL=<endpoint address>/v1`,
`LLM_MODEL=nemotron-3-nano`, `LLM_API_KEY=$AUTH_TOKEN`.

### Path C: Token Factory dedicated endpoint

Token Factory's dedicated endpoints are described by Nebius as isolated deployments with an
OpenAI-compatible API, billed per GPU-hour. Whether **Nemotron 3 Nano 30B-A3B is offered as a
template is not documented where I looked**, so start by listing templates. Endpoint paths below
are from Nebius's "Deploy via API" page.

```bash
export TF_KEY=<your Token Factory API key>

# 1. What can I deploy? Use the values from this response exactly.
curl -s https://api.tokenfactory.nebius.com/v0/dedicated_endpoints/templates \
  -H "Authorization: Bearer $TF_KEY" | jq

# 2. Create it (fill in a Nemotron 3 Nano template's model_name / flavor_name / gpu_type / region).
curl -s -X POST https://api.tokenfactory.nebius.com/v0/dedicated_endpoints \
  -H "Authorization: Bearer $TF_KEY" -H 'Content-Type: application/json' \
  -d '{
    "name": "sentinel-nemotron",
    "description": "Sentinel Core inference",
    "model_name": "<from templates>",
    "flavor_name": "<from templates>",
    "gpu_type": "<from templates>",
    "gpu_count": 1,
    "region": "<from templates>",
    "scaling": {"min_replicas": 1, "max_replicas": 1}
  }' | jq
```

The response includes a `routing_key`. Initial deployment takes several minutes and inference
returns errors (often `404`) until it is routable. Then:

```bash
# .env for Sentinel
LLM_BASE_URL=https://api.tokenfactory.<region>.nebius.com/v1
LLM_MODEL=<routing_key>
LLM_API_KEY=$TF_KEY
```

If Nemotron 3 Nano is not in the templates, use path A, or ask Nebius whether they can provide it.
Sentinel cannot verify a provider's data-handling terms: read Nebius's terms on retention and
logging for dedicated endpoints before sending real documents. If the endpoint rejects
`response_format`, Sentinel falls back to prompt-only JSON automatically.

## 3. Run Sentinel against it

**Path A (same VM).** On the GPU VM:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && exec -l $SHELL
git clone https://github.com/talacha/sentinel.git && cd sentinel
git checkout worktree-sentinel-core-build     # until it is merged to main
uv sync --no-dev

cp .env.example .env && chmod 600 .env
```

Edit `.env`:

```bash
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=nemotron-3-nano
LLM_API_KEY=<the VLLM_API_KEY>
TAVILY_API_KEY=<optional>
```

```bash
uv run sentinel review samples/insurance/life_underwriting_01.txt --vault insurance_life
uv run uvicorn sentinel.api:app --host 127.0.0.1 --port 8080
```

Port 8080 because vLLM already uses 8000. From your laptop, tunnel and open the UI:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@$VM_IP
# then browse http://localhost:8080
```

For a persistent service, use a systemd unit like vLLM's above with
`WorkingDirectory=/home/user/sentinel` and
`ExecStart=/home/user/.local/bin/uv run uvicorn sentinel.api:app --host 127.0.0.1 --port 8080`
(check the path with `which uv`).

**Paths B and C.** Run the app anywhere that can reach the endpoint (a small CPU VM in the same
network for B; any host for C), with `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY` as given in
that path. `docker compose up --build` works for this; see
[Running locally](../docs/running-locally.md#6-run-it-in-docker).

**Check it.** `curl http://127.0.0.1:8080/healthz` should show `llm_configured: true` and the
`llm_host` you expect. Then run the live evals; this is the first real test of the model:

```bash
uv run python evals/run_evals.py
```

It compares live verdicts with `evals/expected.yaml` and exits non-zero on a mismatch. Warnings
about "expected evidence not cited" mean the model quoted a different passage, which may be fine.

## 4. Security checklist

- **The login is off by default.** Without `ACCESS_PASSWORD`, anyone who can reach the port can
  upload documents and read reports: keep it on `127.0.0.1` behind an SSH tunnel or a VPN. To
  expose it publicly, set `ACCESS_PASSWORD` (12+ characters), `MAX_REVIEWS_PER_HOUR`, and serve it
  over HTTPS: `deploy/public` does this with Caddy (see the
  [hackathon runbook](../docs/hackathon.md#going-live-on-the-nebius-stack)). Run
  `sentinel preflight --public` first.
- **The admin console can redirect your documents.** `/admin` (only enabled by `ADMIN_PASSWORD`,
  12+ characters, different from `ACCESS_PASSWORD`) can change the model endpoint (`/admin/config`), add
  admins (`/admin/users`), and edit the policy reviews are judged against (`/admin/vaults`). Treat it like a root login and
  check `/status` after any change. Vault edits are stored in the `sentinel-data` volume, so keep
  that volume across redeploys.
- Keep the model server unreachable from outside: vLLM on `127.0.0.1` (path A), always with an
  API key, private endpoint for path B.
- Restrict SSH to known addresses; use key-only login.
- Protect `.env` (`chmod 600`) and `/etc/vllm.env`; rotate keys if exposed.
- Reports contain verbatim quotes from documents. Treat the JSON downloads and API responses as
  sensitive as the documents themselves.
- The audit log (`audit/audit.jsonl`) holds document hashes, verdicts, evidence offsets, and the
  Tavily queries; never document text. Decide on its retention.
- Sentinel processes uploads in memory, but the web framework may spool uploads larger than 1 MB
  to a temporary file for the duration of the request. Use disk encryption if that matters to you.
- To disable all external calls, leave `TAVILY_API_KEY` unset. Rules that need a public source
  then resolve to `revisar`.
- Sentinel makes review *auditable*; it does not make you compliant. Contractual questions (BAAs,
  retention, regions) are between you and your provider.

## 5. Operating and costing

- GPU VMs bill while they exist. When you are done, delete the instance and its disk in the
  console, or with `nebius compute instance delete --id $VM_ID` (check `nebius compute instance
  --help` for your CLI version). Path B: `nebius ai endpoint delete`.
- Logs: `journalctl -u vllm -f`, and the app's output. `/healthz` reports configuration status.
- Update the app: `git pull && uv sync --no-dev`, then restart it. The server reads vaults once,
  so restart after editing one.
- Weights and the vLLM environment live on the boot disk; snapshot it to avoid re-downloading 60 GB.

## 6. Verified vs not

| Item | Status |
| --- | --- |
| Nebius CLI install and profile commands | From Nebius docs, not run |
| `nebius compute instance create` flags, platform `gpu-h100-sxm`, preset `1gpu-16vcpu-200gb`, image family `ubuntu24.04-cuda13.0` | From Nebius's compute quickstart, not run; I did not find how to list valid values |
| vLLM flags, `nano_v3` plugin, `vllm>=0.12.0` | From the NVIDIA model card and vLLM recipe, not run on a GPU; the parser plugin URL resolves |
| Whether the 60 GB model plus `--max-model-len 262144` fits one H100 with `--max-num-seqs 8` | The vLLM recipe uses these values for H100/H200; not run |
| Serverless AI `nebius ai endpoint create` for Nemotron | Adapted from vLLM's Nebius docs (which serve a small Qwen model); not run |
| Token Factory dedicated endpoints: API paths | From Nebius docs; whether Nemotron 3 Nano is a template is unknown |
| Sentinel against a real model, real Tavily, and `evals/run_evals.py` | **Verified 2026-09-23** against Nemotron 3 Nano through Nebius Token Factory (24 of 24 verdicts as expected). **Not** verified against a self-hosted vLLM on a Nebius GPU: run the evals there after deploying |
| Sentinel app, API, UI, Docker image | Tested locally against a stub model (see the main README) |

## 7. Demo checklist

**Where to demo.** Run the demo on a Nebius GPU VM (path A) and present from your laptop through
an SSH tunnel to `http://localhost:8080`. It is the product's own claim, a dedicated single-tenant
Nebius GPU, so it is both the honest demo and the on-theme one. Nothing is exposed publicly, which
matters because Sentinel has no login. Cost: the pricing page listed an H100 at $3.85 per
GPU-hour on-demand on 2026-09-23 (preemptible from $0.79, but those can be reclaimed, so not for
a demo). That is roughly 25 GPU-hours per $100 of credit, before disk and other charges; check
[nebius.com/prices](https://nebius.com/prices) and whether your event credits apply.

**Order of work.** Nothing has run against a real model yet, so find problems on the cheap path
first:

1. **Today, no GPU:** point `.env` at Nebius Token Factory (see
   [Running locally](../docs/running-locally.md#4b-nebius-token-factory-development-only)) with
   synthetic samples and run `uv run python evals/run_evals.py`. Fix any prompt, vault, or
   expectation mismatches here, where a mistake costs cents.
2. **A day ahead:** check that your project has H100 quota (a request may take time), then
   provision the VM ([A.2](#a2-create-the-vm)) and start vLLM. The first start downloads about
   60 GB, so give it time; snapshot the disk once it works.
3. **Run the evals on the VM's model** and rehearse the flow with all three samples.
4. **Demo day:** start the VM well before you present, open the SSH tunnel, and check `/healthz`
   shows the expected LLM host. Open the UI in a real browser once beforehand: it has only been
   tested in a simulated DOM.
5. **Afterwards:** delete the VM and disk ([Section 5](#5-operating-and-costing)).

**A flow that shows the pitch (about three minutes).**

1. Upload `samples/insurance/life_underwriting_01.txt` with the insurance vault. Show the three
   verdicts and the quoted evidence behind them.
2. On the threshold rule, show the single query that left the perimeter and the source it cites,
   or the `revisar` when no clear source is found.
3. Switch to the legal vault and upload `samples/legal/msa_01.txt` (then health with
   `samples/health/patient_file_01.txt`): same engine, different vault.
4. Show `/healthz` (which host the document went to) and `audit/audit.jsonl` (hashes, verdicts,
   queries, no document text).

**If judges need a link they can open.** The Nebius hackathon rules require a working demo URL
(a login is allowed if you supply the credentials). Use `deploy/public`: the app behind Caddy
with HTTPS, a visitor login, and a review cap, with synthetic documents only. The full runbook,
including the Devpost testing instructions, is in [docs/hackathon.md](../docs/hackathon.md).
Remove it when the judging window ends.

**Do not**

- Present `scripts/stub_llm.py` as a real model. It has canned answers for one file.
- Demo on the shared Token Factory endpoint while describing it as single-tenant. If you fall
  back to it, say so: it is a fine backup for synthetic documents.
- Upload real client data to any demo instance.
- Rely on live model output you have not rehearsed. Keep a recorded run as a backup.
