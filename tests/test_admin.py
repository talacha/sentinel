"""The /status and /admin console: who may use it, what it can change, and what it never reveals."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from sentinel.api import create_app
from sentinel.config import Settings
from sentinel.engine import Engine
from sentinel.llm import FakeLLM
from sentinel.runtime import RuntimeConfig
from sentinel.vault import VaultRegistry
from sentinel.verify import FakeSearch

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ("root", "admin-pass-9f3k2-long-enough")
VISITOR = ("guest", "visitor-pass-77x1-long-enough")
LLM_KEY = "sk-llm-secret-1234567890"
TAVILY_KEY = "tvly-secret-1234567890"
HEADER = {"X-Sentinel-Admin": "1"}

VAULT = {
    "id": "demo",
    "title": "Demo vault",
    "rules": [{"id": "signed", "title": "Signed", "criterion": "Signed.", "on_fail": "no_cumple"}],
}


def base_settings(tmp_path, **overrides) -> Settings:
    vaults = tmp_path / "vaults"
    vaults.mkdir(exist_ok=True)
    (vaults / "demo.yaml").write_text(yaml.safe_dump(VAULT))
    values = {
        "vaults_dir": vaults,
        "audit_log_path": tmp_path / "audit" / "audit.jsonl",
        "llm_base_url": "https://api.example.com/v1/",
        "llm_model": "model-a",
        "llm_api_key": LLM_KEY,
        "tavily_api_key": TAVILY_KEY,
        "admin_user": ADMIN[0],
        "admin_password": ADMIN[1],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def ok_llm() -> FakeLLM:
    def handler(system, user, schema, reasoning):
        if schema.__name__ == "PingOut":
            return {"ok": True, "word": "sentinel"}
        return {"verdict": "cumple", "rationale": "ok", "evidence": ["Signed: yes"]}

    return FakeLLM(handler)


def listing(ids):
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        )
    )


def make(tmp_path, *, deps=None, ui=False, **settings):
    cfg = base_settings(tmp_path, **settings)
    app = create_app(
        cfg,
        Engine(ok_llm()),
        VaultRegistry(cfg.vaults_dir),
        ui_dir=ROOT / "ui" if ui else None,
        client_dir=None,
        preflight_deps=deps
        if deps is not None
        else {"http": listing(["model-a"]), "llm": ok_llm(), "search": FakeSearch([])},
    )
    return TestClient(app), cfg


def put(client, body, **kwargs):
    kwargs.setdefault("auth", ADMIN)
    kwargs.setdefault("headers", HEADER)
    return client.put("/v1/admin/config", json=body, **kwargs)


def field(client, name):
    fields = client.get("/v1/admin/config", auth=ADMIN).json()["fields"]
    return next(f for f in fields if f["name"] == name)


# --------------------------------------------------------------------------- access


def test_admin_api_is_disabled_unless_an_admin_password_is_set(tmp_path):
    client, _ = make(tmp_path, admin_password=None)
    for path in ("/v1/admin/config", "/v1/admin/status"):
        resp = client.get(path, auth=ADMIN)
        assert resp.status_code == 403 and "ADMIN_PASSWORD" in resp.json()["detail"]


def test_admin_api_needs_the_admin_login_not_the_visitor_login(tmp_path):
    client, _ = make(tmp_path, access_password=VISITOR[1], access_user=VISITOR[0])
    assert client.get("/v1/admin/config").status_code == 401
    assert client.get("/v1/admin/config", auth=VISITOR).status_code == 401
    assert client.get("/v1/admin/config", auth=("root", "wrong")).status_code == 401
    assert client.get("/v1/admin/config", auth=ADMIN).status_code == 200


def test_admin_credentials_also_open_the_visitor_pages(tmp_path):
    client, _ = make(tmp_path, access_password=VISITOR[1], access_user=VISITOR[0])
    assert client.get("/v1/vaults").status_code == 401
    assert client.get("/v1/vaults", auth=ADMIN).status_code == 200
    assert client.get("/healthz", auth=ADMIN).json()["llm_model"] == "model-a"


def test_admin_401_never_triggers_the_browsers_native_prompt(tmp_path):
    client, _ = make(tmp_path)
    resp = client.get("/v1/admin/config", headers={"Accept": "text/html"})
    assert resp.status_code == 401 and "www-authenticate" not in resp.headers


def test_console_pages_are_data_free_shells_even_behind_a_visitor_login(tmp_path):
    client, _ = make(tmp_path, ui=True, access_password=VISITOR[1])
    for path in ("/status", "/admin", "/admin/config", "/admin/users", "/admin/vaults"):
        page = client.get(path)
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert page.headers["cache-control"] == "no-store"
        for secret in (LLM_KEY, TAVILY_KEY, ADMIN[1], VISITOR[1]):
            assert secret not in page.text


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({}, 403), ({"X-Sentinel-Admin": "0"}, 403)],
)
def test_writes_need_the_custom_header(tmp_path, headers, expected):
    client, _ = make(tmp_path)
    resp = client.put(
        "/v1/admin/config", json={"set": {"llm_model": "x"}}, auth=ADMIN, headers=headers
    )
    assert resp.status_code == expected
    assert field(client, "llm_model")["value"] == "model-a"  # nothing changed


def test_writes_must_be_json(tmp_path):
    client, _ = make(tmp_path)
    resp = client.put(
        "/v1/admin/config",
        data={"set": "x"},
        auth=ADMIN,
        headers=HEADER,  # a plain HTML form
    )
    assert resp.status_code == 415


def test_a_live_check_also_needs_the_header(tmp_path):
    client, _ = make(tmp_path)
    assert client.post("/v1/admin/status/live", auth=ADMIN).status_code == 403
    assert client.post("/v1/admin/status/live", auth=ADMIN, headers=HEADER).status_code == 200


# --------------------------------------------------------------------------- config view


def test_config_view_describes_every_setting_and_never_reveals_secrets(tmp_path):
    client, _ = make(tmp_path)
    resp = client.get("/v1/admin/config", auth=ADMIN)
    text = resp.text
    for secret in (LLM_KEY, TAVILY_KEY, ADMIN[1]):
        assert secret not in text
    assert "admin_password" not in text and "admin_user" not in text  # not even listed

    key = field(client, "llm_api_key")
    assert key["secret"] and key["is_set"] is True and key["length"] == len(LLM_KEY)
    assert "value" not in key
    assert field(client, "llm_model")["value"] == "model-a"
    assert field(client, "max_workers")["min"] == 1
    assert field(client, "llm_structured_mode")["choices"] == [
        "json_schema",
        "guided_json",
        "prompt",
    ]
    assert field(client, "llm_reasoning")["kind"] == "bool"
    for readonly in ("vaults_dir", "audit_log_path", "cors_allow_origins"):
        assert field(client, readonly)["editable"] is False


# --------------------------------------------------------------------------- applying changes


def test_a_valid_change_takes_effect_immediately_and_is_persisted_privately(tmp_path):
    client, cfg = make(tmp_path)
    resp = put(client, {"set": {"llm_model": "model-b", "max_upload_mb": 5}})
    assert resp.status_code == 200 and resp.json()["changed"] == ["llm_model", "max_upload_mb"]

    health = client.get("/healthz", auth=ADMIN).json()
    assert health["llm_model"] == "model-b" and health["max_upload_mb"] == 5
    assert field(client, "llm_model")["overridden"] is True
    assert field(client, "llm_structured_mode")["overridden"] is False

    saved = cfg.audit_log_path.parent / "overrides.json"
    assert json.loads(saved.read_text())["overrides"] == {
        "llm_model": "model-b",
        "max_upload_mb": 5,
    }
    assert saved.stat().st_mode & 0o777 == 0o600


def test_changes_survive_a_restart(tmp_path):
    client, cfg = make(tmp_path)
    put(client, {"set": {"llm_model": "model-b"}})
    restarted = RuntimeConfig(cfg, cfg.audit_log_path.parent / "overrides.json")
    assert restarted.settings.llm_model == "model-b"
    assert cfg.llm_model == "model-a"  # the environment value is untouched underneath


def test_changing_the_model_rebuilds_the_engine_from_the_new_settings(tmp_path, monkeypatch):
    built = []

    def fake_from_settings(cls, settings):
        built.append(settings.llm_model)
        return Engine(ok_llm())

    monkeypatch.setattr(Engine, "from_settings", classmethod(fake_from_settings))
    client, _ = make(tmp_path)
    put(client, {"set": {"llm_model": "model-b"}})
    review = client.post(
        "/v1/reviews",
        data={"vault_id": "demo"},
        files={"file": ("a.txt", b"Signed: yes")},
        auth=ADMIN,
    )
    assert review.status_code == 200 and built == ["model-b"]


def test_changing_the_review_cap_applies_to_the_running_limiter(tmp_path):
    client, _ = make(tmp_path)
    put(client, {"set": {"max_reviews_per_hour": 1}})
    send = lambda: client.post(  # noqa: E731
        "/v1/reviews",
        data={"vault_id": "demo"},
        files={"file": ("a.txt", b"Signed: yes")},
        auth=ADMIN,
    )
    assert send().status_code == 200
    assert send().status_code == 429


def test_clearing_an_override_falls_back_to_the_environment_value(tmp_path):
    client, _ = make(tmp_path)
    put(client, {"set": {"llm_model": "model-b"}})
    resp = put(client, {"clear": ["llm_model"]})
    assert resp.json()["changed"] == ["llm_model"]
    assert field(client, "llm_model")["value"] == "model-a"
    assert field(client, "llm_model")["overridden"] is False


@pytest.mark.parametrize(
    ("body", "bad_field"),
    [
        ({"set": {"max_workers": 0}}, "max_workers"),
        ({"set": {"max_workers": "many"}}, "max_workers"),
        ({"set": {"llm_structured_mode": "yaml"}}, "llm_structured_mode"),
        ({"set": {"llm_reasoning": "maybe"}}, "llm_reasoning"),
        ({"set": {"llm_base_url": "not a url"}}, "llm_base_url"),
        ({"set": {"llm_api_key": ""}}, "llm_api_key"),
        ({"set": {"vaults_dir": "/etc"}}, "vaults_dir"),
        ({"set": {"audit_log_path": "/tmp/x"}}, "audit_log_path"),
        ({"set": {"admin_password": "hijacked-password-123"}}, "admin_password"),
        ({"set": {"no_such_setting": 1}}, "no_such_setting"),
        ({"clear": ["admin_password"]}, "admin_password"),
    ],
)
def test_invalid_changes_are_rejected_with_a_reason(tmp_path, body, bad_field):
    client, cfg = make(tmp_path)
    resp = put(client, body)
    assert resp.status_code == 422
    assert bad_field in resp.json()["detail"]["errors"]
    assert not (cfg.audit_log_path.parent / "overrides.json").exists()


def test_a_bad_batch_applies_nothing_at_all(tmp_path):
    client, cfg = make(tmp_path)
    resp = put(client, {"set": {"llm_model": "model-b", "max_workers": 0}})
    assert resp.status_code == 422
    assert field(client, "llm_model")["value"] == "model-a"  # the valid half was not applied
    assert not (cfg.audit_log_path.parent / "overrides.json").exists()


def test_error_messages_never_echo_submitted_values(tmp_path):
    client, _ = make(tmp_path)
    resp = put(client, {"set": {"max_workers": "supersecret-looking-value"}})
    assert "supersecret-looking-value" not in resp.text


def test_secrets_can_be_replaced_but_never_come_back_or_reach_the_audit_log(tmp_path):
    client, cfg = make(tmp_path)
    new_key = "sk-brand-new-key-987654321"
    resp = put(client, {"set": {"llm_api_key": new_key}})
    assert resp.status_code == 200 and resp.json()["changed"] == ["llm_api_key"]
    assert new_key not in resp.text and LLM_KEY not in resp.text
    assert field(client, "llm_api_key")["length"] == len(new_key)

    audit = cfg.audit_log_path.read_text()
    assert new_key not in audit and LLM_KEY not in audit
    event = json.loads(audit.splitlines()[-1])
    assert event["event"] == "config_change" and event["secrets_changed"] == ["llm_api_key"]


def test_the_audit_log_records_who_changed_what_but_only_a_host_for_urls(tmp_path):
    client, cfg = make(tmp_path)
    put(client, {"set": {"llm_base_url": "https://api.other.example/v1/", "llm_max_tokens": 4000}})
    event = json.loads(cfg.audit_log_path.read_text().splitlines()[-1])
    assert event["admin"] == ADMIN[0]
    assert sorted(event["changed"]) == ["llm_base_url", "llm_max_tokens"]
    assert event["values"] == {"llm_base_url": "api.other.example", "llm_max_tokens": 4000}


def test_clearing_a_secret_removes_the_override_without_logging_it(tmp_path):
    client, cfg = make(tmp_path)
    put(client, {"set": {"tavily_api_key": "tvly-another-987654321"}})
    resp = put(client, {"clear": ["tavily_api_key"]})
    assert field(client, "tavily_api_key")["length"] == len(TAVILY_KEY)  # back to the env value
    assert "tvly-another-987654321" not in cfg.audit_log_path.read_text()
    assert resp.status_code == 200


def test_corrupt_or_invalid_saved_overrides_do_not_stop_the_service(tmp_path):
    cfg = base_settings(tmp_path)
    path = cfg.audit_log_path.parent / "overrides.json"
    path.parent.mkdir(parents=True)

    path.write_text("{not json")
    assert RuntimeConfig(cfg, path).settings.llm_model == "model-a"
    assert "unreadable" in (RuntimeConfig(cfg, path).load_error or "")

    path.write_text(json.dumps({"version": 1, "overrides": {"max_workers": 0}}))
    runtime = RuntimeConfig(cfg, path)
    assert runtime.settings.max_workers == cfg.max_workers
    assert "ignored" in (runtime.load_error or "")

    path.write_text(
        json.dumps({"version": 1, "overrides": {"admin_password": "x", "llm_model": "m2"}})
    )
    assert RuntimeConfig(cfg, path).settings.admin_password == ADMIN[1]  # not editable, dropped


# --------------------------------------------------------------------------- status


def by_name(payload):
    return {c["name"]: c for c in payload["checks"]}


def test_static_status_makes_no_network_calls_and_groups_checks(tmp_path):
    seen = []
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(500))
    )
    client, _ = make(tmp_path, deps={"http": http, "llm": ok_llm(), "search": FakeSearch([])})
    payload = client.get("/v1/admin/status", auth=ADMIN).json()
    assert seen == [] and payload["live"] is False
    checks = by_name(payload)
    assert (
        checks["LLM_API_KEY"]["group"] == "Model" and checks["TAVILY_API_KEY"]["group"] == "Search"
    )
    assert checks["vaults"]["group"] == "Storage"
    assert ".env file" not in checks  # a running service has no .env to check


def test_live_status_confirms_keys_and_that_the_model_exists(tmp_path):
    client, _ = make(tmp_path)
    payload = client.post("/v1/admin/status/live", auth=ADMIN, headers=HEADER).json()
    checks = by_name(payload)
    assert payload["ready"] is True and payload["live"] is True
    assert checks["endpoint reachable"]["status"] == "pass"
    assert checks["model id"]["status"] == "pass"
    assert checks["extraction call"]["status"] == "pass"
    assert checks["Tavily search"]["status"] == "pass"


def test_live_status_flags_a_model_that_does_not_exist(tmp_path):
    deps = {"http": listing(["something-else"]), "llm": ok_llm(), "search": FakeSearch([])}
    client, _ = make(tmp_path, deps=deps)
    payload = client.post("/v1/admin/status/live", auth=ADMIN, headers=HEADER).json()
    assert payload["ready"] is False
    assert by_name(payload)["model id"]["status"] == "fail"
    assert "not listed" in by_name(payload)["model id"]["detail"]


def test_status_reports_admin_overrides_and_never_any_secret(tmp_path):
    deps = {
        "http": listing(["model-a"]),
        "llm": ok_llm(),
        "search": FakeSearch(error=RuntimeError(f"bad key {TAVILY_KEY}")),
    }
    client, _ = make(tmp_path, deps=deps)
    put(client, {"set": {"llm_max_tokens": 5000}})
    payload = client.post("/v1/admin/status/live", auth=ADMIN, headers=HEADER).json()
    assert "llm_max_tokens" in by_name(payload)["admin overrides"]["detail"]
    text = json.dumps(payload)
    for secret in (LLM_KEY, TAVILY_KEY, ADMIN[1]):
        assert secret not in text


def test_status_reflects_a_change_made_in_the_admin_console(tmp_path):
    client, _ = make(tmp_path)
    put(client, {"set": {"llm_model": "model-c"}})
    payload = client.post("/v1/admin/status/live", auth=ADMIN, headers=HEADER).json()
    # The fake model list only serves model-a, so the new id is reported as missing.
    assert by_name(payload)["model id"]["status"] == "fail"


def test_only_one_live_check_runs_at_a_time(tmp_path):
    started, release = threading.Event(), threading.Event()

    def handler(system, user, schema, reasoning):
        started.set()
        release.wait(5)
        return {"ok": True, "word": "sentinel"}

    deps = {"http": listing(["model-a"]), "llm": FakeLLM(handler), "search": FakeSearch([])}
    client, _ = make(tmp_path, deps=deps)
    first = {}
    worker = threading.Thread(
        target=lambda: first.update(
            code=client.post("/v1/admin/status/live", auth=ADMIN, headers=HEADER).status_code
        )
    )
    worker.start()
    assert started.wait(5)
    second = client.post("/v1/admin/status/live", auth=ADMIN, headers=HEADER)
    release.set()
    worker.join(10)
    assert second.status_code == 409 and first["code"] == 200


# --------------------------------------------------------------------------- brute-force protection


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def throttled_client(tmp_path, clock, **settings):
    from sentinel.limits import FailureThrottle

    cfg = base_settings(tmp_path, **settings)
    app = create_app(
        cfg,
        Engine(ok_llm()),
        VaultRegistry(cfg.vaults_dir),
        ui_dir=None,
        client_dir=None,
        admin_throttle=FailureThrottle(max_failures=3, window=60, clock=clock),
    )
    return TestClient(app)


def test_repeated_wrong_admin_passwords_lock_the_admin_login_temporarily(tmp_path):
    clock = Clock()
    client = throttled_client(tmp_path, clock)
    for _ in range(3):
        assert client.get("/v1/admin/config", auth=("root", "guess")).status_code == 401
    blocked = client.get("/v1/admin/config", auth=("root", "guess"))
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) == 60
    # Even the correct password is refused while locked, so guessing cannot succeed by luck.
    assert client.get("/v1/admin/config", auth=ADMIN).status_code == 429
    clock.now += 61
    assert client.get("/v1/admin/config", auth=ADMIN).status_code == 200


def test_requests_without_credentials_do_not_count_as_failures(tmp_path):
    client = throttled_client(tmp_path, Clock())
    for _ in range(10):
        assert client.get("/v1/admin/config").status_code == 401  # just a missing login
    assert client.get("/v1/admin/config", auth=ADMIN).status_code == 200


def test_a_successful_sign_in_clears_earlier_failures(tmp_path):
    client = throttled_client(tmp_path, Clock())
    for _ in range(2):
        client.get("/v1/admin/config", auth=("root", "guess"))
    assert client.get("/v1/admin/config", auth=ADMIN).status_code == 200
    for _ in range(2):
        assert client.get("/v1/admin/config", auth=("root", "guess")).status_code == 401
    assert client.get("/v1/admin/config", auth=ADMIN).status_code == 200


def test_the_admin_lockout_never_affects_visitors(tmp_path):
    client = throttled_client(tmp_path, Clock(), access_password=VISITOR[1], access_user=VISITOR[0])
    for _ in range(5):
        client.get("/v1/admin/config", auth=("root", "guess"))
    assert client.get("/v1/admin/config", auth=ADMIN).status_code == 429
    assert client.get("/v1/vaults", auth=VISITOR).status_code == 200
    assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- the console shell

CONSOLE = (ROOT / "ui" / "console.html").read_text()


def test_the_console_keeps_hidden_elements_hidden():
    # Author `display:` rules (grid, flex, ...) beat the browser's own [hidden] rule, so without
    # this one a "hidden" label, button, or save bar is still shown. jsdom cannot catch that;
    # it was found in a real browser.
    assert "[hidden] { display:none !important; }" in CONSOLE
    assert "display:flex !important" not in CONSOLE  # would out-rank the rule above


def test_every_console_link_leads_to_a_served_page(tmp_path):
    client, _ = make(tmp_path, ui=True)
    nav = re.findall(r'<a id="nav-\w+" href="([^"]+)"', CONSOLE)
    tiles = re.findall(r'<a class="tile" href="([^"]+)"', CONSOLE)
    assert nav == ["/admin", "/status", "/admin/config", "/admin/users", "/admin/vaults"]
    assert tiles == nav[1:]  # the overview lists every option except itself
    for path in nav:
        assert client.get(path).status_code == 200
