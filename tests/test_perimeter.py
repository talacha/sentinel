from __future__ import annotations

import os

import pytest

from sentinel.config import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("LLM_", "TAVILY_", "ACCESS_", "MAX_", "SENTINEL_")):
            monkeypatch.delenv(name, raising=False)


def perimeter(base_url: str | None) -> str:
    return Settings(llm_base_url=base_url, llm_model="m").llm_perimeter


# Provably not a public multi-tenant API: the request cannot leave the host or the private network.
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/v1",
        "http://localhost:8000/v1",
        "http://[::1]:8000/v1",
        "http://10.0.0.4:8000/v1",
        "http://172.16.3.9:8000/v1",
        "http://172.31.255.254:8000/v1",
        "http://192.168.1.50:8000/v1",
        "http://169.254.10.1:8000/v1",
        "http://gpu-01.internal:8000/v1",
        "http://gpu-01.local:8000/v1",
    ],
)
def test_private_endpoints_are_dedicated(url):
    assert perimeter(url) == "dedicated"


# Known shared, multi-tenant inference endpoints: never for client data.
@pytest.mark.parametrize(
    "url",
    [
        "https://api.tokenfactory.nebius.com/v1/",
        "https://API.TokenFactory.Nebius.com/v1/",
        "https://api.studio.nebius.ai/v1/",
        "https://api.openai.com/v1",
    ],
)
def test_known_shared_endpoints_are_shared(url):
    assert perimeter(url) == "shared"


# Never guess: a public host may be the user's own dedicated GPU or somebody's shared API.
@pytest.mark.parametrize(
    "url",
    [
        "https://inference.acme.example.com/v1",
        "http://89.169.100.20:8000/v1",  # a public IP: could be our own GPU, or anyone's
        "http://172.32.0.1:8000/v1",  # just outside RFC1918's 172.16/12
        "http://11.0.0.1:8000/v1",  # just outside 10/8
        None,
        "",
        "not a url",
    ],
)
def test_everything_else_is_unknown(url):
    assert perimeter(url) == "unknown"
