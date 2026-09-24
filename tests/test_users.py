"""Users and passwords: hashing, the store, adding and editing users, and the admin API."""

from __future__ import annotations

import json
import re
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
    UserError,
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
    page = client.get("/admin/users")
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


# --------------------------------------------------------------------------- add and edit (store)

OPS_PASSWORD = "ops-people-password-8821"


def test_create_stores_a_hashed_console_user_who_can_sign_in(tmp_path):
    store, path = seeded(tmp_path)
    store.create("ops.team", "visitor", OPS_PASSWORD)
    user = store.get("ops.team")
    assert user.source == "console" and user.bootstrap is None and user.role == "visitor"
    assert OPS_PASSWORD not in path.read_text() and "scrypt$" in path.read_text()
    assert UserStore(path).authenticate("ops.team", OPS_PASSWORD) is not None  # survives a restart
    assert store.authenticate("ops.team", "wrong-password-here") is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "has space",
        "has:colon",
        "-leading",
        ".dot",
        "x" * 65,
        "ünï",
        "a/b",
        "tab\t",
        "ops\n",
    ],
)
def test_create_refuses_bad_usernames_and_stores_nothing(tmp_path, name):
    store, path = seeded(tmp_path)
    before = path.read_text()
    with pytest.raises(UserError) as exc:
        store.create(name, "visitor", OPS_PASSWORD)
    assert exc.value.field == "username" and not exc.value.conflict
    assert path.read_text() == before


@pytest.mark.parametrize("name", ["a", "ops", "ops.team-2", "first_last", "person@example.com"])
def test_create_accepts_reasonable_usernames(tmp_path, name):
    store, _ = seeded(tmp_path)
    store.create(name, "visitor", OPS_PASSWORD)
    assert store.get(name) is not None


def test_create_refuses_a_duplicate_ignoring_case(tmp_path):
    store, _ = seeded(tmp_path)
    for name in ("judge", "JUDGE", "Admin"):
        with pytest.raises(UserError) as exc:
            store.create(name, "visitor", OPS_PASSWORD)
        assert exc.value.conflict and exc.value.field == "username"


def test_create_checks_role_and_password_and_changes_nothing_on_failure(tmp_path):
    store, path = seeded(tmp_path)
    before = path.read_text()
    with pytest.raises(UserError) as role:
        store.create("ops", "superuser", OPS_PASSWORD)
    assert role.value.field == "role"
    for bad in ("short", "ops", "  padded-password-123  "):
        with pytest.raises(PasswordPolicyError):
            store.create("ops", "visitor", bad)
    assert store.get("ops") is None and path.read_text() == before


def test_the_number_of_users_is_capped(tmp_path, monkeypatch):
    store, _ = seeded(tmp_path)
    monkeypatch.setattr(users_module, "MAX_USERS", 3)
    store.create("third", "visitor", OPS_PASSWORD)
    with pytest.raises(UserError, match="at most 3"):
        store.create("fourth", "visitor", OPS_PASSWORD)


def test_an_environment_user_never_overwrites_a_console_user_with_the_same_name(tmp_path):
    store, path = seeded(tmp_path, visitor_password=None)  # no environment visitor yet
    store.create("judge", "visitor", OPS_PASSWORD)
    store.sync_env(  # the environment now defines a visitor with that name
        admin_user="admin", admin_password=ADMIN[1], visitor_user="judge", visitor_password=JUDGE[1]
    )
    assert store.authenticate("judge", OPS_PASSWORD) is not None
    assert store.authenticate("judge", JUDGE[1]) is None
    assert store.get("judge").source == "console"


def test_update_changes_the_role_and_password_together(tmp_path):
    store, _ = seeded(tmp_path)
    store.create("ops", "visitor", OPS_PASSWORD)
    change = store.update("ops", role="admin", password=NEW_PASSWORD, actor="admin")
    assert (change.role_from, change.role_to, change.password_changed) == ("visitor", "admin", True)
    user = store.authenticate("ops", NEW_PASSWORD)
    assert user is not None and user.role == "admin"
    assert store.authenticate("ops", OPS_PASSWORD) is None  # the old password stops at once


def test_update_is_all_or_nothing(tmp_path):
    store, path = seeded(tmp_path)
    store.create("ops", "visitor", OPS_PASSWORD)
    before = path.read_text()
    with pytest.raises(PasswordPolicyError):
        store.update("ops", role="admin", password="short", actor="admin")
    assert store.get("ops").role == "visitor" and path.read_text() == before  # role not applied
    with pytest.raises(UserError):
        store.update("ops", role="admin", password=NEW_PASSWORD, actor="ops")  # self role change
    assert store.authenticate("ops", OPS_PASSWORD) is not None  # password not applied


def test_update_needs_something_to_change_and_a_known_user_and_a_valid_role(tmp_path):
    store, _ = seeded(tmp_path)
    store.create("ops", "visitor", OPS_PASSWORD)
    with pytest.raises(UserError, match="nothing to change"):
        store.update("ops", role="visitor", actor="admin")  # the role it already has
    with pytest.raises(KeyError):
        store.update("nobody", password=NEW_PASSWORD)
    with pytest.raises(UserError) as exc:
        store.update("ops", role="root", actor="admin")
    assert exc.value.field == "role"


def test_nobody_can_change_their_own_role(tmp_path):
    store, _ = seeded(tmp_path)
    store.create("ops", "admin", OPS_PASSWORD)
    with pytest.raises(UserError, match="your own role"):
        store.update("ops", role="visitor", actor="ops")
    assert store.update("ops", role="visitor", actor="admin").role_to == "visitor"


def test_the_environment_defined_users_keep_their_roles(tmp_path):
    store, _ = seeded(tmp_path)
    store.create("ops", "visitor", OPS_PASSWORD)  # so neither is the only one of its kind
    store.create("boss", "admin", OPS_PASSWORD)
    for name, role in (("admin", "visitor"), ("judge", "admin")):
        with pytest.raises(UserError, match="environment"):
            store.update(name, role=role, actor="boss")


def test_the_only_visitor_and_the_only_admin_keep_their_roles(tmp_path):
    store, _ = seeded(tmp_path, visitor_password=None, admin_password=None)
    store.create("solo-visitor", "visitor", OPS_PASSWORD)
    store.create("solo-admin", "admin", OPS_PASSWORD)
    with pytest.raises(UserError, match="visitor login off"):
        store.update("solo-visitor", role="admin", actor="solo-admin")
    with pytest.raises(UserError, match="only admin"):
        store.update("solo-admin", role="visitor", actor="someone-else")
    assert store.login_required()  # the visitor login is still on
    store.create("second-visitor", "visitor", OPS_PASSWORD)  # now the first is not the only one
    assert store.update("solo-visitor", role="admin", actor="solo-admin").role_to == "admin"


def test_the_list_says_why_a_role_cannot_be_changed(tmp_path):
    store, _ = seeded(tmp_path)
    store.create("ops", "visitor", OPS_PASSWORD)
    locks = {u["username"]: u["role_locked"] for u in store.list()}
    assert "environment" in locks["admin"] and "environment" in locks["judge"]
    assert locks["ops"] is None


# --------------------------------------------------------------------------- add and edit (API)


def create(client, body, **kwargs):
    kwargs.setdefault("auth", ADMIN)
    kwargs.setdefault("headers", HEADER)
    return client.post("/v1/admin/users", json=body, **kwargs)


def edit(client, username, body, **kwargs):
    kwargs.setdefault("auth", ADMIN)
    kwargs.setdefault("headers", HEADER)
    return client.put(f"/v1/admin/users/{username}", json=body, **kwargs)


def test_only_an_admin_with_the_header_and_json_can_add_or_edit_users(tmp_path):
    client, _ = make(tmp_path)
    body = {"username": "ops", "role": "visitor", "password": OPS_PASSWORD}
    assert create(client, body, auth=None).status_code == 401
    assert create(client, body, auth=JUDGE).status_code == 401  # a visitor is not an admin
    assert create(client, body, headers={}).status_code == 403
    form = client.post("/v1/admin/users", data=body, auth=ADMIN, headers=HEADER)
    assert form.status_code == 415
    assert edit(client, "judge", {"generate": True}, auth=JUDGE).status_code == 401
    assert edit(client, "judge", {"generate": True}, headers={}).status_code == 403
    names = [u["username"] for u in client.get("/v1/admin/users", auth=ADMIN).json()["users"]]
    assert names == ["admin", "judge"]  # nothing was added


def test_adding_a_visitor_with_a_chosen_password(tmp_path):
    client, cfg = make(tmp_path)
    resp = create(client, {"username": "ops", "role": "visitor", "password": OPS_PASSWORD})
    assert resp.status_code == 201 and resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["user"]["username"] == "ops" and body["user"]["role"] == "visitor"
    assert body["user"]["source"] == "console" and body["generated"] is False
    assert "password" not in body and OPS_PASSWORD not in resp.text
    assert client.get("/v1/vaults", auth=("ops", OPS_PASSWORD)).status_code == 200
    assert client.get("/v1/admin/users", auth=("ops", OPS_PASSWORD)).status_code == 401
    assert OPS_PASSWORD not in (cfg.audit_log_path.parent / "users.json").read_text()


def test_adding_a_user_with_a_generated_password_shows_it_once(tmp_path):
    client, cfg = make(tmp_path)
    resp = create(client, {"username": "ops", "role": "visitor", "generate": True})
    password = resp.json()["password"]
    assert resp.status_code == 201 and len(password) >= 20 and resp.json()["generated"] is True
    assert client.get("/v1/vaults", auth=("ops", password)).status_code == 200
    assert password not in client.get("/v1/admin/users", auth=ADMIN).text  # never shown again
    assert password not in (cfg.audit_log_path.parent / "users.json").read_text()
    assert password not in cfg.audit_log_path.read_text()


def test_a_new_admin_can_use_the_console_and_a_new_visitor_cannot(tmp_path):
    client, _ = make(tmp_path)
    create(client, {"username": "boss", "role": "admin", "password": OPS_PASSWORD})
    create(client, {"username": "guest", "role": "visitor", "password": NEW_PASSWORD})
    assert client.get("/v1/admin/users", auth=("boss", OPS_PASSWORD)).status_code == 200
    assert client.get("/v1/admin/users", auth=("guest", NEW_PASSWORD)).status_code == 401
    listed = client.get("/v1/admin/users", auth=ADMIN).json()["users"]
    assert [u["username"] for u in listed] == ["admin", "judge", "boss", "guest"]


@pytest.mark.parametrize(
    ("body", "status", "field"),
    [
        ({"username": "ops", "role": "visitor"}, 422, "password"),  # neither
        (
            {"username": "ops", "password": OPS_PASSWORD, "generate": True},
            422,
            "password",
        ),  # both
        ({"username": "ops", "password": "short"}, 422, "password"),
        ({"username": "ops", "password": "ops"}, 422, "password"),
        ({"username": "bad name", "password": OPS_PASSWORD}, 422, "username"),
        ({"username": "has:colon", "password": OPS_PASSWORD}, 422, "username"),
        ({"username": "JUDGE", "password": OPS_PASSWORD}, 409, "username"),  # case-insensitive
        ({"username": "admin", "password": OPS_PASSWORD}, 409, "username"),
    ],
)
def test_bad_new_users_are_rejected_with_the_field_to_blame(tmp_path, body, status, field):
    client, _ = make(tmp_path)
    resp = create(client, body)
    assert resp.status_code == status and field in resp.json()["detail"]["errors"]
    names = [u["username"] for u in client.get("/v1/admin/users", auth=ADMIN).json()["users"]]
    assert names == ["admin", "judge"]  # nothing was added


def test_an_unknown_role_is_refused(tmp_path):
    client, _ = make(tmp_path)
    resp = create(client, {"username": "ops", "role": "root", "password": OPS_PASSWORD})
    assert resp.status_code == 422
    assert edit(client, "judge", {"role": "root"}).status_code == 422


def test_adding_and_editing_are_audited_without_passwords(tmp_path):
    client, cfg = make(tmp_path)
    create(client, {"username": "ops", "role": "visitor", "password": OPS_PASSWORD})
    create(client, {"username": "two", "role": "visitor", "generate": True})
    edit(client, "ops", {"role": "admin", "password": NEW_PASSWORD})
    audit = cfg.audit_log_path.read_text()
    assert OPS_PASSWORD not in audit and NEW_PASSWORD not in audit
    events = [json.loads(line) for line in audit.splitlines()]
    assert [(e["event"], e["username"]) for e in events] == [
        ("user_created", "ops"),
        ("user_created", "two"),
        ("user_updated", "ops"),
    ]
    assert (events[0]["admin"], events[0]["role"], events[0]["generated"]) == (
        "admin",
        "visitor",
        False,
    )
    assert events[1]["generated"] is True
    assert (events[2]["role_from"], events[2]["role_to"], events[2]["password_reset"]) == (
        "visitor",
        "admin",
        True,
    )


def test_editing_the_role_takes_effect_immediately(tmp_path):
    client, _ = make(tmp_path)
    create(client, {"username": "ops", "role": "visitor", "password": OPS_PASSWORD})
    creds = ("ops", OPS_PASSWORD)
    assert client.get("/v1/admin/users", auth=creds).status_code == 401
    resp = edit(client, "ops", {"role": "admin"})
    assert resp.status_code == 200 and resp.json()["user"]["role"] == "admin"
    assert client.get("/v1/admin/users", auth=creds).status_code == 200
    edit(client, "ops", {"role": "visitor"})
    assert client.get("/v1/admin/users", auth=creds).status_code == 401
    assert client.get("/v1/vaults", auth=creds).status_code == 200


def test_editing_a_password_replaces_it_and_can_generate_one(tmp_path):
    client, _ = make(tmp_path)
    create(client, {"username": "ops", "role": "visitor", "password": OPS_PASSWORD})
    assert edit(client, "ops", {"password": NEW_PASSWORD}).status_code == 200
    assert client.get("/v1/vaults", auth=("ops", OPS_PASSWORD)).status_code == 401
    assert client.get("/v1/vaults", auth=("ops", NEW_PASSWORD)).status_code == 200
    generated = edit(client, "ops", {"generate": True}).json()
    assert client.get("/v1/vaults", auth=("ops", generated["password"])).status_code == 200


def test_a_bad_edit_changes_nothing(tmp_path):
    client, _ = make(tmp_path)
    create(client, {"username": "ops", "role": "visitor", "password": OPS_PASSWORD})
    resp = edit(client, "ops", {"role": "admin", "password": "short"})
    assert resp.status_code == 422 and "password" in resp.json()["detail"]["errors"]
    listed = {u["username"]: u for u in client.get("/v1/admin/users", auth=ADMIN).json()["users"]}
    assert listed["ops"]["role"] == "visitor"  # the valid role change was not applied either
    assert client.get("/v1/vaults", auth=("ops", OPS_PASSWORD)).status_code == 200
    both = edit(client, "ops", {"password": NEW_PASSWORD, "generate": True})
    assert both.status_code == 422 and "password" in both.json()["detail"]["errors"]
    assert edit(client, "ops", {}).status_code == 422  # nothing to change
    assert edit(client, "nobody", {"generate": True}).status_code == 404


def test_role_rules_are_enforced_by_the_api(tmp_path):
    client, _ = make(tmp_path)
    create(client, {"username": "boss", "role": "admin", "password": OPS_PASSWORD})
    boss = ("boss", OPS_PASSWORD)
    own = edit(client, "boss", {"role": "visitor"}, auth=boss)
    assert own.status_code == 422 and "your own role" in own.json()["detail"]["errors"]["role"]
    for name, role in (("admin", "visitor"), ("judge", "admin")):
        resp = edit(client, name, {"role": role}, auth=boss)  # boss is not the one being changed
        assert resp.status_code == 422 and "environment" in resp.json()["detail"]["errors"]["role"]
    listed = {u["username"]: u for u in client.get("/v1/admin/users", auth=ADMIN).json()["users"]}
    assert (listed["admin"]["role"], listed["judge"]["role"], listed["boss"]["role"]) == (
        "admin",
        "visitor",
        "admin",
    )
    assert (
        edit(client, "boss", {"password": NEW_PASSWORD}, auth=boss).status_code == 200
    )  # own pw ok


def test_the_user_list_carries_the_role_lock_reasons(tmp_path):
    client, _ = make(tmp_path)
    create(client, {"username": "ops", "role": "visitor", "password": OPS_PASSWORD})
    listed = {u["username"]: u for u in client.get("/v1/admin/users", auth=ADMIN).json()["users"]}
    assert "environment" in listed["admin"]["role_locked"] and listed["ops"]["role_locked"] is None


def test_the_status_page_counts_added_users(tmp_path):
    client, _ = make(tmp_path)
    create(client, {"username": "ops", "role": "visitor", "password": OPS_PASSWORD})
    create(client, {"username": "boss", "role": "admin", "password": NEW_PASSWORD})
    checks = {c["name"]: c for c in client.get("/v1/admin/status", auth=ADMIN).json()["checks"]}
    assert "4 user(s): 2 admin, 2 visitor" in checks["users"]["detail"]


# --------------------------------------------------------------------------- pages and old URLs


@pytest.mark.parametrize(
    "path",
    ["/status", "/admin", "/admin/config", "/admin/users", "/admin/vaults", "/admin/vaults/demo"],
)
def test_every_console_page_is_a_data_free_shell_even_behind_a_visitor_login(tmp_path, path):
    client, _ = make(tmp_path, ui=True)
    page = client.get(path)
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "no-store"
    for secret in (ADMIN[1], JUDGE[1]):
        assert secret not in page.text
    assert client.get(path + "/").status_code in (200, 307)  # a trailing slash is not a 401


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/admin/user", "/admin/users"),
        ("/admin/vault", "/admin/vaults"),
        ("/admin/vault/demo", "/admin/vaults/demo"),
        ("/admin/vault/demo/edit", "/admin/vaults/demo/edit"),
        ("/admin/vault/demo?version=1", "/admin/vaults/demo?version=1"),
    ],
)
def test_the_earlier_singular_urls_redirect_without_a_login(tmp_path, old, new):
    client, _ = make(tmp_path, ui=True)  # a visitor login is required for everything else
    resp = client.get(old, follow_redirects=False)
    assert resp.status_code in (307, 308) and resp.headers["location"].rstrip("/") == new
    assert client.get(old).status_code == 200  # and the new page loads


def test_a_redirect_can_only_ever_point_inside_the_console(tmp_path):
    client, _ = make(tmp_path, ui=True)
    for old in ("/admin/vault//evil.example.com", "/admin/vault/%2F%2Fevil.example.com"):
        resp = client.get(old, follow_redirects=False)
        assert resp.headers["location"].startswith("/admin/vaults/")
        assert not resp.headers["location"].startswith("//")


# --------------------------------------------------------------------------- hardening


def test_a_reset_ends_the_old_password_even_for_a_check_already_under_way(tmp_path, monkeypatch):
    store, _ = seeded(tmp_path)
    real = users_module.verify_password
    landed = []

    def verify_then_reset(password, stored):
        result = real(password, stored)  # the old password checks out against the old hash...
        if password == JUDGE[1] and not landed:
            landed.append(True)
            store.reset_password("judge", NEW_PASSWORD)  # ...and a reset lands before it is used
        return result

    monkeypatch.setattr(users_module, "verify_password", verify_then_reset)
    assert store.authenticate("judge", JUDGE[1]) is None
    assert store.authenticate("judge", JUDGE[1]) is None  # and it was not cached as valid
    assert store.authenticate("judge", NEW_PASSWORD) is not None


def test_a_user_removed_mid_check_does_not_authenticate(tmp_path, monkeypatch):
    store, _ = seeded(tmp_path)
    real = users_module.verify_password

    def verify_then_remove(password, stored):
        result = real(password, stored)
        store._users.pop("judge", None)  # the account disappears while the check runs
        return result

    monkeypatch.setattr(users_module, "verify_password", verify_then_remove)
    assert store.authenticate("judge", JUDGE[1]) is None


def test_validation_errors_never_echo_what_was_sent(tmp_path):
    client, _ = make(tmp_path)
    secret = "sk-this-secret-must-not-come-back-1234"
    missing_name = client.post(  # the whole body is the "input" of a missing-field error
        "/v1/admin/users", json={"password": secret}, auth=ADMIN, headers=HEADER
    )
    wrong_type = client.put("/v1/admin/config", json={"set": secret}, auth=ADMIN, headers=HEADER)
    for resp in (missing_name, wrong_type):
        assert resp.status_code == 422
        assert secret not in resp.text
        errors = resp.json()["detail"]
        assert errors and all(set(e) == {"type", "loc", "msg"} for e in errors)
    assert missing_name.json()["detail"][0]["loc"] == ["body", "username"]  # still says where


@pytest.mark.parametrize(
    "path",
    ["/status", "/admin", "/admin/config", "/admin/users", "/admin/vaults", "/admin/vaults/demo"],
)
def test_console_pages_carry_a_strict_content_security_policy(tmp_path, path):
    client, _ = make(tmp_path, ui=True)
    headers = client.get(path).headers
    policy = {
        part.split()[0]: part.split()[1:]
        for part in headers["content-security-policy"].split(";")
        if part.strip()
    }
    assert policy["default-src"] == ["'none'"]  # nothing is allowed unless listed below
    assert policy["connect-src"] == ["'self'"]  # so the page can only talk to its own server
    assert policy["frame-ancestors"] == ["'none'"] and policy["base-uri"] == ["'none'"]
    assert policy["form-action"] == ["'none'"] and policy["script-src"] == ["'unsafe-inline'"]
    assert "'unsafe-eval'" not in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"


def test_the_console_needs_nothing_from_any_other_origin():
    # The policy above allows only inline script and style, so any external reference would break
    # the page. Keep it self-contained.
    page = (ROOT / "ui" / "console.html").read_text()
    assert "<script src" not in page and "<link" not in page and "@import" not in page
    assert not re.search(r"(?:src|href|action)=[\"']https?:", page)
    assert "eval(" not in page and "innerHTML" not in page


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/admin/", "/admin"),
        ("/status/", "/status"),
        ("/admin/config/", "/admin/config"),
        ("/admin/users/", "/admin/users"),
        ("/admin/vaults/", "/admin/vaults"),
        ("/admin/vaults/demo/", "/admin/vaults/demo"),
        ("/admin/vaults/demo/edit/", "/admin/vaults/demo/edit"),
        ("/admin/vaults/demo/?version=2", "/admin/vaults/demo?version=2"),
        ("/admin/user/", "/admin/users"),
        ("/admin/vault/", "/admin/vaults"),
    ],
)
def test_a_trailing_slash_redirects_on_the_same_origin_in_one_hop(tmp_path, path, target):
    # Behind a TLS-terminating proxy the router's own redirect would point at http://.
    client, _ = make(tmp_path, ui=True)
    resp = client.get(path, follow_redirects=False, headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 307 and resp.headers["location"] == target
    assert client.get(path).status_code == 200  # and it lands on the page, without a login


def test_a_redirect_from_an_odd_path_cannot_split_the_header_or_leave_the_site(tmp_path):
    client, _ = make(tmp_path, ui=True)
    crlf = client.get("/admin/vaults/a%0d%0aX-Injected:%201/", follow_redirects=False)
    assert crlf.status_code == 307 and "x-injected" not in crlf.headers
    location = crlf.headers["location"]
    assert "\r" not in location and "\n" not in location  # encoded, so the header cannot be split
    assert location.startswith("/admin/vaults/") and not location.startswith("//")
    # A slash in the id is not a route at all, so there is nothing to redirect to.
    slashes = client.get("/admin/vaults/%2f%2fevil.example.com/", follow_redirects=False)
    assert slashes.status_code == 404 and "location" not in slashes.headers
