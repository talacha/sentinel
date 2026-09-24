"""Editable vaults: validation, versions, the overlay, and the admin Vaults API."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sentinel.api import create_app
from sentinel.config import Settings
from sentinel.engine import Engine
from sentinel.llm import FakeLLM
from sentinel.vault import (
    VaultConflict,
    VaultError,
    VaultRegistry,
    VaultUnavailable,
    parse_vault,
)

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ("admin", "admin-pass-9f3k2-long-enough")
VISITOR = ("judge", "judge-pass-77x1-long-enough")
HEADER = {"X-Sentinel-Admin": "1"}

DEMO = """# Demo vault: this comment must survive editing and display.
id: demo
title: Demo vault
description: Original description.
rules:
  - id: signed
    title: Signed
    criterion: The application is signed.
    on_fail: no_cumple
  - id: income
    title: Income multiple
    facts:
      - { name: sum_insured, type: number, description: "sum" }
      - { name: income, type: number, description: "income" }
    check: "sum_insured <= 15 * income"
    on_fail: revisar
"""
OTHER = """id: other
title: Other vault
rules:
  - id: r1
    title: One
    criterion: Something.
"""
EDITED = DEMO.replace("Original description.", "Edited description.").replace(
    "The application is signed.", "The application is countersigned."
)


def write_shipped(directory: Path, demo: str = DEMO) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "demo.yaml").write_text(demo)
    (directory / "other.yaml").write_text(OTHER)
    return directory


def registry(tmp_path, *, overlay=True) -> VaultRegistry:
    shipped = write_shipped(tmp_path / "vaults")
    return VaultRegistry(shipped, overlay=tmp_path / "state" / "vaults.d" if overlay else None)


def save(reg, text=EDITED, *, base=1, by="admin", note=""):
    return reg.save("demo", text, saved_by=by, base_version=base, note=note)


# --------------------------------------------------------------------------- parse_vault


def test_valid_yaml_parses():
    assert parse_vault(DEMO).id == "demo"


def test_yaml_syntax_errors_carry_a_line_number():
    with pytest.raises(VaultError) as err:
        parse_vault("id: demo\ntitle: [unclosed\nrules: []\n")
    first = err.value.errors[0]
    assert first["line"] >= 2 and "invalid YAML" in first["message"]


def test_schema_errors_point_at_the_field():
    bad = DEMO.replace("sum_insured <= 15 * income", "sum_insured <= 15 * salary")
    with pytest.raises(VaultError) as err:
        parse_vault(bad)
    assert any("rules.1" in e["path"] and "salary" in e["message"] for e in err.value.errors)


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "", "42\n"])
def test_a_vault_must_be_a_mapping(text):
    with pytest.raises(VaultError, match="mapping"):
        parse_vault(text)


def test_oversized_vaults_are_rejected():
    with pytest.raises(VaultError, match="larger than"):
        parse_vault("# " + "x" * 300_000)


def test_yaml_cannot_construct_python_objects():
    evil = "id: !!python/object/apply:os.system ['echo pwned']\ntitle: x\nrules: []\n"
    with pytest.raises(VaultError):
        parse_vault(evil)


# --------------------------------------------------------------------------- registry and overlay


def test_without_an_overlay_vaults_are_version_one_and_not_editable(tmp_path):
    reg = registry(tmp_path, overlay=False)
    assert reg.editable is False
    info = reg.info("demo")
    assert (info.version, info.source, info.updated_by) == (1, "shipped", None)
    assert reg.text("demo") == DEMO
    assert [h.version for h in reg.history("demo")] == [1]
    with pytest.raises(VaultUnavailable):
        save(reg)


def test_saving_creates_version_two_and_applies_immediately(tmp_path):
    reg = registry(tmp_path)
    saved = save(reg, note="tighten wording")
    assert saved.version == 2
    assert reg.get("demo").description == "Edited description."
    info = reg.info("demo")
    assert (info.version, info.source, info.updated_by, info.note) == (
        2,
        "edited",
        "admin",
        "tighten wording",
    )
    assert reg.get("other").title == "Other vault"  # other vaults are untouched


def test_the_yaml_text_is_stored_and_shown_verbatim_including_comments(tmp_path):
    reg = registry(tmp_path)
    with_comment = EDITED + "\n# trailing note kept exactly\n"
    save(reg, with_comment)
    assert reg.text("demo") == with_comment
    assert "must survive editing" in reg.text("demo")


def test_history_lists_every_version_and_old_versions_stay_viewable(tmp_path):
    reg = registry(tmp_path)
    save(reg, note="first")
    save(reg, EDITED.replace("Edited", "Twice edited"), base=2, by="root", note="second")
    history = reg.history("demo")
    assert [(h.version, h.saved_by, h.note) for h in history] == [
        (1, None, "Shipped version"),
        (2, "admin", "first"),
        (3, "root", "second"),
    ]
    assert reg.text("demo", 1) == DEMO  # the shipped baseline
    assert reg.text("demo", 2) == EDITED
    assert "Twice edited" in reg.text("demo")
    with pytest.raises(VaultError, match="no version"):
        reg.text("demo", 9)


def test_files_are_written_privately_with_the_shipped_file_untouched(tmp_path):
    reg = registry(tmp_path)
    save(reg)
    folder = tmp_path / "state" / "vaults.d" / "demo"
    assert (folder / "v2.yaml").read_text() == EDITED
    assert (folder / "v2.yaml").stat().st_mode & 0o777 == 0o600
    assert (folder / "index.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "vaults" / "demo.yaml").read_text() == DEMO  # never modified


def test_a_stale_save_is_refused_and_changes_nothing(tmp_path):
    reg = registry(tmp_path)
    save(reg)
    with pytest.raises(VaultConflict) as err:
        save(reg, EDITED.replace("Edited", "Stale"), base=1)
    assert err.value.current == 2
    assert reg.info("demo").version == 2 and "Stale" not in reg.text("demo")


def test_two_concurrent_saves_from_the_same_version_cannot_both_win(tmp_path):
    reg = registry(tmp_path)
    barrier, results = threading.Barrier(2), []

    def attempt(label):
        barrier.wait()
        try:
            save(reg, EDITED.replace("Edited", label), base=1, by=label)
            results.append("saved")
        except VaultConflict:
            results.append("conflict")

    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("a", "b")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["conflict", "saved"]
    assert reg.info("demo").version == 2


@pytest.mark.parametrize(
    "bad",
    [
        "id: demo\ntitle: [unclosed\n",
        DEMO.replace("sum_insured <= 15 * income", "sum_insured <= 15 * nope"),
        "- not a mapping\n",
    ],
)
def test_an_invalid_save_changes_nothing_on_disk_or_in_memory(tmp_path, bad):
    reg = registry(tmp_path)
    with pytest.raises(VaultError):
        save(reg, bad)
    assert reg.info("demo").version == 1 and reg.text("demo") == DEMO
    assert not (tmp_path / "state" / "vaults.d" / "demo").exists()


def test_the_vault_id_cannot_be_changed_by_an_edit(tmp_path):
    reg = registry(tmp_path)
    with pytest.raises(VaultError, match="cannot be changed") as err:
        save(reg, DEMO.replace("id: demo", "id: renamed"))
    assert err.value.errors[0]["path"] == "id"
    assert reg.ids() == ["demo", "other"]


def test_unknown_vaults_and_path_tricks_are_refused(tmp_path):
    reg = registry(tmp_path)
    for bad_id in ("nope", "../escape", "a/b", ".."):
        with pytest.raises(VaultError):
            reg.save(bad_id, EDITED, saved_by="admin", base_version=1)
    assert not (tmp_path / "state").exists() or not any((tmp_path / "state").rglob("*.yaml"))


def test_notes_are_trimmed_to_a_sane_length(tmp_path):
    reg = registry(tmp_path)
    assert len(save(reg, note="  " + "n" * 500).note) == 200


def test_edits_survive_a_restart_with_full_history(tmp_path):
    reg = registry(tmp_path)
    save(reg, note="one")
    save(reg, EDITED.replace("Edited", "Second"), base=2, note="two")
    reloaded = VaultRegistry(tmp_path / "vaults", overlay=tmp_path / "state" / "vaults.d")
    assert reloaded.info("demo").version == 3
    assert "Second" in reloaded.get("demo").description
    assert [h.version for h in reloaded.history("demo")] == [1, 2, 3]
    assert reloaded.errors == []


def test_a_corrupt_overlay_falls_back_to_the_shipped_vault_and_reports_it(tmp_path):
    reg = registry(tmp_path)
    save(reg)
    folder = tmp_path / "state" / "vaults.d" / "demo"
    (folder / "index.json").write_text("{not json")
    reloaded = VaultRegistry(tmp_path / "vaults", overlay=tmp_path / "state" / "vaults.d")
    assert reloaded.get("demo").description == "Original description."
    assert reloaded.info("demo").source == "shipped"
    assert any("demo" in e and "ignored" in e for e in reloaded.errors)


def test_an_edited_version_that_no_longer_validates_is_ignored_not_fatal(tmp_path):
    reg = registry(tmp_path)
    save(reg)
    (tmp_path / "state" / "vaults.d" / "demo" / "v2.yaml").write_text("id: demo\ntitle: [broken\n")
    reloaded = VaultRegistry(tmp_path / "vaults", overlay=tmp_path / "state" / "vaults.d")
    assert reloaded.info("demo").source == "shipped" and reloaded.errors


def test_the_registry_flags_when_the_shipped_file_changed_after_an_edit(tmp_path):
    reg = registry(tmp_path)
    save(reg)
    assert reg.info("demo").shipped_changed is False
    write_shipped(tmp_path / "vaults", DEMO.replace("Original", "Upstream-updated"))
    reloaded = VaultRegistry(tmp_path / "vaults", overlay=tmp_path / "state" / "vaults.d")
    assert reloaded.info("demo").shipped_changed is True
    assert reloaded.info("demo").version == 2  # the edit still wins; it is only flagged


# --------------------------------------------------------------------------- the API


def make(tmp_path, *, overlay=True, **settings):
    values = {
        "vaults_dir": tmp_path / "vaults",
        "audit_log_path": tmp_path / "state" / "audit.jsonl",
        "admin_user": ADMIN[0],
        "admin_password": ADMIN[1],
    }
    values.update(settings)
    cfg = Settings(_env_file=None, **values)
    reg = registry(tmp_path, overlay=overlay)
    prompts: list[str] = []

    def handler(system, user, schema, reasoning):
        prompts.append(user)
        return {"verdict": "cumple", "rationale": "ok", "evidence": ["Signed: yes"]}

    app = create_app(cfg, Engine(FakeLLM(handler)), reg, ui_dir=ROOT / "ui", client_dir=None)
    return TestClient(app), cfg, prompts


def put(client, vault_id="demo", **body):
    payload = {"yaml": EDITED, "base_version": 1, "note": ""}
    payload.update(body)
    return client.put(f"/v1/admin/vaults/{vault_id}", json=payload, auth=ADMIN, headers=HEADER)


def test_only_an_admin_can_use_the_vault_api(tmp_path):
    client, _, _ = make(tmp_path, access_password=VISITOR[1], access_user=VISITOR[0])
    for method, path in (("get", "/v1/admin/vaults"), ("get", "/v1/admin/vaults/demo")):
        assert getattr(client, method)(path).status_code == 401
        assert getattr(client, method)(path, auth=VISITOR).status_code == 401
    assert put(client, **{}).status_code == 200
    assert (
        client.put(
            "/v1/admin/vaults/demo",
            json={"yaml": EDITED, "base_version": 2},
            auth=VISITOR,
            headers=HEADER,
        ).status_code
        == 401
    )


def test_the_list_shows_every_vault_with_its_version_and_source(tmp_path):
    client, _, _ = make(tmp_path)
    body = client.get("/v1/admin/vaults", auth=ADMIN).json()
    assert body["editable"] is True and body["problems"] == []
    assert [(v["id"], v["version"], v["source"]) for v in body["vaults"]] == [
        ("demo", 1, "shipped"),
        ("other", 1, "shipped"),
    ]
    assert body["vaults"][0]["rule_count"] == 2 and body["vaults"][0]["title"] == "Demo vault"


def test_the_detail_page_returns_the_current_yaml_verbatim_rules_and_history(tmp_path):
    client, _, _ = make(tmp_path)
    detail = client.get("/v1/admin/vaults/demo", auth=ADMIN).json()
    assert detail["yaml"] == DEMO and "must survive editing" in detail["yaml"]
    assert detail["version"] == 1 and detail["is_current"] is True
    assert [(r["id"], r["kind"]) for r in detail["rules"]] == [
        ("signed", "criterion"),
        ("income", "check"),
    ]
    assert [h["version"] for h in detail["history"]] == [1]
    assert client.get("/v1/admin/vaults/nope", auth=ADMIN).status_code == 404


def test_an_edit_changes_the_live_vault_the_public_api_and_the_review_prompt(tmp_path):
    client, _, prompts = make(tmp_path)
    resp = put(client, note="be stricter")
    assert resp.status_code == 200 and resp.json()["saved_version"] == 2

    detail = client.get("/v1/admin/vaults/demo", auth=ADMIN).json()
    assert detail["yaml"] == EDITED and detail["source"] == "edited"
    assert (detail["updated_by"], detail["note"]) == ("admin", "be stricter")
    assert [h["version"] for h in detail["history"]] == [2, 1]  # newest first

    public = {v["id"]: v for v in client.get("/v1/vaults").json()}
    assert public["demo"]["description"] == "Edited description."

    review = client.post(
        "/v1/reviews", data={"vault_id": "demo"}, files={"file": ("a.txt", b"Signed: yes")}
    )
    assert review.status_code == 200
    assert any("The application is countersigned." in p for p in prompts)  # the new rule text
    assert not any("The application is signed." in p for p in prompts)


def test_older_versions_can_be_viewed(tmp_path):
    client, _, _ = make(tmp_path)
    put(client)
    old = client.get("/v1/admin/vaults/demo?version=1", auth=ADMIN).json()
    assert old["yaml"] == DEMO and old["is_current"] is False and old["viewing_version"] == 1
    assert client.get("/v1/admin/vaults/demo?version=9", auth=ADMIN).status_code == 404


def test_validate_reports_problems_without_saving(tmp_path):
    client, _, _ = make(tmp_path)

    def check(text):
        return client.post(
            "/v1/admin/vaults/demo/validate", json={"yaml": text}, auth=ADMIN, headers=HEADER
        )

    ok = check(EDITED)
    assert ok.status_code == 200 and ok.json() == {
        "ok": True,
        "title": "Demo vault",
        "rule_count": 2,
    }
    bad = check(DEMO.replace("sum_insured <= 15 * income", "sum_insured <= 15 * nope"))
    assert bad.status_code == 422 and "nope" in bad.json()["detail"]["errors"][0]["message"]
    syntax = check("id: demo\ntitle: [x\n")
    assert syntax.json()["detail"]["errors"][0]["line"] >= 2
    renamed = check(DEMO.replace("id: demo", "id: other2"))
    assert renamed.status_code == 422 and renamed.json()["detail"]["errors"][0]["path"] == "id"
    assert client.get("/v1/admin/vaults/demo", auth=ADMIN).json()["version"] == 1  # nothing saved


def test_an_invalid_save_is_rejected_and_the_live_vault_is_unchanged(tmp_path):
    client, _, _ = make(tmp_path)
    resp = put(client, yaml="id: demo\ntitle: [broken\n")
    assert resp.status_code == 422 and resp.json()["detail"]["errors"][0]["line"] >= 2
    assert client.get("/v1/admin/vaults/demo", auth=ADMIN).json()["yaml"] == DEMO


def test_saving_from_an_old_version_is_a_409_with_the_current_version(tmp_path):
    client, _, _ = make(tmp_path)
    assert put(client).status_code == 200
    stale = put(client, yaml=EDITED.replace("Edited", "Stale"), base_version=1)
    assert stale.status_code == 409 and stale.json()["detail"]["current"] == 2
    assert "Stale" not in client.get("/v1/admin/vaults/demo", auth=ADMIN).json()["yaml"]


def test_save_guards_unknown_vault_size_headers_and_json(tmp_path):
    client, _, _ = make(tmp_path)
    assert put(client, "nope").status_code == 404
    assert put(client, yaml="# " + "x" * 300_000).status_code == 413
    body = {"yaml": EDITED, "base_version": 1}
    assert (
        client.put("/v1/admin/vaults/demo", json=body, auth=ADMIN).status_code == 403
    )  # no header
    form = client.put(
        "/v1/admin/vaults/demo",
        data={"yaml": EDITED, "base_version": "1"},
        auth=ADMIN,
        headers=HEADER,
    )
    assert form.status_code == 415
    assert client.get("/v1/admin/vaults/demo", auth=ADMIN).json()["version"] == 1


def test_editing_is_reported_unavailable_without_a_writable_overlay(tmp_path):
    client, _, _ = make(tmp_path, overlay=False)
    assert client.get("/v1/admin/vaults", auth=ADMIN).json()["editable"] is False
    resp = put(client)
    assert resp.status_code == 501 and "not available" in resp.json()["detail"]["message"]


def test_the_audit_log_records_the_edit_by_hash_without_copying_the_vault(tmp_path):
    client, cfg, _ = make(tmp_path)
    put(client, note="raise the bar")
    audit = cfg.audit_log_path.read_text()
    assert (
        "Edited description." not in audit and "countersigned" not in audit
    )  # the text is not logged
    event = json.loads(audit.splitlines()[-1])
    assert event["event"] == "vault_edit" and event["admin"] == "admin"
    assert (event["vault_id"], event["version"], event["previous_version"]) == ("demo", 2, 1)
    assert len(event["sha256"]) == 64 and event["note"] == "raise the bar"


def test_the_status_page_notes_edited_vaults(tmp_path):
    client, _, _ = make(tmp_path)
    put(client)
    checks = {c["name"]: c for c in client.get("/v1/admin/status", auth=ADMIN).json()["checks"]}
    assert checks["vault edits"]["group"] == "Storage"
    assert "1 edited in the console: demo" in checks["vault edits"]["detail"]


@pytest.mark.parametrize("path", ["/admin/vaults", "/admin/vaults/demo", "/admin/vaults/demo/edit"])
def test_the_vault_pages_are_data_free_shells(tmp_path, path):
    client, _, _ = make(tmp_path, access_password=VISITOR[1], access_user=VISITOR[0])
    page = client.get(path)
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "no-store"
    assert "Original description" not in page.text and "must survive editing" not in page.text


# --------------------------------------------------------------------------- the CLI agrees


@pytest.fixture
def cli_state(tmp_path, monkeypatch):
    """A shipped vault directory plus a state directory holding one console edit."""
    from sentinel import cli

    shipped = write_shipped(tmp_path / "vaults")
    state = tmp_path / "state"
    monkeypatch.setenv("VAULTS_DIR", str(shipped))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(state / "audit.jsonl"))
    cli.get_settings.cache_clear()
    reg = VaultRegistry(shipped, overlay=state / "vaults.d")
    save(reg, EDITED.replace("title: Demo vault", "title: Demo vault (edited in console)"))
    yield cli, shipped
    cli.get_settings.cache_clear()


def test_the_cli_uses_console_edits_like_the_service_does(cli_state, capsys):
    cli, _ = cli_state
    assert cli.main(["vaults"]) == 0
    out = capsys.readouterr().out
    assert "Demo vault (edited in console)" in out and "Other vault" in out


def test_an_explicit_vaults_dir_is_used_as_given_without_console_edits(cli_state, capsys):
    cli, shipped = cli_state
    assert cli.main(["--vaults-dir", str(shipped), "vaults"]) == 0
    out = capsys.readouterr().out
    assert "Demo vault" in out and "edited in console" not in out


def test_the_overlay_location_is_one_definition_for_service_and_cli(tmp_path):
    settings = Settings(audit_log_path=tmp_path / "state" / "audit.jsonl")
    assert settings.vaults_overlay_dir == tmp_path / "state" / "vaults.d"
