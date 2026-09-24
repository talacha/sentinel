"""Users and passwords: hashing, the store, and the admin Users API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sentinel import users as users_module
from sentinel.api import create_app
from sentinel.config import Settings
from sentinel.engine import Engine
from sentinel.llm import FakeLLM
from sentinel.users import (
    PasswordPolicyError,
    UserStore,
    generate_password,
    hash_password,
    validate_password,
    verify_password,
)
from sentinel.vault import VaultRegistry

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ("admin", "admin-pass-9f3k2-long-enough")
JUDGE = ("judge", "judge-pass-77x1-long-enough")
NEW_PASSWORD = "a-brand-new-password-4471"
HEADER = {"X-Sentinel-Admin": "1"}


# --------------------------------------------------------------------------- hashing


def test_hash_and_verify_round_trip():
    stored = hash_password("correct-horse-battery")
    assert stored.startswith("scrypt$")
    assert verify_password("correct-horse-battery", stored)
    assert not verify_password("wrong-horse-battery", stored)
    assert "correct-horse-battery" not in stored


def test_hashes_are_salted_and_unicode_safe():
    assert hash_password("same-password-here") != hash_password("same-password-here")
    assert verify_password("pässwörd-长-🔑-ok", hash_password("pässwörd-长-🔑-ok"))


@pytest.mark.parametrize(
    "bad", ["", "plaintext", "scrypt$x$y$z$a$b", "md5$1$1$1$AA==$AA==", "scrypt$16384$8$1$!!$!!"]
)
def test_malformed_hashes_never_verify(bad):
    assert verify_password("anything", bad) is False


@pytest.mark.real_scrypt
def test_production_hashing_cost_is_not_weakened():
    assert users_module._SCRYPT_N == 2**14
    assert hash_password("x" * 12).startswith("scrypt$16384$8$1$")


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short", "at least 12"),
        ("password", "at least 12"),
        ("x" * 200, "at most"),
        (" leading-space-ok-length", "whitespace"),
        ("Administrator123", None),
    ],
)
def test_password_policy(password, problem):
    if problem is None:
        validate_password("someone", password)
        return
    with pytest.raises(PasswordPolicyError, match=problem):
        validate_password("someone", password)


def test_password_may_not_be_the_username_or_a_known_weak_one():
    with pytest.raises(PasswordPolicyError, match="easy to guess"):
        validate_password("sentinel-admin", "SENTINEL-ADMIN")
    assert "sentinel" in users_module.WEAK_PASSWORDS


def test_generated_passwords_are_strong_and_unique():
    first, second = generate_password(), generate_password()
    assert first != second and len(first) >= 20
    validate_password("anyone", first)


# --------------------------------------------------------------------------- the store


def seeded(tmp_path, **env) -> tuple[UserStore, Path]:
    path = tmp_path / "state" / "users.json"
    store = UserStore(path)
    values = {
        "admin_user": "admin",
        "admin_password": ADMIN[1],
        "visitor_user": "judge",
        "visitor_password": JUDGE[1],
    }
    values.update(env)
    store.sync_env(**values)
    return store, path


def test_environment_users_are_created_admin_first_and_persisted_privately(tmp_path):
    store, path = seeded(tmp_path)
    listed = store.list()
    assert [(u["username"], u["role"]) for u in listed] == [
        ("admin", "admin"),
        ("judge", "visitor"),
    ]
    assert all(u["source"] == "environment" and u["bootstrap"] for u in listed)
    assert path.stat().st_mode & 0o777 == 0o600
    assert UserStore(path).authenticate("judge", JUDGE[1]) is not None  # survives a restart


def test_listing_never_exposes_hashes_or_passwords(tmp_path):
    store, _ = seeded(tmp_path)
    text = json.dumps(store.list())
    assert "scrypt" not in text and "hash" not in text
    assert ADMIN[1] not in text and JUDGE[1] not in text


def test_the_file_holds_hashes_only(tmp_path):
    _, path = seeded(tmp_path)
    raw = path.read_text()
    assert ADMIN[1] not in raw and JUDGE[1] not in raw and "scrypt$" in raw


def test_environment_users_come_first_even_when_other_users_are_older(tmp_path):
    path = tmp_path / "users.json"
    store = UserStore(path)
    extra = users_module.User(
        "aaa-early",
        "visitor",
        hash_password(NEW_PASSWORD),
        "console",
        None,
        "2020-01-01",
        "2020-01-01",
    )
    store._users["aaa-early"] = extra
    store.sync_env(
        admin_user="root", admin_password=ADMIN[1], visitor_user="guest", visitor_password=JUDGE[1]
    )
    assert [u["username"] for u in store.list()] == ["root", "guest", "aaa-early"]


def test_login_is_required_exactly_when_a_visitor_exists(tmp_path):
    only_admin, _ = seeded(tmp_path, visitor_password=None)
    assert only_admin.login_required() is False
    with_visitor, _ = seeded(tmp_path / "b")
    assert with_visitor.login_required() is True


def test_authentication_accepts_the_right_password_only(tmp_path):
    store, _ = seeded(tmp_path)
    assert store.authenticate("judge", JUDGE[1]).role == "visitor"
    assert store.authenticate("admin", ADMIN[1]).role == "admin"
    assert store.authenticate("judge", ADMIN[1]) is None
    assert store.authenticate("judge", "") is None
    assert store.authenticate("nobody", JUDGE[1]) is None
    assert store.authenticate("", "") is None


def test_unknown_users_cost_the_same_hashing_work(tmp_path, monkeypatch):
    store, _ = seeded(tmp_path)
    calls = []
    real = users_module.verify_password
    monkeypatch.setattr(users_module, "verify_password", lambda p, h: calls.append(1) or real(p, h))
    store.authenticate("nobody", "whatever-password")
    assert calls, "an unknown username must still do a hash comparison (no timing oracle)"


def test_repeat_logins_are_cached_but_a_reset_takes_effect_immediately(tmp_path, monkeypatch):
    store, _ = seeded(tmp_path)
    calls = []
    real = users_module.verify_password
    monkeypatch.setattr(users_module, "verify_password", lambda p, h: calls.append(1) or real(p, h))
    for _ in range(5):
        assert store.authenticate("judge", JUDGE[1]) is not None
    assert len(calls) == 1  # hashed once, then served from the short-lived cache

    store.reset_password("judge", NEW_PASSWORD)
    assert store.authenticate("judge", JUDGE[1]) is None  # the cache cannot keep the old one alive
    assert store.authenticate("judge", NEW_PASSWORD) is not None


def test_reset_marks_the_password_as_console_managed(tmp_path):
    store, _ = seeded(tmp_path)
    before = store.list()[1]["password_changed_at"]
    store.reset_password("judge", NEW_PASSWORD)
    judge = store.list()[1]
    assert judge["source"] == "console" and judge["password_changed_at"] >= before


def test_a_rejected_reset_changes_nothing(tmp_path):
    store, _ = seeded(tmp_path)
    with pytest.raises(PasswordPolicyError):
        store.reset_password("judge", "short")
    with pytest.raises(KeyError):
        store.reset_password("nobody", NEW_PASSWORD)
    assert store.authenticate("judge", JUDGE[1]) is not None
    assert store.list()[1]["source"] == "environment"


def test_a_rotated_environment_password_is_followed_until_the_console_takes_over(tmp_path):
    store, path = seeded(tmp_path)
    rotated = "rotated-in-the-env-password-1"
    store.sync_env(
        admin_user="admin", admin_password=ADMIN[1], visitor_user="judge", visitor_password=rotated
    )
    assert store.authenticate("judge", JUDGE[1]) is None
    assert store.authenticate("judge", rotated) is not None

    store.reset_password("judge", NEW_PASSWORD)  # from now on the console's password wins
    store.sync_env(
        admin_user="admin",
        admin_password=ADMIN[1],
        visitor_user="judge",
        visitor_password="env-changed-again-2222",
    )
    assert store.authenticate("judge", NEW_PASSWORD) is not None
    assert store.authenticate("judge", "env-changed-again-2222") is None
    assert UserStore(path).authenticate("judge", NEW_PASSWORD) is not None


def test_an_unset_environment_password_removes_only_environment_defined_users(tmp_path):
    store, _ = seeded(tmp_path)
    store.sync_env(
        admin_user="admin", admin_password=ADMIN[1], visitor_user="judge", visitor_password=None
    )
    assert [u["username"] for u in store.list()] == ["admin"]
    assert store.login_required() is False

    store, _ = seeded(tmp_path / "console")
    store.reset_password("judge", NEW_PASSWORD)
    store.sync_env(
        admin_user="admin", admin_password=ADMIN[1], visitor_user="judge", visitor_password=None
    )
    assert "judge" in [u["username"] for u in store.list()]  # a console-managed user stays


def test_renaming_the_environment_user_keeps_the_entry_and_password(tmp_path):
    store, _ = seeded(tmp_path)
    store.sync_env(
        admin_user="root", admin_password=ADMIN[1], visitor_user="judge", visitor_password=JUDGE[1]
    )
    assert [u["username"] for u in store.list()] == ["root", "judge"]
    assert store.authenticate("root", ADMIN[1]) is not None
    assert store.authenticate("admin", ADMIN[1]) is None


def test_a_visitor_cannot_share_the_admin_username(tmp_path):
    store, _ = seeded(tmp_path, visitor_user="admin")
    assert [(u["username"], u["role"]) for u in store.list()] == [("admin", "admin")]


def test_a_corrupt_users_file_is_ignored_and_reseeded_from_the_environment(tmp_path):
    path = tmp_path / "users.json"
    path.write_text("{not json")
    store = UserStore(path)
    assert "unreadable" in (store.load_error or "")
    store.sync_env(
        admin_user="admin", admin_password=ADMIN[1], visitor_user="judge", visitor_password=JUDGE[1]
    )
    assert store.authenticate("admin", ADMIN[1]) is not None


# --------------------------------------------------------------------------- the API


VAULT = {
    "id": "demo",
    "title": "Demo",
    "rules": [{"id": "signed", "title": "Signed", "criterion": "Signed.", "on_fail": "no_cumple"}],
}


def make_app(tmp_path, *, ui=False, **settings):
    vaults = tmp_path / "vaults"
    vaults.mkdir(parents=True, exist_ok=True)
    (vaults / "demo.yaml").write_text(yaml.safe_dump(VAULT))
    values = {
        "vaults_dir": vaults,
        "audit_log_path": tmp_path / "state" / "audit.jsonl",
        "admin_user": ADMIN[0],
        "admin_password": ADMIN[1],
        "access_user": JUDGE[0],
        "access_password": JUDGE[1],
    }
    values.update(settings)
    cfg = Settings(_env_file=None, **values)
    llm = FakeLLM(
        lambda s, u, schema, r: {"verdict": "cumple", "rationale": "ok", "evidence": ["x"]}
    )
    return create_app(
        cfg,
        Engine(llm),
        VaultRegistry(vaults),
        ui_dir=ROOT / "ui" if ui else None,
        client_dir=None,
        preflight_deps={"llm": llm},
    ), cfg


def make(tmp_path, **kw):
    app, cfg = make_app(tmp_path, **kw)
    return TestClient(app), cfg


def reset(client, username, body, **kwargs):
    kwargs.setdefault("auth", ADMIN)
    kwargs.setdefault("headers", HEADER)
    return client.post(f"/v1/admin/users/{username}/password", json=body, **kwargs)


def test_only_an_admin_can_list_users_and_admin_and_judge_come_first(tmp_path):
    client, _ = make(tmp_path)
    assert client.get("/v1/admin/users").status_code == 401
    assert client.get("/v1/admin/users", auth=JUDGE).status_code == 401  # a visitor is not an admin
    body = client.get("/v1/admin/users", auth=ADMIN).json()
    assert [(u["username"], u["role"]) for u in body["users"]] == [
        ("admin", "admin"),
        ("judge", "visitor"),
    ]
    assert body["min_password_length"] == 12


def test_the_users_api_never_returns_hashes_or_passwords(tmp_path):
    client, _ = make(tmp_path)
    text = client.get("/v1/admin/users", auth=ADMIN).text
    for secret in (ADMIN[1], JUDGE[1], "scrypt"):
        assert secret not in text


def test_the_users_api_is_off_without_an_admin_password(tmp_path):
    client, _ = make(tmp_path, admin_password=None)
    resp = client.get("/v1/admin/users", auth=ADMIN)
    assert resp.status_code == 403 and "ADMIN_PASSWORD" in resp.json()["detail"]


def test_resetting_a_password_replaces_it_everywhere_immediately(tmp_path):
    client, cfg = make(tmp_path)
    assert client.get("/v1/vaults", auth=JUDGE).status_code == 200  # warms the login cache
    resp = reset(client, "judge", {"password": NEW_PASSWORD})
    assert resp.status_code == 200 and resp.json() == {"username": "judge", "generated": False}
    assert client.get("/v1/vaults", auth=("judge", JUDGE[1])).status_code == 401
    assert client.get("/v1/vaults", auth=("judge", NEW_PASSWORD)).status_code == 200
    judge = client.get("/v1/admin/users", auth=ADMIN).json()["users"][1]
    assert judge["source"] == "console"

    stored = (cfg.audit_log_path.parent / "users.json").read_text()
    assert NEW_PASSWORD not in stored and JUDGE[1] not in stored  # hashes only, never plaintext


def test_a_generated_password_is_returned_once_and_works(tmp_path):
    client, cfg = make(tmp_path)
    resp = reset(client, "judge", {"generate": True})
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    password = resp.json()["password"]
    assert len(password) >= 20
    assert client.get("/v1/vaults", auth=("judge", password)).status_code == 200
    assert password not in client.get("/v1/admin/users", auth=ADMIN).text  # never shown again
    assert password not in (cfg.audit_log_path.parent / "users.json").read_text()


def test_the_audit_log_records_who_reset_whom_but_never_the_password(tmp_path):
    client, cfg = make(tmp_path)
    chosen = reset(client, "judge", {"password": NEW_PASSWORD})
    generated = reset(client, "judge", {"generate": True}).json()["password"]
    assert chosen.status_code == 200
    audit = cfg.audit_log_path.read_text()
    assert NEW_PASSWORD not in audit and generated not in audit
    events = [json.loads(line) for line in audit.splitlines()]
    assert [(e["event"], e["admin"], e["username"], e["generated"]) for e in events] == [
        ("user_password_reset", "admin", "judge", False),
        ("user_password_reset", "admin", "judge", True),
    ]


@pytest.mark.parametrize(
    ("body", "status", "field"),
    [
        ({"password": "short"}, 422, "password"),
        ({"password": "judge"}, 422, "password"),
        ({"password": "  padded-password-123  "}, 422, "password"),
        ({"password": NEW_PASSWORD, "generate": True}, 422, "password"),
        ({}, 422, "password"),
        ({"generate": False}, 422, "password"),
    ],
)
def test_bad_reset_requests_are_rejected_and_change_nothing(tmp_path, body, status, field):
    client, _ = make(tmp_path)
    resp = reset(client, "judge", body)
    assert resp.status_code == status and field in resp.json()["detail"]["errors"]
    assert client.get("/v1/vaults", auth=JUDGE).status_code == 200  # the old password still works


def test_resetting_an_unknown_user_is_a_404(tmp_path):
    client, _ = make(tmp_path)
    assert reset(client, "nobody", {"generate": True}).status_code == 404


def test_visitors_cannot_reset_anyone_and_writes_need_the_header_and_json(tmp_path):
    client, _ = make(tmp_path)
    assert reset(client, "judge", {"generate": True}, auth=JUDGE).status_code == 401
    assert reset(client, "judge", {"generate": True}, headers={}).status_code == 403
    form = client.post(
        "/v1/admin/users/judge/password", data={"generate": "true"}, auth=ADMIN, headers=HEADER
    )
    assert form.status_code == 415
    assert client.get("/v1/vaults", auth=JUDGE).status_code == 200  # nothing changed


def test_an_admin_can_reset_their_own_password(tmp_path):
    client, _ = make(tmp_path)
    assert reset(client, "admin", {"password": NEW_PASSWORD}).status_code == 200
    assert client.get("/v1/admin/users", auth=ADMIN).status_code == 401  # the old one is gone
    assert client.get("/v1/admin/users", auth=("admin", NEW_PASSWORD)).status_code == 200


def test_a_console_reset_survives_a_restart_and_beats_the_environment(tmp_path):
    client, _ = make(tmp_path)
    reset(client, "judge", {"password": NEW_PASSWORD})
    restarted, _ = make(tmp_path)  # same state directory, environment unchanged
    assert restarted.get("/v1/vaults", auth=("judge", NEW_PASSWORD)).status_code == 200
    assert restarted.get("/v1/vaults", auth=("judge", JUDGE[1])).status_code == 401


def test_rotating_the_environment_password_takes_effect_on_restart_until_reset(tmp_path):
    make(tmp_path)
    rotated, _ = make(tmp_path, access_password="rotated-env-password-8888")
    assert rotated.get("/v1/vaults", auth=("judge", "rotated-env-password-8888")).status_code == 200
    assert rotated.get("/v1/vaults", auth=("judge", JUDGE[1])).status_code == 401


def test_healthz_reports_whether_a_login_is_required(tmp_path):
    with_login, _ = make(tmp_path)
    assert with_login.get("/healthz").json()["auth_required"] is True
    open_site, _ = make(tmp_path / "open", access_password=None)
    assert open_site.get("/healthz").json()["auth_required"] is False
    assert open_site.get("/v1/vaults").status_code == 200  # no visitor exists, so no login


def test_the_status_page_summarizes_the_users(tmp_path):
    client, _ = make(tmp_path)
    payload = client.get("/v1/admin/status", auth=ADMIN).json()
    users = next(c for c in payload["checks"] if c["name"] == "users")
    assert users["group"] == "Access" and users["status"] == "pass"
    assert "2 user(s): 1 admin, 1 visitor" in users["detail"] and "required" in users["detail"]


def test_logins_are_no_longer_a_runtime_setting_because_the_users_page_owns_them(tmp_path):
    client, _ = make(tmp_path)
    names = {f["name"] for f in client.get("/v1/admin/config", auth=ADMIN).json()["fields"]}
    assert "access_password" not in names and "access_user" not in names
    resp = client.put(
        "/v1/admin/config",
        json={"set": {"access_password": NEW_PASSWORD}},
        auth=ADMIN,
        headers=HEADER,
    )
    assert resp.status_code == 422 and "access_password" in resp.json()["detail"]["errors"]


def test_the_users_page_is_a_data_free_shell(tmp_path):
    client, _ = make(tmp_path, ui=True)
    page = client.get("/admin/user")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "no-store"
    for secret in (ADMIN[1], JUDGE[1]):
        assert secret not in page.text


def test_repeated_wrong_admin_passwords_are_throttled_before_any_hashing(tmp_path, monkeypatch):
    client, _ = make(tmp_path)
    for _ in range(10):
        assert client.get("/v1/admin/users", auth=("admin", "wrong-guess")).status_code == 401
    calls = []
    real = users_module.verify_password
    monkeypatch.setattr(users_module, "verify_password", lambda p, h: calls.append(1) or real(p, h))
    blocked = client.get("/v1/admin/users", auth=("admin", "another-wrong-guess"))
    assert blocked.status_code == 429 and calls == []  # refused without spending CPU on the guess
