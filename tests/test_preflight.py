from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from sentinel import cli, preflight
from sentinel.config import Settings
from sentinel.llm import FakeLLM, LLMError, OpenAICompatibleLLM
from sentinel.models import Source
from sentinel.verify import FakeSearch

ROOT = Path(__file__).resolve().parents[1]
LLM_KEY = "sk-realkey-1234567890abcdef"
TAVILY_KEY = "tvly-abcdef1234567890"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("LLM_", "TAVILY_", "ACCESS_", "MAX_", "SENTINEL_")):
            monkeypatch.delenv(name, raising=False)


def make(tmp_path, **overrides) -> Settings:
    values = {
        "llm_base_url": "https://api.example.com/v1/",
        "llm_model": "nvidia/nemotron-3-nano",
        "llm_api_key": LLM_KEY,
        "tavily_api_key": TAVILY_KEY,
        "vaults_dir": ROOT / "vaults",
        "audit_log_path": tmp_path / "audit" / "audit.jsonl",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def offline(settings, raw=None, env_path=None, public=False):
    return preflight.run(settings, raw or {}, env_path, live=False, public=public)


def by_name(report):
    return {c.name: c for c in report.checks}


def env_file(tmp_path, mode=0o600) -> Path:
    path = tmp_path / ".env"
    path.write_text("LLM_MODEL=x\n")
    path.chmod(mode)
    return path


# --------------------------------------------------------------------------- static checks


def test_complete_config_passes_offline(tmp_path):
    report = offline(make(tmp_path), env_path=env_file(tmp_path))
    assert report.ok, preflight.render(report)
    assert by_name(report)["LLM_API_KEY"].detail == f"set ({len(LLM_KEY)} chars)"
    assert by_name(report)["vaults"].status == "pass"
    assert by_name(report)["audit log"].status == "pass"


def test_missing_required_values_fail(tmp_path):
    report = offline(make(tmp_path, llm_base_url=None, llm_model=None))
    checks = by_name(report)
    assert checks["LLM_BASE_URL"].status == "fail" and "no default" in checks["LLM_BASE_URL"].detail
    assert checks["LLM_MODEL"].status == "fail"
    assert not report.ok


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_base_url", "https://<your-endpoint>/v1"),
        ("llm_model", "<model id>"),
        ("llm_api_key", "your-key-here"),
        ("tavily_api_key", "tvly-xxxxxxxx"),
    ],
)
def test_placeholders_fail(tmp_path, field, value):
    report = offline(make(tmp_path, **{field: value}))
    assert not report.ok
    assert any("placeholder" in c.detail for c in report.checks if c.status == "fail")


def test_base_url_must_be_a_full_url(tmp_path):
    assert offline(make(tmp_path, llm_base_url="api.example.com/v1")).ok is False


def test_remote_endpoint_without_a_key_fails_but_private_vllm_does_not(tmp_path):
    remote = offline(make(tmp_path, llm_api_key="EMPTY"))
    assert by_name(remote)["LLM_API_KEY"].status == "fail"
    local = offline(make(tmp_path, llm_base_url="http://127.0.0.1:8000/v1", llm_api_key="EMPTY"))
    assert by_name(local)["LLM_API_KEY"].status == "pass" and local.ok


def test_plain_http_to_a_remote_host_warns(tmp_path):
    report = offline(make(tmp_path, llm_base_url="http://api.example.com/v1"))
    assert by_name(report)["LLM_BASE_URL"].status == "warn"
    private = offline(make(tmp_path, llm_base_url="http://10.1.2.3:8000/v1"))
    assert by_name(private)["LLM_BASE_URL"].status == "pass"


def test_key_with_whitespace_fails(tmp_path):
    report = offline(make(tmp_path, llm_api_key=LLM_KEY + " "))
    assert by_name(report)["LLM_API_KEY"].status == "fail"


def test_missing_tavily_key_warns_not_fails(tmp_path):
    report = offline(make(tmp_path, tavily_api_key=None))
    assert by_name(report)["TAVILY_API_KEY"].status == "warn" and report.ok
    odd = offline(make(tmp_path, tavily_api_key="abcdef1234567890"))
    assert by_name(odd)["TAVILY_API_KEY"].status == "warn"


def test_typos_in_env_keys_are_caught_with_a_suggestion(tmp_path):
    report = offline(
        make(tmp_path), raw={"LLM_BASEURL": "x", "TAVILY_API_KEY": "y", "SENTINEL_DOMAIN": ""}
    )
    detail = by_name(report)["unknown keys"].detail
    assert by_name(report)["unknown keys"].status == "warn"
    assert "LLM_BASEURL (did you mean LLM_BASE_URL?)" in detail
    assert "SENTINEL_DOMAIN" not in detail and "TAVILY_API_KEY" not in detail


def test_world_readable_env_file_warns(tmp_path):
    report = offline(make(tmp_path), env_path=env_file(tmp_path, mode=0o644))
    assert by_name(report)[".env permissions"].status == "warn"
    assert "chmod 600" in by_name(report)[".env permissions"].detail


def test_missing_env_file_warns(tmp_path):
    assert by_name(offline(make(tmp_path), env_path=None))[".env file"].status == "warn"


def test_bad_vault_dir_fails(tmp_path):
    report = offline(make(tmp_path, vaults_dir=tmp_path / "nope"))
    assert by_name(report)["vaults"].status == "fail" or not report.ok


def test_unwritable_audit_location_fails(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        report = offline(make(tmp_path, audit_log_path=locked / "sub" / "audit.jsonl"))
        if os.access(locked, os.W_OK):  # running as root: permissions do not apply
            pytest.skip("directory is writable for this user")
        assert by_name(report)["audit log"].status == "fail"
    finally:
        locked.chmod(0o700)


# --------------------------------------------------------------------------- public mode


def public_settings(tmp_path, **overrides):
    values = {"access_password": PASSWORD, "max_reviews_per_hour": 30}
    values.update(overrides)
    return make(tmp_path, **values)


PUBLIC_RAW = {"SENTINEL_DOMAIN": "demo.example.com"}


def test_public_mode_passes_with_login_cap_and_domain(tmp_path):
    report = offline(public_settings(tmp_path), raw=PUBLIC_RAW, public=True)
    assert report.ok, preflight.render(report)


def test_public_mode_requires_a_login(tmp_path):
    report = offline(public_settings(tmp_path, access_password=None), raw=PUBLIC_RAW, public=True)
    assert by_name(report)["ACCESS_PASSWORD"].status == "fail" and not report.ok
    # ...but a local run without login is fine.
    assert offline(make(tmp_path)).ok


@pytest.mark.parametrize("weak", ["demo", "password", "short1"])
def test_weak_passwords_fail_in_public_mode(tmp_path, weak):
    report = offline(public_settings(tmp_path, access_password=weak), raw=PUBLIC_RAW, public=True)
    assert by_name(report)["ACCESS_PASSWORD"].status == "fail"


def test_uncapped_usage_warns_in_public_mode(tmp_path):
    report = offline(public_settings(tmp_path, max_reviews_per_hour=0), raw=PUBLIC_RAW, public=True)
    assert by_name(report)["MAX_REVIEWS_PER_HOUR"].status == "warn" and report.ok


@pytest.mark.parametrize(
    ("domain", "status"),
    [("", "fail"), ("not a host", "fail"), ("localhost", "warn"), ("demo.example.com", "pass")],
)
def test_domain_validation_in_public_mode(tmp_path, domain, status):
    report = offline(public_settings(tmp_path), raw={"SENTINEL_DOMAIN": domain}, public=True)
    assert by_name(report)["SENTINEL_DOMAIN"].status == status


# --------------------------------------------------------------------------- live checks


def models_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def listing(ids):
    return lambda request: httpx.Response(200, json={"data": [{"id": i} for i in ids]})


def ping_llm() -> FakeLLM:
    return FakeLLM(lambda s, u, schema, r: {"ok": True, "word": "sentinel"})


def live(settings, *, http=None, llm=None, search=None):
    return preflight.run(
        settings,
        {},
        None,
        live=True,
        http=http or models_client(listing(["nvidia/nemotron-3-nano"])),
        llm=llm or ping_llm(),
        search=search or FakeSearch([Source(url="https://x.example/a")]),
    )


def test_live_happy_path(tmp_path):
    report = live(make(tmp_path))
    checks = by_name(report)
    assert report.ok, preflight.render(report)
    assert checks["endpoint reachable"].status == "pass"
    assert checks["model id"].status == "pass"
    assert checks["extraction call"].status == "pass"
    assert checks["judgement call (reasoning)"].status == "pass"
    assert checks["Tavily search"].status == "pass"


def test_live_checks_send_the_key_only_as_a_bearer_header_to_the_configured_host(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "nvidia/nemotron-3-nano"}]})

    live(make(tmp_path), http=models_client(handler))
    assert [r.url.host for r in seen] == ["api.example.com"]
    assert seen[0].url.path == "/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {LLM_KEY}"


def test_wrong_model_id_fails_with_suggestions(tmp_path):
    ids = ["meta-llama/Meta-Llama-3.1-8B", "nvidia/nemotron-3-super-120b-a12b"]
    report = live(make(tmp_path), http=models_client(listing(ids)))
    detail = by_name(report)["model id"].detail
    assert by_name(report)["model id"].status == "fail"
    assert "nvidia/nemotron-3-super-120b-a12b" in detail and "not listed" in detail


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_fails(tmp_path, status):
    report = live(make(tmp_path), http=models_client(lambda r: httpx.Response(status)))
    assert by_name(report)["endpoint reachable"].status == "fail"
    assert str(status) in by_name(report)["endpoint reachable"].detail


def test_endpoint_without_a_models_route_only_warns(tmp_path):
    report = live(make(tmp_path), http=models_client(lambda r: httpx.Response(404)))
    assert by_name(report)["endpoint reachable"].status == "warn"


def test_unreachable_endpoint_fails(tmp_path):
    def boom(request):
        raise httpx.ConnectError("connection refused")

    report = live(make(tmp_path), http=models_client(boom))
    assert by_name(report)["endpoint reachable"].status == "fail"
    assert "ConnectError" in by_name(report)["endpoint reachable"].detail


def test_model_that_cannot_produce_json_fails(tmp_path):
    def handler(system, user, schema, reasoning):
        raise LLMError("LLM returned an empty reply (output truncated; raise LLM_MAX_TOKENS)")

    report = live(make(tmp_path), llm=FakeLLM(handler))
    assert by_name(report)["extraction call"].status == "fail"
    assert "LLM_MAX_TOKENS" in by_name(report)["judgement call (reasoning)"].detail
    assert not report.ok


def test_server_rejecting_response_format_is_reported_as_a_warning(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "unsupported", "type": "x"}})
        content = json.dumps({"ok": True, "word": "sentinel"})
        return httpx.Response(
            200,
            json={
                "id": "1",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    llm = OpenAICompatibleLLM(
        "https://api.example.com/v1",
        "m",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    report = live(make(tmp_path), llm=llm)
    check = by_name(report)["structured output"]
    assert check.status == "warn" and "LLM_STRUCTURED_MODE=prompt" in check.detail
    assert report.ok  # a warning, not a failure: replies are still validated


def test_tavily_failure_fails_and_redacts_the_key(tmp_path):
    search = FakeSearch(error=RuntimeError(f"401 Unauthorized for key {TAVILY_KEY}"))
    report = live(make(tmp_path), search=search)
    detail = by_name(report)["Tavily search"].detail
    assert by_name(report)["Tavily search"].status == "fail"
    assert TAVILY_KEY not in detail and "***" in detail


def test_no_secret_ever_appears_in_the_rendered_report(tmp_path):
    settings = public_settings(tmp_path)

    def leaky(request):
        return httpx.Response(401, text=f"bad key {LLM_KEY}")

    def handler(system, user, schema, reasoning):
        raise LLMError(f"upstream said: {LLM_KEY} is invalid; password {PASSWORD}")

    report = preflight.run(
        settings,
        PUBLIC_RAW,
        None,
        live=True,
        public=True,
        http=models_client(leaky),
        llm=FakeLLM(handler),
        search=FakeSearch(error=RuntimeError(TAVILY_KEY)),
    )
    text = preflight.render(report)
    for secret in (LLM_KEY, TAVILY_KEY, PASSWORD):
        assert secret not in text


def test_live_checks_are_skipped_when_llm_settings_are_broken(tmp_path):
    report = live(make(tmp_path, llm_model=None))
    assert by_name(report)["live model checks"].status == "skip"
    assert "endpoint reachable" not in by_name(report)


def test_no_tavily_key_skips_the_live_search(tmp_path):
    report = live(make(tmp_path, tavily_api_key=None))
    assert by_name(report)["Tavily search"].status == "skip"


# --------------------------------------------------------------------------- CLI


def test_cli_offline_ready_and_not_ready(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    good = tmp_path / "good.env"
    good.write_text(
        f"LLM_BASE_URL=https://api.example.com/v1\nLLM_MODEL=m\nLLM_API_KEY={LLM_KEY}\n"
        f"TAVILY_API_KEY={TAVILY_KEY}\nAUDIT_LOG_PATH={tmp_path}/audit/a.jsonl\n"
    )
    good.chmod(0o600)
    assert cli.main(["preflight", "--env-file", str(good), "--offline"]) == 0
    out = capsys.readouterr().out
    assert "READY" in out and LLM_KEY not in out and TAVILY_KEY not in out

    bad = tmp_path / "bad.env"
    bad.write_text("LLM_BASE_URL=\nLLM_MODEL=\n")
    bad.chmod(0o600)
    assert cli.main(["preflight", "--env-file", str(bad), "--offline"]) == 1
    assert "NOT READY" in capsys.readouterr().out


def test_cli_reports_invalid_settings_without_a_traceback_or_echoing_values(tmp_path, capsys):
    path = tmp_path / "broken.env"
    path.write_text("LLM_MAX_TOKENS=abc-secret-looking-value\n")
    assert cli.main(["preflight", "--env-file", str(path), "--offline"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] llm_max_tokens" in out and "abc-secret-looking-value" not in out


def test_cli_public_flag_demands_login(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    path = tmp_path / "pub.env"
    path.write_text(
        f"LLM_BASE_URL=https://api.example.com/v1\nLLM_MODEL=m\nLLM_API_KEY={LLM_KEY}\n"
        f"AUDIT_LOG_PATH={tmp_path}/audit/a.jsonl\n"
    )
    path.chmod(0o600)
    assert cli.main(["preflight", "--env-file", str(path), "--offline", "--public"]) == 1
    assert "ACCESS_PASSWORD" in capsys.readouterr().out
