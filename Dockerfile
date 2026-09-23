FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv && useradd --create-home --uid 10001 sentinel

WORKDIR /app

# Dependencies first (cached layer), then the app.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY ui ./ui
COPY client ./client
COPY vaults ./vaults

RUN mkdir -p /app/audit && chown -R sentinel:sentinel /app/audit
USER sentinel

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

# The inference endpoint (LLM_BASE_URL) is required and is NOT baked in: point it at your
# dedicated vLLM server. The container makes no other outbound calls except scoped Tavily
# queries (only if TAVILY_API_KEY is set).
CMD ["uvicorn", "sentinel.api:app", "--host", "0.0.0.0", "--port", "8000"]
