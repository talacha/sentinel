"""Login, CORS, and the review cap: what makes a public deployment safe to expose."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sentinel.api import create_app
from sentinel.config import Settings
from sentinel.engine import Engine
from sentinel.limits import HourlyLimiter
from sentinel.llm import FakeLLM
from sentinel.vault import VaultRegistry

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct-horse-battery-staple"
ORIGIN = "https://app.example.com"

VAULT = {
    "id": "demo",
    "title": "Demo vault",
    "rules": [{"id": "signed", "title": "Signed", "criterion": "Signed.", "on_fail": "no_cumple"}],
}
DOC = b"Signed: yes\n"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_client(tmp_path, *, limiter=None, ui=False, **settings):
    vaults = tmp_path / "vaults"
    vaults.mkdir(exist_ok=True)
    (vaults / "demo.yaml").write_text(yaml.safe_dump(VAULT))
    llm = FakeLLM(
        lambda s, u, schema, r: {
            "verdict": "cumple",
            "rationale": "ok",
            "evidence": ["Signed: yes"],
        }
    )
    cfg = Settings(_env_file=None, vaults_dir=vaults, **settings)
    app = create_app(
        cfg,
        Engine(llm),
        VaultRegistry(vaults),
        ui_dir=ROOT / "ui" if ui else None,
        client_dir=None,
        limiter=limiter,
    )
    return TestClient(app), llm


def secured(tmp_path, **extra):
    return make_client(tmp_path, access_password=PASSWORD, **extra)


def post(client, **kwargs):
    return client.post(
        "/v1/reviews", data={"vault_id": "demo"}, files={"file": ("a.txt", DOC)}, **kwargs
    )


# --------------------------------------------------------------------------- login


def test_login_is_off_by_default(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.get("/v1/vaults").status_code == 200
    assert client.get("/healthz").json()["auth_required"] is False


def test_requests_without_credentials_are_rejected_and_never_reach_the_model(tmp_path):
    client, llm = secured(tmp_path)
    assert client.get("/v1/vaults").status_code == 401
    assert post(client).status_code == 401
    assert llm.calls == []


def test_correct_credentials_are_accepted(tmp_path):
    client, _ = secured(tmp_path, access_user="judge")
    assert client.get("/v1/vaults", auth=("judge", PASSWORD)).status_code == 200
    assert post(client, auth=("judge", PASSWORD)).status_code == 200


@pytest.mark.parametrize(
    "auth", [("demo", "wrong"), ("someone", PASSWORD), ("demo", PASSWORD[:-1]), ("", "")]
)
def test_wrong_credentials_are_rejected(tmp_path, auth):
    client, _ = secured(tmp_path)
    assert client.get("/v1/vaults", auth=auth).status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "Basic !!!not-base64!!!",
        "Bearer " + PASSWORD,
        "Basic " + base64.b64encode(b"nocolon").decode(),
    ],
)
def test_malformed_authorization_headers_are_rejected(tmp_path, header):
    client, _ = secured(tmp_path)
    assert client.get("/v1/vaults", headers={"Authorization": header}).status_code == 401


def test_password_may_contain_colons_and_unicode(tmp_path):
    client, _ = make_client(tmp_path, access_password="p:ässwörd:with:colons-123")
    assert client.get("/v1/vaults", auth=("demo", "p:ässwörd:with:colons-123")).status_code == 200


def test_browser_login_prompt_is_only_sent_to_page_navigations(tmp_path):
    client, _ = secured(tmp_path)
    page = client.get("/v1/vaults", headers={"Accept": "text/html"})
    assert page.headers["www-authenticate"].startswith("Basic")
    api = client.get("/v1/vaults", headers={"Accept": "application/json"})
    assert "www-authenticate" not in api.headers  # lets a client app show its own login form


def test_the_built_in_ui_is_behind_the_login_too(tmp_path):
    client, _ = make_client(tmp_path, ui=True, access_password=PASSWORD)
    assert client.get("/").status_code == 401
    assert client.get("/static/app.js").status_code == 401
    assert client.get("/", auth=("demo", PASSWORD)).status_code == 200


def test_healthz_stays_open_but_only_reveals_configuration_to_authorized_callers(tmp_path):
    client, _ = secured(
        tmp_path,
        llm_base_url="https://gpu.internal:8000/v1",
        llm_model="m",
        tavily_api_key="tvly-1",
    )
    anonymous = client.get("/healthz")
    assert anonymous.status_code == 200
    assert anonymous.json() == {"status": "ok", "auth_required": True, "max_upload_mb": 20}
    full = client.get("/healthz", auth=("demo", PASSWORD)).json()
    assert full["llm_host"] == "gpu.internal:8000" and full["search_configured"] is True


# --------------------------------------------------------------------------- CORS


def preflight(client, origin=ORIGIN):
    return client.options(
        "/v1/reviews",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def test_cors_is_off_unless_configured(tmp_path):
    client, _ = make_client(tmp_path)
    resp = client.get("/healthz", headers={"Origin": ORIGIN})
    assert "access-control-allow-origin" not in resp.headers


def test_preflight_succeeds_without_credentials_for_an_allowed_origin(tmp_path):
    client, _ = secured(tmp_path, cors_allow_origins=ORIGIN + "/, https://other.example.com")
    resp = preflight(client)
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ORIGIN
    assert "authorization" in resp.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in resp.headers


def test_disallowed_origins_get_no_cors_headers(tmp_path):
    client, _ = secured(tmp_path, cors_allow_origins=ORIGIN)
    resp = preflight(client, origin="https://evil.example.com")
    assert "access-control-allow-origin" not in resp.headers


def test_a_401_still_carries_cors_headers_so_the_browser_client_can_read_it(tmp_path):
    client, _ = secured(tmp_path, cors_allow_origins=ORIGIN)
    resp = client.get("/v1/vaults", headers={"Origin": ORIGIN})
    assert resp.status_code == 401
    assert resp.headers["access-control-allow-origin"] == ORIGIN


# --------------------------------------------------------------------------- review cap


def test_limiter_unit_behaviour():
    clock = Clock()
    limiter = HourlyLimiter(2, window=100, clock=clock)
    assert limiter.try_acquire() is None
    clock.now += 10
    assert limiter.try_acquire() is None
    assert limiter.try_acquire() == pytest.approx(90)  # first slot frees in 90s
    clock.now += 91
    assert limiter.try_acquire() is None  # window slid past the first review
    assert HourlyLimiter(0).try_acquire() is None  # 0 = unlimited


def test_review_cap_returns_429_with_retry_after_and_recovers(tmp_path):
    clock = Clock()
    client, llm = make_client(tmp_path, limiter=HourlyLimiter(2, clock=clock))
    assert post(client).status_code == 200
    assert post(client).status_code == 200
    blocked = post(client)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) == 3600
    calls = len(llm.calls)
    assert post(client).status_code == 429 and len(llm.calls) == calls  # model not called
    clock.now += 3601
    assert post(client).status_code == 200


def test_rejected_uploads_do_not_use_up_the_allowance(tmp_path):
    client, _ = make_client(tmp_path, limiter=HourlyLimiter(1, clock=Clock()))
    empty = client.post("/v1/reviews", data={"vault_id": "demo"}, files={"file": ("a.txt", b"   ")})
    assert empty.status_code == 400
    assert (
        client.post(
            "/v1/reviews", data={"vault_id": "nope"}, files={"file": ("a.txt", DOC)}
        ).status_code
        == 404
    )
    assert post(client).status_code == 200  # the single allowed review is still available


def test_cap_comes_from_settings(tmp_path):
    client, _ = make_client(tmp_path, max_reviews_per_hour=1)
    assert post(client).status_code == 200
    assert post(client).status_code == 429
