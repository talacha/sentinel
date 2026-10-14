# Roadmap

What to do next, written on 2026-09-24 so the work can be picked up cold. **Nothing in this file has
been started** unless it says so. It has three tracks:

- **A. Admin experience rebuild:** the largest piece, in milestones A0 to A6, with the detailed plan
  and task list in [admin-rebuild.md](admin-rebuild.md).
- **B. Product and platform follow-ups:** smaller items found while building.
- **C. Hackathon submission:** dated checklist for the 2026-10-30 deadline.

## Resume here

1. Read this file, then [admin-rebuild.md](admin-rebuild.md), then `CLAUDE.md` (the invariants that
   must not break).
2. Start with **A0.1**: rescue the browser test harness (next section). Everything after it is safer
   with that net in place.
3. Check the state below is still true (`git log`, the open PRs, and the live site's version)
   before trusting it.

Everyday commands:

```bash
uv sync                                        # install
uv run pytest                                  # 435 tests, no network
uv run ruff check . && uv run ruff format --check .
uv run uvicorn sentinel.api:app                # API, UI and console on :8000 (needs a .env; see running-locally.md)
```

Pull requests: base every PR on `main`. `main` is protected by the CI job `test` (ruff and pytest);
auto-merge is enabled on the repository, so after the check passes `gh pr merge <n> --auto --merge`
merges it. There are no required reviewers.

## Where things stand (2026-09-24)

| Area | State |
| --- | --- |
| Code | On `main` at `628d658`: the review engine, client app, preflight, visitor and admin logins, review cap, and the admin console (overview, status, configuration, users add and edit, vaults list, view, edit, **create**, versions), strict CSP, and CI. 435 tests. |
| Live | https://sntnl.cc runs `58e7f1c`. `main` is ahead by PR #12 (new vaults), so **`/admin/vaults/new` is not live** until the VM is updated. Nothing deploys automatically: use [Updating a running deployment](hackathon.md#updating-a-running-deployment). |
| Model | Nemotron 3 Nano through Nebius Token Factory, verified live: 24 of 24 sample verdicts as expected. |
| Submission | Closes **2026-10-30, 10:00 AM PT**. The demo URL is live. The video and the Devpost entry are not started. |
| Known gaps | See tracks A and B. The main ones: no audit-log viewer, no delete or disable for users and vaults, review reports do not record the vault version, no backup procedure for the state volume, and nothing in the product shows which version is running. |

### Tooling to rescue (do this first)

During the console work a browser-level test harness was built and used to verify every page:
about 190 jsdom checks (overview and navigation, configuration, users, vaults, including the files
created on disk) plus a real-Chrome run that takes screenshots (light, dark, phone), checks layout,
redirects and the Content-Security-Policy, and fails on any CSP violation. It caught two real bugs
that unit tests could not: author CSS un-hiding `[hidden]` elements, and a CSP that would have broken
the page.

**It is not in the repository.** It lives in a temporary scratch directory outside the repo (the
Claude Code job directory of the 2026-09-23/24 session) and will be lost when that job is deleted.
The pieces: a shared helper (`smoke_lib.mjs`), one script per area (`ux_hub_config.mjs`,
`ux_users.mjs`, `ux_vaults.mjs`), the real-Chrome run (`real_browser.mjs`), a live-site check
(`live_browser.mjs`), a CSP negative test (`csp_negative.mjs`), and small shell scripts that start a
throwaway server and run the suite. Task **A0.1** ports it into `tests/e2e/`. If you need the
originals before then, ask for them before that job is cleaned up.

Why it matters beyond this project: jsdom does not implement the CSS cascade, so it cannot catch
layout or visibility bugs. Anything visual must be checked in a real browser.

## Track A: Admin experience rebuild

The console feels fractured because it grew one request at a time: three front-ends with no common
shell, three ways to sign in, a menu on only one of them, four interaction patterns for the same
job, and an incomplete feature set. The rebuild gives it one shell, one menu that follows you on
every screen, shared components, and the missing capabilities. Full audit, target design, decisions
and tasks: [admin-rebuild.md](admin-rebuild.md).

| Milestone | Goal | Size | Needs |
| --- | --- | :---: | --- |
| **A0** Decide and make it testable | E2E suite in the repo and CI, ADRs, wireframes, tokens, vocabulary. No user-visible change. | S to M | |
| **A1** App shell and menu | The consistent menu experience: sidebar and phone drawer, router, role-aware menu, account menu, strict CSP with no inline code. Pages move in unchanged. | M to L | A0 |
| **A2** Components and patterns | One component set; the same list, detail, create, edit, confirm and feedback patterns on every page. | L | A1 |
| **A3** Sign-in and account | Sign in once for the whole product; my account and change password. Security-sensitive. | M to L | A0 (decision D2) |
| **A4** Complete the capabilities | Audit-log viewer, delete or disable users and vaults, version diff and restore, export and import, build info and deploy script. | L | A2 |
| **A5** One product, one menu | The review flow inside the shell; retire the duplicate UI; shared design tokens. | M | A2, decision D3 |
| **A6** Quality bar | Accessibility, responsive and performance checks in CI, visual regression, help text, docs. | M | A2 |

Each milestone ships on its own and leaves `main` working. Eight decisions (front-end architecture,
what "signed in" means, one or three front-ends, vocabulary, menu layout, and so on) are listed
with recommendations in [admin-rebuild.md, section 4](admin-rebuild.md#4-decisions-to-make-first).

## Track B: Product and platform follow-ups

| ID | Task | Size | Why |
| --- | --- | :---: | --- |
| B1 | **Record the vault version and text hash on every review report and audit event**, and show it in the report header of the built-in UI and the client. | S | Vaults are now editable, so a verdict must say which policy version it was judged against. The change to the report is additive. |
| B2 | **Backup and restore of the state volume** (`sentinel-data`): a documented procedure and `scripts/backup.sh`, and a restore test. | S | The volume holds users (hashes), overrides, vault edits and additions, and the audit log. Nothing else has a copy, and the teardown plan deletes the VM. |
| B3 | **Audit log rotation and retention.** Rotate `audit.jsonl` by size, keep a set number of files, and make the viewer (A4.1) read across them. | S | The file grows without bound. |
| B4 | **Deploy path** (decision D8): `scripts/deploy.sh` first (with A4.6), then evaluate a pull-based updater on the VM. A GitHub Action over SSH does not fit while SSH is limited to one IP. | S to M | Merging does not deploy; the live site fell two PRs behind unnoticed. |
| B5 | **Admin throttle review.** Today ten wrong admin passwords lock the admin API for everyone for five minutes; that is deliberate (it cannot be used against visitors) but anyone who can reach the site can trigger it. A per-client bucket behind Caddy needs trusted proxy headers for the Caddy container only. | S to M | Reduces a denial-of-service lever without weakening brute-force protection. Best done inside A3. |
| B6 | **Check the client app in a real browser** and run an accessibility pass on it. It has only been tested in jsdom. | S | It is what judges see first. Fold into A0's harness. |
| B7 | **Expand the evals:** more synthetic samples per vault, including negative cases; a manual or nightly workflow that runs them with real keys (never in PR CI). | M | 24 verdicts is a good smoke test, not a benchmark. |
| B8 | **Vault authoring aids:** a JSON Schema for the vault YAML (editor hints), and "try this rule on a pasted snippet". | L | Makes vault authoring approachable. The snippet stays inside the perimeter like any document, so it must go through the same engine and the same invariants. Later. |

Explicit non-goals: storing documents or reports (breaks the core invariant); multi-tenant or
fine-grained roles; translating the UI.

## Track C: Hackathon submission (deadline 2026-10-30, 10:00 AM PT)

The judge-facing surface is the live demo at `/app/` and the video, not the admin console, so the
submission takes priority over track A after the freeze date below.

| ID | Task | By |
| --- | --- | --- |
| C1 | Re-read the [official rules](https://nebiusglobalaihackathon.devpost.com/rules). They can change; update the table in [hackathon.md](hackathon.md). | Oct 5 |
| C2 | Decide judge access: the visitor login to hand out, whether `MAX_REVIEWS_PER_HOUR=30` (shared by all visitors) is enough during judging, and the Token Factory and Tavily budgets. | Oct 5 |
| C3 | Write the demo script and record the video: under 3 minutes, public on YouTube, with audio explaining how Token Factory and Nemotron are used. Suggested arc: the cotinine contradiction (`no_cumple`), the income multiple (`revisar`), the threshold confirmed against a public source (Tavily), then swap the vault to prove the engine is domain-agnostic. See the [demo checklist](../deploy/README.md#7-demo-checklist). | Oct 24 |
| C4 | Devpost entry: description, testing instructions (URL and credentials), built with (Nemotron, Token Factory, Tavily, FastAPI, Nebius), images, video and repository links; track 02 plus the Tavily bonus. | Oct 24 |
| C5 | Refresh the README's "How it works" to highlight Nemotron and Token Factory (the rules table marks this "partly"). | Oct 24 |
| C6 | Final verification on the live site from a clean browser and a phone: every bundled sample on all three vaults, `/status` live checks, HTTPS and the `www` redirect, certificate and disk headroom. | Oct 26 |
| C7 | Tag the submitted build (`v1.0.0-submission`), record the deployed commit, and **do not deploy during judging (Dec 1 to 15)** except for a fix. | Oct 27 |
| C8 | After the results: rotate the passwords, revoke the Token Factory and Tavily keys, and tear down the VM, disk, static IP, security group and project ([hackathon.md](hackathon.md)). | After Jan 11, 2027 |

## Suggested sequencing

Thirty-six days remain to the deadline (a Thursday today, a Friday on 2026-10-30). This is a
proposal, sized for one person; adjust it to the real pace.

| Dates | Work |
| --- | --- |
| Sep 24 to 27 | Update the VM (deploys #12). **B1** and **B2** (small, valuable, unrelated to the rebuild). **A0.1 to A0.3**: the e2e suite in the repo and in CI. |
| Sep 28 to Oct 4 | **A0.4 to A0.7**: decisions, wireframes, tokens, vocabulary. Review the wireframes before any UI moves. C1, C2. |
| Oct 5 to 11 | **A1**: the shell and the menu. |
| Oct 12 to 18 | **A2**: components and the consistent patterns (at least Users and Vaults). If time allows, pull **A5.3** forward (shared design tokens and matching menu labels in the client): it is the cheapest visible consistency win for judges. |
| **Oct 19** | **Feature freeze** for the submission build. Only fixes after this. Deploy the frozen build by Oct 21. |
| Oct 19 to 24 | C3 (video), C4 (Devpost), C5 (README). |
| Oct 25 to 27 | C6 and C7. **Submit by Oct 27**, three days early. |
| Oct 28 to 30 | Buffer only. |
| After submission | **A3, A4, A5, A6**, then B3 to B8. A3 (sign-in) is deliberately after the deadline: it is security sensitive and judges never see it. |

## Working agreements and lessons

- **Base every PR on `main`.** Stacking a PR on another PR's branch once caused a feature to be merged
  into a dead branch and never reach `main`.
- **Merging does not deploy.** After merging anything user-facing, update the VM and check the live
  site. A4.6 adds a visible build version so this can be seen.
- **Check anything visual in a real browser** and include screenshots in the PR. jsdom cannot check
  CSS.
- **Never print secrets.** Verify the live site read-only (status codes, counts, names); to test
  signed-in behaviour run the check inside the container so it reads its own environment.
- **Run `/security-review`** before merging anything that touches sign-in, sessions, users or
  permissions.
- **Update the docs in the same PR** as the change, and keep `CLAUDE.md`'s invariants current.
- **Remove a safety check to prove its test fails.** New guards get a quick mutation check before
  they are trusted.
