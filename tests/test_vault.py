from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from sentinel.config import ConfigError, Settings
from sentinel.models import Rule, Vault, Verdict
from sentinel.vault import VaultError, VaultRegistry, load_vault


def rule(**overrides):
    base = {
        "id": "income-multiple",
        "title": "Sum insured proportionate to income",
        "facts": [
            {"name": "sum_insured", "type": "number", "description": "sum"},
            {"name": "income", "type": "number", "description": "income"},
        ],
        "check": "sum_insured <= 15 * income",
        "on_fail": "revisar",
    }
    base.update(overrides)
    return base


def vault(**overrides):
    base = {"id": "v1", "title": "V", "params": {"jurisdiction": "Mexico"}, "rules": [rule()]}
    base.update(overrides)
    return base


def test_valid_vault_parses():
    v = Vault.model_validate(vault())
    assert v.rules[0].on_fail == Verdict.REVISAR


def test_rule_needs_exactly_one_of_check_or_criterion():
    with pytest.raises(ValidationError, match="exactly one"):
        Rule.model_validate(rule(criterion="be nice"))
    with pytest.raises(ValidationError, match="exactly one"):
        Rule.model_validate(rule(check=None, facts=[]))


def test_criterion_rule_cannot_declare_facts():
    with pytest.raises(ValidationError, match="must not declare facts"):
        Rule.model_validate(rule(check=None, criterion="x"))


def test_check_rejects_undefined_names_and_unsafe_calls():
    with pytest.raises(ValidationError, match="undefined name"):
        Rule.model_validate(rule(check="sum_insured <= 15 * salary"))
    with pytest.raises(ValidationError, match="not allowed"):
        Rule.model_validate(rule(check="__import__('os').system('x')"))
    with pytest.raises(ValidationError, match="invalid check"):
        Rule.model_validate(rule(check="sum_insured <="))


def test_on_fail_cannot_be_cumple():
    with pytest.raises(ValidationError, match="on_fail"):
        Rule.model_validate(rule(on_fail="cumple"))


def test_references_usable_in_check():
    r = Rule.model_validate(
        rule(references={"threshold": 2_000_000}, check="sum_insured < threshold")
    )
    assert r.references["threshold"] == 2_000_000


def test_external_check_placeholders_limited_to_vault_params_and_year():
    ext = {
        "statement": "threshold is 2M",
        "query_template": "threshold {jurisdiction} {year}",
    }
    ok = vault(rules=[rule(external_check=ext)])
    Vault.model_validate(ok)

    leaky = dict(ext, query_template="threshold for {applicant_name}")
    with pytest.raises(ValidationError, match="applicant_name"):
        Vault.model_validate(vault(rules=[rule(external_check=leaky)]))


def test_duplicate_rule_ids_rejected():
    with pytest.raises(ValidationError, match="duplicate rule ids"):
        Vault.model_validate(vault(rules=[rule(), rule()]))


def test_load_vault_reports_path_and_field(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(vault(rules=[rule(check="nope + 1")])))
    with pytest.raises(VaultError, match="bad.yaml"):
        load_vault(p)
    with pytest.raises(VaultError, match="not found"):
        load_vault(tmp_path / "missing.yaml")


def test_registry_loads_directory_and_rejects_unknown(tmp_path):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(vault(id="alpha")))
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(vault(id="beta")))
    reg = VaultRegistry(tmp_path)
    assert reg.ids() == ["alpha", "beta"]
    assert reg.get("alpha").title == "V"
    with pytest.raises(VaultError, match="unknown vault"):
        reg.get("gamma")


def test_settings_require_llm_has_no_default_endpoint(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    s = Settings(_env_file=None)
    with pytest.raises(ConfigError, match="LLM_BASE_URL"):
        s.require_llm()
    s2 = Settings(_env_file=None, llm_base_url="https://gpu.internal:8000/v1", llm_model="m")
    assert s2.require_llm() == ("https://gpu.internal:8000/v1", "m")
    assert s2.llm_host == "gpu.internal:8000"
