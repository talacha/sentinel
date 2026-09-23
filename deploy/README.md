# Serving Nemotron 3 Nano on a dedicated Nebius GPU

Sentinel talks to any OpenAI-compatible endpoint. For the data-isolation guarantee, that endpoint
must be a **dedicated, single-tenant** vLLM server you control, not a shared API. This page covers
that setup. Model and flag details come from the
[model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16); check it for
changes before you deploy.

## 1. GPU host

Create a GPU VM in Nebius AI Cloud sized for a 30B-parameter MoE model. The BF16 weights are about
60 GB, so plan on an 80 GB-class GPU (H100/H200) or use the FP8 variant on a smaller one. Confirm
current platform names and sizes in the Nebius console; this repo does not pin them.

Networking (this is what makes the deployment single-tenant in practice):

- Put the VM and the Sentinel app in the same private network / VPC.
- Do **not** expose port 8000 to the internet. Allow it only from the Sentinel host.
- Set `--api-key` on vLLM and the same value as `LLM_API_KEY` for Sentinel; terminate TLS in front
  of it if traffic crosses hosts.

## 2. Start vLLM

Requires `vllm>=0.12.0`.

```bash
pip install "vllm>=0.12.0"

# Reasoning parser plugin (from the model repo)
wget https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16/resolve/main/nano_v3_reasoning_parser.py

vllm serve nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
  --served-model-name nemotron-3-nano \
  --host 0.0.0.0 --port 8000 \
  --api-key "$VLLM_API_KEY" \
  --trust-remote-code \
  --max-num-seqs 8 \
  --tensor-parallel-size 1 \
  --max-model-len 65536 \
  --reasoning-parser-plugin nano_v3_reasoning_parser.py \
  --reasoning-parser nano_v3
```

Notes:

- `--max-model-len` defaults to 262144 on the card; 65536 is plenty for the document sizes
  Sentinel accepts (`MAX_DOCUMENT_CHARS`, 300k characters by default) and leaves more GPU memory
  for concurrency. Raise it if you raise that limit.
- `--reasoning-parser nano_v3` separates the reasoning trace from the final answer, so
  `message.content` holds just the JSON Sentinel validates. Sentinel also strips `<think>` blocks
  defensively if a server does not use a parser.
- Reasoning is toggled per request by Sentinel through
  `chat_template_kwargs: {"enable_thinking": true|false}`: off for fact extraction, on for
  judgement calls. NVIDIA recommends `temperature=1.0, top_p=1.0` with reasoning on
  (`LLM_TEMPERATURE_REASONING`), and a large `max_tokens` (`LLM_MAX_TOKENS`, default 10000).

Smoke test from the Sentinel host:

```bash
curl -s "$LLM_BASE_URL/models" -H "Authorization: Bearer $LLM_API_KEY"
```

## 3. Point Sentinel at it

```bash
# .env
LLM_BASE_URL=http://<gpu-private-ip>:8000/v1
LLM_MODEL=nemotron-3-nano        # the --served-model-name above
LLM_API_KEY=<same as --api-key>
```

Start the app (`uv run uvicorn sentinel.api:app`, or `docker compose up --build`) and open
`/healthz`: `llm_host` shows exactly which host document text will be sent to.

## Development without a dedicated GPU

Any OpenAI-compatible endpoint works for development, for example
[Nebius Token Factory](https://docs.tokenfactory.nebius.com/) (base URL
`https://api.tokenfactory.nebius.com/v1/`, model id from its catalog). Token Factory is
multi-tenant: use it only with synthetic documents like the ones in `samples/`, never with real
client data. If a server rejects `response_format`, Sentinel falls back to prompt-only JSON and
still validates every reply locally (`LLM_STRUCTURED_MODE=prompt` skips the attempt).

## Verification (Tavily)

Set `TAVILY_API_KEY` to enable scoped verification. Only a templated query built from the vault
leaves your network; see the "What crosses the perimeter" section of the main README. Without a
key, rules that need external confirmation resolve to `revisar` with the reason stated.
