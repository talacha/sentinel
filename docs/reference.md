# Reference

Configuration, command line, API, and vault format. For the overview see the
[README](../README.md); to get running see [Running locally](running-locally.md).

## Configuration

Set in the environment or in `.env` (copy `.env.example`). Run commands from the repository root.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_BASE_URL`, `LLM_MODEL` | none (required) | OpenAI-compatible inference endpoint and model name. There is deliberately no default endpoint. |
| `LLM_API_KEY` | `EMPTY` | Key for the endpoint (`--api-key` on vLLM). |
| `LLM_REASONING` | `true` | Nemotron reasoning mode for judgement calls. |
| `LLM_TEMPERATURE_REASONING`, `LLM_MAX_TOKENS` | `1.0`, `10000` | Sampling with reasoning on; output token budget. |
| `LLM_STRUCTURED_MODE` | `json_schema` | `json_schema`, `guided_json`, or `prompt`. Falls back to `prompt` automatically if the server rejects `response_format`. |
| `LLM_TIMEOUT_SECONDS` | `120` | Per-request timeout. |
| `TAVILY_API_KEY` | unset | Enables scoped verification. Without it, rules that need a public source resolve to `revisar`. |
| `MAX_SEARCHES_PER_REVIEW` | `5` | Hard cap on outbound searches per review. |
| `VAULTS_DIR` | `vaults` | Where the shipped vaults are read. They are never modified; edits made in the console are saved as versions in `vaults.d/` next to the audit log (see Vaults, below). |
| `AUDIT_LOG_PATH` | `audit/audit.jsonl` | Where the audit log is appended. |
| `MAX_UPLOAD_MB`, `MAX_DOCUMENT_CHARS` | `20`, `300000` | Upload size and extracted-text limits. |
| `MAX_WORKERS` | `4` | Rules reviewed concurrently. |
| `ACCESS_USER`, `ACCESS_PASSWORD` | `demo`, unset | The initial visitor login (HTTP Basic) for every page and API call except a minimal `/healthz`. Unset means no login. Manage it afterwards on the Users page (below). |
| `MAX_REVIEWS_PER_HOUR` | `0` | Global cap on reviews across all visitors (`429` when reached). `0` = unlimited. |
| `ADMIN_USER`, `ADMIN_PASSWORD` | `admin`, unset | The initial super-admin login for the console. Unset disables the console. The username and this variable are environment only; the password can be reset on the Users page. |
| `CORS_ALLOW_ORIGINS` | empty | Comma-separated browser origins allowed to call the API (only needed when the client app is hosted elsewhere). |
| `SENTINEL_DOMAIN` | unset | Read by `deploy/public` (Caddy), not by the app. |

## Command line

```bash
uv run sentinel vaults                                       # list vaults
uv run sentinel review <file> --vault <id>                   # human-readable report
uv run sentinel review <file> --vault <id> --json > report.json
uv run sentinel preflight [--env-file PATH] [--offline] [--public]   # validate configuration
```

Exit codes for `review`: `0` every rule `cumple`, `2` at least one `no_cumple` or `revisar`, `1` error.
For `preflight`: `0` ready (warnings allowed), `1` at least one failed check.

**`sentinel preflight`** checks that required values are present and not placeholders, that keys
have no stray whitespace, that `.env` is not world-readable, that no key is misspelled, and that
vaults and the audit path work. Unless `--offline`, it also proves the configuration works: it
lists the endpoint's models with your key, checks that `LLM_MODEL` is one of them, makes a real
JSON call in both modes Sentinel uses (extraction and reasoning), and runs one Tavily search
(a few tokens and one search credit). With `--public` it also requires a visitor login, a usage
cap, and a domain. It never prints secrets.

## API

| Endpoint | Description |
| --- | --- |
| `POST /v1/reviews` | Multipart form: `file` (PDF or text) and `vault_id`. Returns the JSON report. |
| `GET /v1/vaults` | Vaults and their rules. |
| `GET /healthz` | Status, LLM host and model, whether search is configured. Never returns secrets. |
| `GET /` | The built-in web UI, where people upload documents. |
| `GET /app/` | The client-facing app (see [`client/`](../client/README.md)). |
| `GET /admin`, `/status`, `/admin/config`, `/admin/users`, `/admin/vaults[/<id>[/edit]]` | The admin console: one shell, five pages (overview, status, configuration, users, vaults). Contains no data; see below. |
| `/v1/admin/*` | The console's API. Admin login required; see below. |

```bash
curl -F vault_id=insurance_life -F file=@samples/insurance/life_underwriting_01.txt \
  http://localhost:8000/v1/reviews
```

Errors: `404` unknown vault, `400` unreadable or empty document (for example an image-only PDF),
`413` upload over `MAX_UPLOAD_MB`, `503` no LLM endpoint configured, `401` login required,
`429` review cap reached (with `Retry-After`).

By default the API has no login. Set `ACCESS_PASSWORD` to require HTTP Basic credentials on every
page and call except a minimal `/healthz` (which reports only `status`, `auth_required`, and
`max_upload_mb` to anonymous callers). See the
[security checklist](../deploy/README.md#4-security-checklist).

### The report

The same JSON comes from `--json` and `POST /v1/reviews`:

- `document`: filename, SHA-256, page and character counts; `model`; `created_at`.
- `summary`: counts of `cumple`, `no_cumple`, `revisar`.
- `results[]`, one per rule, in vault order:
  - `rule_id`, `title`, `severity`, `verdict`, `rationale`;
  - `evidence[]`: `quote` (the document's own text), `page`, `start`, `end`;
  - `facts[]`: each extracted fact with its `value`, `found`, supporting `quote`, and a `note`
    when it could not be established;
  - `external` (only when a public source was consulted): `query` (the only text that left the
    perimeter), `status` (`confirmed`, `contradicted`, `unclear`, `unavailable`), `rationale`, and
    `sources[]` with `url` and `role` (`supports`, `contradicts`, `consulted`).

## Admin console: `/admin`

One shell, five pages, for the person running the deployment. Enable it by setting
`ADMIN_PASSWORD` (12+ characters, different from `ACCESS_PASSWORD`); without it the pages and
`/v1/admin/*` refuse to work. Every page has the same navigation and breadcrumbs, and moving
between them does not reload, so you sign in once. A form with unsaved changes asks before you
leave it (a link, Back, or closing the tab).

| Page | What it is for |
| --- | --- |
| **`/admin`** | The overview: a card for each page below, with a one-line live summary (ready or not, how many settings are overridden, how many users, how many vaults). |
| **`/status`** | Whether each key and model is valid. |
| **`/admin/config`** | A form to override the values the server's environment set. |
| **`/admin/users`** | Who can sign in: add users and edit them. |
| **`/admin/vaults`** | The vaults: list, view the current version, edit. |

The earlier URLs `/admin/user` and `/admin/vault[/...]` still work and redirect to the pages above.

### Status: `/status`

Runs the same checks as `sentinel preflight`, against the settings the service is actually using,
grouped by Model, Search, Access, and Storage. *Refresh* runs the static checks (free, instant);
*Run live checks* also contacts the model endpoint and Tavily to prove the keys work and the model
id exists (a few tokens and one search; one live run at a time).

### Configuration: `/admin/config`

Edits settings at runtime: the model endpoint, model id and its parameters, the Tavily key, limits,
and the review cap. A change is validated like `.env` (a batch with any invalid field applies
nothing), takes effect immediately, is saved to `overrides.json` next to the audit log with
owner-only permissions, and survives a restart. "Reset to environment value" removes an override,
and *Save, then verify with live checks* saves and opens Status with a live run. Not editable at
runtime: file paths and CORS origins. Logins are managed on the Users page, not here.

### Users: `/admin/users`

Lists the authorized users; the admin and visitor logins defined by the environment (`admin` and
`judge` in this deployment) always come first. Admins can **add** a user and **edit** one:

- **Add user:** a username (letters, digits, and `. _ @ -`, up to 64, unique ignoring case), a role
  (`visitor` runs reviews; `admin` can also use this console, so it is as powerful as the
  environment's admin), and a password: chosen (12+ characters, not easy to guess) or generated.
  A generated password is shown **once** and then wiped from the page.
- **Edit user:** change the role, and/or keep, choose, or generate a new password, in one form.
  It is all or nothing: if anything is invalid, nothing is changed. A changed password stops the
  old one working immediately, and an admin can change their own password (the console stays
  signed in).
- **Roles that cannot be changed:** your own; the environment-defined admin and visitor (their
  role is set by `ADMIN_USER` and `ACCESS_USER`); the only admin; and the only visitor (changing
  it would silently turn the visitor login off). The form says which applies.
- **Not offered on purpose:** deleting or disabling users, for the same reason.
- **Where users live:** `users.json` next to the audit log, owner-only permissions, holding
  salted scrypt **hashes only**, never plaintext.
- **Environment vs console:** the two environment-defined users follow the environment
  (a rotated `ACCESS_PASSWORD` takes effect on restart) until someone changes their password on
  this page; from then on the console's password wins and the environment value is ignored for
  that user. Removing `ACCESS_PASSWORD` from the environment removes a still-environment-defined
  visitor; the login turns off only if no other visitor exists. An environment user whose name is
  already taken by a console user is not created (the console user is never overwritten).
- **Audited:** `user_created` and `user_updated` (who did it, to whom, the role change, and whether
  a password was reset or generated); `user_password_reset` for the older reset call. Passwords
  are never logged.

The API behind the page (admin-only; writes need JSON and the `X-Sentinel-Admin` header):

| Endpoint | Description |
| --- | --- |
| `GET /v1/admin/users` | The users, each with `role`, `source`, and `role_locked` (why its role cannot be changed, or `null`). Never a hash. |
| `POST /v1/admin/users` | `{"username", "role", "password"}` or `{"username", "role", "generate": true}`. `201` with the user (and a generated `password`, once); `409` name taken; `422` with `errors` by field. |
| `PUT /v1/admin/users/<name>` | `{"role"?, "password"? or "generate"?}`. All or nothing. `422` with `errors` by field; `404` unknown user. |
| `POST /v1/admin/users/<name>/password` | `{"password"}` or `{"generate": true}`. The password-only form of the call above. |

### Vaults: `/admin/vaults`

Lists every vault with its rule count, current version, and last change. `/admin/vaults/<id>`
shows the **current version**: its rules, the YAML, and the version history (any earlier version can
be opened and read). Admins can edit at `/admin/vaults/<id>/edit`; see
[Editing vaults](#editing-vaults).

Security model:

- **Secrets are write-only.** API keys and passwords are never returned by the API, shown on the
  page, or written to the audit log; the form shows only "set (N chars)".
- **Explicit login, not a browser-cached one.** The pages are data-free shells; they sign in with
  an `Authorization` header held in memory, so a page on another site cannot ride on a saved
  login. Every call also needs an `X-Sentinel-Admin: 1` header, and writes must be JSON.
- **Brute-force lockout.** Ten wrong admin passwords in five minutes lock the admin API for the
  remainder of the window, even against the correct password. This is deliberately global and
  applies to the admin login only, so it cannot be used to lock visitors out.
- **Audited.** Each change appends a `config_change` event to the audit log with who, which
  fields, and non-secret values (for the endpoint URL, only the host).
- **Powerful by design.** An admin can change where documents are sent, and can rewrite the
  policy that reviews are judged against. Treat `ADMIN_PASSWORD` like a root password, and check
  `/status` after any change.

## Vaults

A vault is a YAML file of rules in `vaults/`. The engine is domain-agnostic, so swapping the
vault changes the sector with no code changes. Shipped: `insurance_life`, `legal_contract`, and
`health_patient` (illustrative demo policy, not regulatory, legal, or clinical guidance).

A rule is one of two kinds:

- **`check`**: the model extracts `facts` (each backed by a quote) and a deterministic expression
  decides. Fact types are `number`, `boolean`, `date`, and `text`; expressions may use the facts,
  the rule's `references` (vault-held constants), and `abs`, `min`, `max`, `round`, `len`. Optional
  facts that are not found are `None`; a missing required fact gives `revisar`.
- **`criterion`**: a natural-language criterion the model judges, citing verbatim quotes.

`on_fail` says what a failing rule becomes (`no_cumple` or `revisar`). A `check` rule may add an
`external_check`. It runs only when the check fails, to confirm that the vault reference (for
example a threshold) is still in force before a failure is asserted.

```yaml
id: insurance_life
title: Life insurance underwriting
params:
  jurisdiction: México          # the only values a query template may use, besides {year}

rules:
  - id: income-multiple
    title: Sum insured is proportionate to declared income
    facts:
      - { name: sum_insured, type: number, description: "Total sum insured requested, in MXN" }
      - { name: declared_income, type: number, description: "Declared ANNUAL income, in MXN" }
      - { name: justification, type: text, optional: true, description: "Stated justification" }
    check: "sum_insured <= 15 * declared_income or justification is not None"
    on_fail: revisar

  - id: enhanced-review-threshold
    title: Enhanced financial review above the regulatory threshold
    references: { threshold_mxn: 2000000 }
    facts:
      - { name: sum_insured, type: number, description: "Total sum insured requested, in MXN" }
      - { name: financial_questionnaire_attached, type: boolean, optional: true, description: "..." }
    check: "sum_insured < threshold_mxn or financial_questionnaire_attached == True"
    on_fail: no_cumple
    external_check:
      statement: "A regulatory threshold of MXN 2,000,000 ... is currently in force in Mexico."
      query_template: "umbral regulatorio revisión financiera suscripción seguro de vida {jurisdiction} {year} vigente"
      allowed_domains: ["gob.mx"]

  - id: smoker-consistency
    title: Tobacco declaration is consistent with lab results
    criterion: >-
      The applicant's tobacco declaration must not conflict with any laboratory result in the file.
    on_fail: no_cumple
```

Vaults are validated when they load, and a mistake names the file and field:

- exactly one of `check` or `criterion` per rule; `check` rules declare `facts`, `criterion` rules
  do not;
- `check` expressions may only use declared fact and reference names and the allowed functions;
- `external_check` requires a `check`;
- query templates may only use the vault's `params` and `{year}`, so a query can never contain
  document content.

Write rules so a `cumple` or `no_cumple` can point at a passage in the document: a verdict without
a verifiable quote becomes `revisar`. Keep real client data out of the repository.

### Editing vaults

An admin edits a vault at `/admin/vaults/<id>/edit`: a YAML editor with **Validate** (checks
without saving) and **Save as new version**, plus an optional note that is kept in the history.

- **Same validation as a shipped file.** A save that fails is rejected whole, and the live vault
  is unchanged. Errors show the line and column for YAML syntax problems, and the field for rule
  problems. The vault's `id` cannot change, and a vault can be at most 256 KB.
- **Versions, not overwrites.** The shipped file is version 1 and is never modified (it can be
  mounted read-only). Each save writes the next version (2, 3, ...) to `vaults.d/<id>/` next to the
  audit log, with owner-only permissions. Every version stays readable at
  `/admin/vaults/<id>?version=N`. To go back, open an old version and save its text again.
- **Applies immediately.** New reviews use the saved version at once, with no restart, through the
  API and the CLI alike (`sentinel --vaults-dir DIR ...` reads that directory as given, without
  edits). A review already running finishes on the version it started with.
- **No lost updates.** The editor remembers the version it started from. If someone saved in the
  meantime, the save is refused (`409`) and the editor keeps your text.
- **If the shipped file changes later** (for example after a `git pull`), the edited version stays
  in use and the page says so, so a shipped fix is never silently ignored or silently applied.
- **Audited by hash.** Each save appends a `vault_edit` event (who, which vault and version, the
  SHA-256 of the saved text, and the note). The vault text is not copied into the log.
- **Keep the state directory.** It holds the edits, so it belongs in a volume that survives
  redeploys; `deploy/public` already keeps it in the `sentinel-data` volume.

The console's API, all admin-only (writes need JSON and the `X-Sentinel-Admin` header):

| Endpoint | Description |
| --- | --- |
| `GET /v1/admin/vaults` | Every vault: version, source (`shipped` or `edited`), last change, rule count. |
| `GET /v1/admin/vaults/<id>[?version=N]` | The YAML, rules, and version history. |
| `POST /v1/admin/vaults/<id>/validate` | `{"yaml": "..."}`. `200` if valid, else `422` with `errors[]`. Saves nothing. |
| `PUT /v1/admin/vaults/<id>` | `{"yaml", "base_version", "note"}`. `200` with `saved_version`; `409` if `base_version` is stale; `413` too large; `422` invalid. |
