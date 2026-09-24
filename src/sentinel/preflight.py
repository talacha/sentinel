"""Preflight: validate configuration before you deploy or demo. Never prints secrets.

    sentinel preflight [--env-file PATH] [--offline] [--public]

Static checks look for missing, placeholder, and mistyped values, file permissions, vaults, and
the audit path. Live checks (skipped with --offline) call your model endpoint and Tavily with a
few tokens and one search, so a wrong key, a wrong model id, or a server that rejects the
parameters Sentinel sends shows up here instead of in front of an audience.
"""

from __future__ import annotations

import difflib
import ipaddress
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from .config import Settings
from .llm import LLM, LLMError, OpenAICompatibleLLM
from .vault import VaultError, VaultRegistry
from .verify import SearchClient, TavilySearch

Status = Literal["pass", "warn", "fail", "skip"]

# Keys that live in .env but are not `Settings` fields (read by docker compose instead).
EXTRA_KEYS = {"SENTINEL_DOMAIN"}

WEAK_PASSWORDS = {"password", "demo", "sentinel", "changeme", "admin", "letmein", "123456"}
_PLACEHOLDER = re.compile(r"[<>]|\.\.\.|your[-_ ]|changeme|xxxx", re.IGNORECASE)
_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)


class PingOut(BaseModel):
    ok: bool
    word: str


# Which section of the /status page a check belongs to.
GROUPS = {
    ".env file": "Configuration",
    ".env permissions": "Configuration",
    "unknown keys": "Configuration",
    "admin overrides": "Configuration",
    "LLM_BASE_URL": "Model",
    "LLM_MODEL": "Model",
    "LLM_API_KEY": "Model",
    "LLM_MAX_TOKENS": "Model",
    "live model checks": "Model",
    "endpoint reachable": "Model",
    "model id": "Model",
    "extraction call": "Model",
    "judgement call (reasoning)": "Model",
    "structured output": "Model",
    "TAVILY_API_KEY": "Search",
    "Tavily search": "Search",
    "vaults": "Storage",
    "audit log": "Storage",
    "ACCESS_PASSWORD": "Access",
    "ADMIN_PASSWORD": "Access",
    "users": "Access",
    "vault edits": "Storage",
    "MAX_REVIEWS_PER_HOUR": "Access",
    "SENTINEL_DOMAIN": "Access",
}


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    group: str = "General"


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: Status, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail, GROUPS.get(name, "General")))

    def count(self, status: Status) -> int:
        return sum(1 for c in self.checks if c.status == status)

    @property
    def ok(self) -> bool:
        return self.count("fail") == 0


def _secrets(settings: Settings) -> list[str]:
    return [
        s
        for s in (
            settings.llm_api_key,
            settings.tavily_api_key,
            settings.access_password,
            settings.admin_password,
        )
        if s
    ]


def redact(report: Report, secrets: list[str]) -> Report:
    """Replace any secret that leaked into a message (for example inside an error string)."""
    real = [s for s in secrets if s and s != "EMPTY" and len(s) >= 4]
    for check in report.checks:
        for secret in real:
            check.detail = check.detail.replace(secret, "***")
    return report


def _is_private_host(host: str) -> bool:
    if host in {"localhost", "host.docker.internal"} or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _mask(value: str) -> str:
    return f"set ({len(value)} chars)"


# --------------------------------------------------------------------------- static checks


def check_env_file(report: Report, env_path: Path | None) -> None:
    if env_path is None or not env_path.is_file():
        report.add(
            ".env file",
            "warn",
            "no .env file found; only the process environment is used (copy .env.example to .env)",
        )
        return
    mode = env_path.stat().st_mode & 0o777
    if mode & 0o077:
        report.add(
            ".env permissions",
            "warn",
            f"{env_path} is readable by other users (mode {mode:o}); run: chmod 600 {env_path}",
        )
    else:
        report.add(".env file", "pass", f"{env_path} (mode {mode:o})")


def check_unknown_keys(report: Report, raw: dict[str, str | None]) -> None:
    known = {name.upper() for name in Settings.model_fields} | EXTRA_KEYS
    unknown = sorted(k for k in raw if k.upper() not in known)
    if not unknown:
        report.add("unknown keys", "pass", "every key in .env is recognized")
        return
    parts = []
    for key in unknown:
        close = difflib.get_close_matches(key.upper(), sorted(known), n=1)
        parts.append(f"{key} (did you mean {close[0]}?)" if close else key)
    report.add("unknown keys", "warn", "ignored, probably typos: " + ", ".join(parts))


def check_llm(report: Report, s: Settings) -> None:
    url = (s.llm_base_url or "").strip()
    host = ""
    if not url:
        report.add("LLM_BASE_URL", "fail", "not set; there is no default endpoint")
    elif _PLACEHOLDER.search(url):
        report.add("LLM_BASE_URL", "fail", "still looks like a placeholder")
    else:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not host:
            report.add("LLM_BASE_URL", "fail", "must be a full http(s) URL, e.g. https://host/v1")
        elif parsed.scheme == "http" and not _is_private_host(host):
            report.add("LLM_BASE_URL", "warn", f"{host} over plain http: traffic is unencrypted")
        else:
            report.add("LLM_BASE_URL", "pass", f"{parsed.scheme}://{parsed.netloc}{parsed.path}")

    model = (s.llm_model or "").strip()
    if not model:
        report.add("LLM_MODEL", "fail", "not set")
    elif _PLACEHOLDER.search(model):
        report.add("LLM_MODEL", "fail", "still looks like a placeholder")
    else:
        report.add("LLM_MODEL", "pass", model)

    key = s.llm_api_key
    remote = bool(host) and not _is_private_host(host)
    if key != key.strip():
        report.add("LLM_API_KEY", "fail", "has leading or trailing whitespace")
    elif key in {"", "EMPTY"}:
        if remote:
            report.add("LLM_API_KEY", "fail", f"{host} is a remote endpoint but no key is set")
        else:
            report.add("LLM_API_KEY", "pass", "no key (fine for a private vLLM without --api-key)")
    elif _PLACEHOLDER.search(key):
        report.add("LLM_API_KEY", "fail", "still looks like a placeholder")
    else:
        report.add("LLM_API_KEY", "pass", _mask(key))

    if s.llm_max_tokens < 2000:
        report.add(
            "LLM_MAX_TOKENS", "warn", f"{s.llm_max_tokens} may truncate reasoning; 10000 suggested"
        )


def check_search_key(report: Report, s: Settings) -> None:
    key = s.tavily_api_key
    if not key:
        report.add(
            "TAVILY_API_KEY",
            "warn",
            "not set: rules that need a public source will resolve to revisar, and there is no "
            "live search to show",
        )
    elif key != key.strip():
        report.add("TAVILY_API_KEY", "fail", "has leading or trailing whitespace")
    elif _PLACEHOLDER.search(key):
        report.add("TAVILY_API_KEY", "fail", "still looks like a placeholder")
    elif not key.startswith("tvly-"):
        report.add("TAVILY_API_KEY", "warn", f"{_mask(key)}; Tavily keys normally start with tvly-")
    else:
        report.add("TAVILY_API_KEY", "pass", _mask(key))


def check_paths(report: Report, s: Settings) -> None:
    try:
        registry = VaultRegistry(s.vaults_dir)
        rules = sum(len(v.rules) for v in registry)
        if len(registry) == 0:
            report.add("vaults", "fail", f"no *.yaml vaults in {s.vaults_dir.resolve()}")
        else:
            report.add("vaults", "pass", f"{len(registry)} vaults, {rules} rules ({s.vaults_dir})")
    except (VaultError, OSError) as exc:
        report.add("vaults", "fail", str(exc))

    # Audit log: the nearest existing ancestor of the log's directory must be writable.
    target = s.audit_log_path.resolve().parent
    ancestor = target
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    probe = ancestor / ".sentinel-preflight"
    try:
        probe.touch()
        probe.unlink()
        report.add("audit log", "pass", f"{s.audit_log_path} is writable")
    except OSError as exc:
        report.add("audit log", "fail", f"cannot write under {ancestor}: {exc.strerror}")


def check_public(report: Report, s: Settings, raw: dict[str, str | None], public: bool) -> None:
    password = s.access_password or ""
    if not password:
        if public:
            report.add(
                "ACCESS_PASSWORD",
                "fail",
                "not set: the app has no login and anyone who finds the URL can use your credits",
            )
        else:
            report.add("ACCESS_PASSWORD", "pass", "not set (login off; fine for localhost)")
    elif password.lower() in WEAK_PASSWORDS or len(password) < 12:
        report.add(
            "ACCESS_PASSWORD",
            "fail" if public else "warn",
            "too weak: use 12+ characters, e.g. `openssl rand -base64 18`",
        )
    else:
        report.add("ACCESS_PASSWORD", "pass", f"{_mask(password)}, user '{s.access_user}'")

    admin = s.admin_password or ""
    if not admin:
        report.add(
            "ADMIN_PASSWORD",
            "warn" if public else "pass",
            "not set: the /status and /admin pages are disabled",
        )
    elif admin.lower() in WEAK_PASSWORDS or len(admin) < 12:
        # A super-admin can redirect where documents are sent, so a weak one always fails.
        report.add(
            "ADMIN_PASSWORD", "fail", "too weak: use 12+ characters, e.g. `openssl rand -base64 18`"
        )
    elif admin == password:
        report.add(
            "ADMIN_PASSWORD", "warn", "same as ACCESS_PASSWORD: visitors could open the admin page"
        )
    else:
        report.add("ADMIN_PASSWORD", "pass", f"{_mask(admin)}, user '{s.admin_user}'")

    if not public:
        return
    if s.max_reviews_per_hour == 0:
        report.add(
            "MAX_REVIEWS_PER_HOUR",
            "warn",
            "0 = unlimited: visitors can spend your token and search credits (try 30)",
        )
    else:
        report.add("MAX_REVIEWS_PER_HOUR", "pass", f"{s.max_reviews_per_hour} per hour")

    domain = (raw.get("SENTINEL_DOMAIN") or "").strip()
    if not domain:
        report.add(
            "SENTINEL_DOMAIN", "fail", "not set: Caddy needs the hostname to get a certificate"
        )
    elif domain == "localhost":
        report.add(
            "SENTINEL_DOMAIN", "warn", "localhost: fine for a local test, not for the public"
        )
    elif not _HOSTNAME.match(domain):
        report.add("SENTINEL_DOMAIN", "fail", f"{domain!r} is not a valid hostname")
    else:
        report.add("SENTINEL_DOMAIN", "pass", domain)


# --------------------------------------------------------------------------- live checks


def check_model_listing(report: Report, s: Settings, http: httpx.Client) -> None:
    url = (s.llm_base_url or "").rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    try:
        resp = http.get(url, headers=headers, timeout=15)
    except httpx.HTTPError as exc:
        report.add("endpoint reachable", "fail", f"{type(exc).__name__}: {exc}")
        return
    if resp.status_code in (401, 403):
        report.add("endpoint reachable", "fail", f"key rejected (HTTP {resp.status_code})")
        return
    if resp.status_code != 200:
        report.add(
            "endpoint reachable",
            "warn",
            f"GET /models returned HTTP {resp.status_code}; cannot verify the model id",
        )
        return
    report.add("endpoint reachable", "pass", "authenticated OK")
    try:
        ids = [m["id"] for m in resp.json()["data"]]
    except (ValueError, KeyError, TypeError):
        report.add("model id", "warn", "the /models response was not in OpenAI format")
        return
    model = s.llm_model or ""
    if model in ids:
        report.add("model id", "pass", f"{model} is served here")
        return
    similar = difflib.get_close_matches(model, ids, n=3, cutoff=0.4)
    nemotron = [i for i in ids if "nemotron" in i.lower()][:5]
    hint = similar or nemotron
    report.add(
        "model id",
        "fail",
        f"{model!r} is not listed by this endpoint ({len(ids)} models)."
        + (f" Similar: {', '.join(hint)}" if hint else ""),
    )


def check_llm_roundtrip(report: Report, llm: LLM) -> None:
    configured = getattr(llm, "_mode", None)
    for label, reasoning in (("extraction call", False), ("judgement call (reasoning)", True)):
        start = time.perf_counter()
        try:
            out = llm.complete_json(
                system="You are a connectivity test.",
                user='Reply with ok=true and word="sentinel".',
                schema=PingOut,
                reasoning=reasoning,
            )
        except LLMError as exc:
            report.add(label, "fail", str(exc))
            continue
        took = time.perf_counter() - start
        if out.ok:
            report.add(label, "pass", f"valid JSON in {took:.1f}s")
        else:
            report.add(label, "warn", f"valid JSON but unexpected content, in {took:.1f}s")
    now = getattr(llm, "_mode", None)
    if configured and now and now != configured:
        report.add(
            "structured output",
            "warn",
            f"the server rejected {configured!r}; running in {now!r} mode (replies are still "
            f"validated). Set LLM_STRUCTURED_MODE={now} to skip the failed attempt",
        )
    elif configured:
        report.add("structured output", "pass", f"{configured} accepted")


def check_search_roundtrip(report: Report, search: SearchClient) -> None:
    try:
        results = search.search("Nebius Token Factory", include_domains=[], max_results=1)
    except Exception as exc:  # any failure (auth, quota, network) is the answer here
        report.add("Tavily search", "fail", f"{type(exc).__name__}: {exc}")
        return
    report.add("Tavily search", "pass", f"live search returned {len(results)} result(s)")


# --------------------------------------------------------------------------- entry point


def run(
    settings: Settings,
    raw: dict[str, str | None],
    env_path: Path | None,
    *,
    live: bool = True,
    public: bool = False,
    include_env_file: bool = True,
    http: httpx.Client | None = None,
    llm: LLM | None = None,
    search: SearchClient | None = None,
) -> Report:
    """`include_env_file=False` skips the checks about the .env file itself (a running service,
    for example in a container, has none), leaving only the effective settings."""
    report = Report()
    if include_env_file:
        check_env_file(report, env_path)
        check_unknown_keys(report, raw)
    check_llm(report, settings)
    check_search_key(report, settings)
    check_paths(report, settings)
    check_public(report, settings, raw, public)

    if live:
        llm_ready = not any(
            c.status == "fail" and c.name in {"LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"}
            for c in report.checks
        )
        if llm_ready:
            own_http = http or httpx.Client()
            try:
                check_model_listing(report, settings, own_http)
            finally:
                if http is None:
                    own_http.close()
            check_llm_roundtrip(report, llm or OpenAICompatibleLLM.from_settings(settings))
        else:
            report.add("live model checks", "skip", "fix the LLM settings above first")

        if settings.tavily_api_key:
            client = search or TavilySearch(settings.tavily_api_key)
            check_search_roundtrip(report, client)
        else:
            report.add("Tavily search", "skip", "no TAVILY_API_KEY")
    else:
        report.add("live checks", "skip", "--offline: endpoint and Tavily were not contacted")

    return redact(report, _secrets(settings))


_SYMBOL = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "skip"}


def render(report: Report) -> str:
    width = max(len(c.name) for c in report.checks)
    lines = [
        f"[{_SYMBOL[c.status]}] {c.name.ljust(width)}  {c.detail}".rstrip() for c in report.checks
    ]
    lines.append("")
    lines.append(
        f"{report.count('pass')} passed, {report.count('warn')} warnings, "
        f"{report.count('fail')} failed, {report.count('skip')} skipped"
    )
    lines.append("READY" if report.ok else "NOT READY: fix the FAIL lines above")
    return "\n".join(lines)
