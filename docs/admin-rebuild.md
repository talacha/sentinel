# Admin experience rebuild: plan and tasks

Status: **planned, not started** (written 2026-09-24). Nothing here has been built. This is the
detailed plan for track A of the [roadmap](roadmap.md); read that first for the current state of the
project and how to resume.

The goal, in one line: **one consistent product shell with a menu that follows you across every
screen, built from shared components, so every screen looks and behaves like the others.**

Size legend: **S** is about a day, **M** two to three days, **L** four to six days, for one person.
Task IDs (`A1.3`) are stable so PRs, commits and notes can refer to them.

## 1. Why: what feels fractured today

The console grew one request at a time (status, configuration, users, vaults), and each addition
was made consistent with the one before it, not with a whole. These are the concrete fractures,
each verified in the code on `main`:

| # | Fracture | Evidence | Fixed in |
| --- | --- | --- | --- |
| 1 | **Three front-ends, no common shell.** | The built-in UI (`ui/index.html`, `ui/app.js`, `ui/styles.css`, about 390 lines), the client app (`client/`, about 760 lines), and the console (`ui/console.html`, about 1,200 lines, all CSS and JS inline). None links to another: a signed-in admin cannot get from the console to the review screen, or back. | A1, A5 |
| 2 | **Three ways to sign in.** | The built-in UI relies on the browser's native Basic prompt (no JS auth, no way to sign out). The client keeps credentials in `sessionStorage`. The console keeps them in memory only, so a reload signs you out. One person can be asked to sign in three times. | A3 |
| 3 | **A menu exists on one of the three, and it is minimal.** | The console has a five-link top bar. It is not grouped, not role-aware (a visitor is not shown a smaller menu, they are refused), has no account menu, no signal that something needs attention, and scrolls sideways on a phone. | A1 |
| 4 | **Four interaction patterns for the same job.** | Users: add and edit in a modal dialog. Vaults: a full-page editor with Validate and Save. Configuration: one long form with a sticky save bar and per-field errors. Status: buttons that run checks. Feedback is sometimes a banner, sometimes an inline error box, sometimes a dialog, and confirmation is only the browser's `confirm()` (endpoint change, discarding edits). | A2 |
| 5 | **No reuse, hard to test, and it weakens the CSP.** | Tables, dialogs, error lists and form rows are copy-pasted inside one file. Inline script and style force `'unsafe-inline'` in the console's Content-Security-Policy. | A1, A2 |
| 6 | **The design is copied three times.** | Each front-end defines its own `:root` colour tokens. They will drift. | A0, A5 |
| 7 | **The console feels incomplete.** | The audit log is written but can never be read in the product. Users and vaults can be added but not removed or disabled. Nobody can change their own password. No version diff between vault versions. And nothing shows which version is running: we could not tell that the live site was two PRs behind `main` until URLs 401'd. | A4 |
| 8 | **The vocabulary is inconsistent.** | "Configuration" (page) vs "settings" (docs) vs `/admin` (old URL); "visitor" (role) vs "judge" (user name) vs "reviewer"; "vault" vs "policy" in the copy. | A0 |
| 9 | **Quality is unproven beyond one browser.** | Checked in Chrome only. No accessibility audit (keyboard, screen reader, contrast). The browser test harness that verified every page is **not in the repository** (see [roadmap: tooling to rescue](roadmap.md#tooling-to-rescue-do-this-first)). | A0, A6 |

## 2. Goals and non-goals

**Goals**

1. One shell for everything a signed-in person does, with **one menu in the same place with the
   same behaviour on every screen**.
2. Every screen is composed from the same small set of components.
3. The same pattern for the same job: list, detail, create, edit, delete, validate, confirm,
   report success, report an error.
4. The menu is role-aware: a visitor sees a small menu (Review, Account), an admin sees everything.
5. Sign in once for the whole product.
6. The admin can see and do what the product implies: read the audit log, remove what they added,
   change their own password, see which version is running.
7. Accessible (WCAG 2.2 AA as the target) and proven by automated and manual checks.
8. Every step ships on its own and leaves `main` working and deployable.

**Non-goals**

- Storing review documents or reports. That would break the core invariant (document text never
  leaves the perimeter and is never stored). "Review history" is deliberately not a feature.
- Multi-tenant or fine-grained roles beyond `admin` and `visitor`.
- A framework migration, unless decision D1 says so.
- Translating the UI. (Vaults have a `language` field; the UI is English only. Revisit later.)

## 3. Target experience

### 3.1 The shell

```
Desktop
+-----------------------------------------------------------------------------+
| Sentinel      sntnl.cc                              Ready    admin (menu v) |
+--------------------+--------------------------------------------------------+
| REVIEW             |  Console > Vaults > insurance_life                     |
|   New review       |  Life insurance underwriting          [Edit]  [More v] |
|                    |  ------------------------------------------------------|
| MANAGE             |  (messages: banner / toasts)                           |
|   Overview         |                                                        |
|   Vaults        3  |  page content built from the shared components         |
|   Users         2  |                                                        |
|   Settings         |                                                        |
|   Status      ! 1  |                                                        |
|   Audit log        |                                                        |
+--------------------+--------------------------------------------------------+
| v0.1.0 . 628d658 . deployed 2 hours ago                                     |
+-----------------------------------------------------------------------------+

Phone: a top bar with a menu button (opens a drawer with the same menu), the page title, and the
account menu. The drawer traps focus and closes on Escape or when a link is chosen.
```

Rules that make it consistent:

- The **same shell wraps every screen**: header, menu, page area, message area, footer. Nothing
  renders outside it, including sign-in, errors and "not allowed".
- The **menu is data, not markup** (one definition, rendered per role). Adding a screen means adding
  one entry.
- The **current item is always marked** (`aria-current`), including on child screens (a vault's
  page keeps "Vaults" highlighted).
- **Badges carry attention**: a count for lists, a warning mark on Status when the last checks were
  not ready. Nothing else in the menu changes.
- The **account menu** (top right) shows who you are and your role, and holds Change password,
  About (version), and Sign out. It is the only place those live.
- The **footer shows the running build** (version, commit, deploy age) so "is the live site up to
  date?" can be answered at a glance.
- Menu behaviour: works with the keyboard (arrows, Home, End, Enter), links open in a new tab, the
  unsaved-changes guard applies to menu clicks exactly as it does to breadcrumbs and Back.

### 3.2 Menu contents by role

| Item | Route | Visitor | Admin | Notes |
| --- | --- | :---: | :---: | --- |
| New review | `/review` (see D3) | yes | yes | The product's main action. |
| Overview | `/admin` | | yes | Cards with live one-line summaries, as today. |
| Vaults | `/admin/vaults` | | yes | List, view, edit, new (child screens keep it highlighted). |
| Users | `/admin/users` | | yes | |
| Settings | `/admin/config` | | yes | Renamed from Configuration if D4 agrees; the URL stays. |
| Status | `/status` | | yes | Warning badge when not ready. |
| Audit log | `/admin/audit` | | yes | New (A4.1). |
| Account: Change password | menu | yes | yes | New (A3.4). |
| Account: About | menu | yes | yes | Version and build (A4.6). |
| Account: Sign out | menu | yes | yes | Works the same on every screen. |

Every existing URL keeps working (redirects where a route moves), as the earlier renames did.

### 3.3 Anatomy of every screen

1. **Page header**: breadcrumb, title, and the primary action on the right (`New vault`, `Add user`).
2. **Message area**: one place for banners and toasts; errors that block the page appear here.
3. **Content**: built only from shared components.
4. **Footer actions** on forms: primary action first on the left, then secondary, then Cancel;
   sticky when the form is long.

### 3.4 One pattern per job

| Job | Pattern |
| --- | --- |
| Browse | **Table** with a row action, an empty state that says what to do next, and a loading skeleton. |
| Read | **Detail** screen with facts, related tables, and one primary action. |
| Create and edit | The **same form component and the same screen shape** for both (today Users is a dialog and Vaults a page; pick one, see A2.4). Errors inline next to the field plus a summary that links to each field. |
| Validate before saving | A **Validate** button wherever a save can fail on content (vaults today), with the same result panel. |
| Change something risky | A **confirm dialog** that names the effect ("this changes where documents are sent"), used the same way everywhere. Irreversible actions ask you to type the name. |
| Show a secret once | The **once-only secret** component (generated passwords today): copy button, wiped on close. |
| Report success | A toast or banner naming what changed, in the same words pattern ("`ops` added as visitor"). |
| Report an error | Inline first, then a summary, then the message area for page-level failures. Wording templates, no raw HTTP codes unless useful. |

### 3.5 Components (inventory for A2)

`AppShell`, `Menu`, `AccountMenu`, `PageHeader`, `Breadcrumbs`, `Button` (primary, secondary,
danger, link), `Table`, `EmptyState`, `Skeleton`, `Form`, `Field`, `TextInput`, `Select`,
`Textarea`, `CodeEditor` (a textarea with line numbers is enough to start), `Badge`, `Chip`,
`Notice`, `Toast`, `Dialog`, `ConfirmDialog`, `OnceOnlySecret`, `Tabs` (only if a screen needs
them), `Pagination` (audit log). Each is documented with its states in a component gallery page
that the browser tests screenshot.

### 3.6 Sign-in and account

- One sign-in screen for every role, inside the shell, returning you to the page you asked for.
- What "signed in" means (memory, `sessionStorage`, or a server session) is decision **D2**. Whatever
  is chosen must work for `/`, `/app/` and the console, and keeps HTTP Basic working for scripts.
- Friendly failure: wrong password, locked out (with the time from `Retry-After`), session ended
  (your unsaved edits are kept and offered back after signing in).
- "My account": see your role, change your own password (asks for the current one), sign out.
- A visitor who opens an admin URL sees a clear "admins only" screen with a link to Review, not a
  sign-in loop.

### 3.7 Accessibility baseline

Skip link and landmarks on every screen; focus moves to the page title on navigation; visible focus
everywhere; every control reachable and operable by keyboard; dialogs trap focus and restore it;
colour contrast at least AA in both themes; no information by colour alone; live regions for
toasts; reduced-motion respected; touch targets at least 44 px on phones.

## 4. Decisions to make first

Each is written up as a short ADR in A0.3. The recommendation is a starting point, not a decision.

| ID | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| D1 | Front-end architecture | (a) no-build ES modules served from `/static`; (b) a small framework (Preact, Lit, Svelte) with a build step | **(a).** Keeps self-hosting to "clone and run", allows a strict CSP with no inline code, and the app is small. Revisit only if components become painful. |
| D2 | What "signed in" means | (a) keep credentials in memory; (b) `sessionStorage`; (c) server session cookie (`HttpOnly`, `SameSite=Strict`, idle and absolute timeouts, CSRF header), Basic kept for API clients | **(c)** for one sign-in across all screens that survives a reload, with (a) as the fallback if a security review dislikes cookies. A1 and A2 must not depend on the answer (hide it behind one `auth` module). |
| D3 | One front-end or three | (a) one shell that includes the review flow, retire `ui/index.html`, keep `client/` standalone (it can be hosted elsewhere with `?api=`) on shared tokens and the same menu labels; (b) merge everything; (c) leave as is | **(a).** The client's "hosted anywhere" use is real; the built-in UI is redundant. |
| D4 | Vocabulary | Vault or policy; visitor or reviewer; Configuration or Settings | Keep **vault** (it is the product's word), rename the role to **reviewer** in the UI only (API values stay), **Settings**. Write the word list once. |
| D5 | Menu layout | (a) left sidebar (desktop) and drawer (phone); (b) top bar | **(a).** It scales past five items and groups Review and Manage. |
| D6 | Removing things | soft delete or archive vs hard delete | **Archive** added vaults (restorable), **disable** users; hard delete only for users with confirmation. |
| D7 | Test stack | jsdom for logic plus real Chrome for layout; Playwright or `puppeteer-core` | Keep both layers. **Playwright** for the real-browser layer (built-in traces, screenshots, axe integration). |
| D8 | Deploy path | manual with a script; a pull-based updater on the VM; a GitHub Action over SSH | **Script plus a visible build version** first (A4.6). The firewall only allows the owner's IP for SSH, which rules out runner-based deploys without a bastion. |

## 5. Milestones and tasks

Order matters: each milestone builds on the last. Tasks are small enough for one PR each unless
noted. A task is done only when its tests pass, the docs are updated in the same PR, and (for
anything visual) it has been checked in a real browser with screenshots in the PR.

### A0. Decide, and make the work testable  (S to M, no user-visible change)

Goal: nothing is built on guesses, and the safety net exists before the first line of UI moves.

- [ ] **A0.1** Port the browser test harness into the repo as `tests/e2e/`: shared helpers, one suite
  per page (overview and navigation, configuration, users, vaults), and the real-Chrome run. One
  documented command runs it locally (it starts its own throwaway server on a free port with a
  temporary state directory). Baseline to preserve: about 190 jsdom checks and the real-Chrome
  checks (layout, `[hidden]`, CSP violations, redirects, dark mode, phone width).
- [ ] **A0.2** Run it in CI as a second job (GitHub-hosted runners include Chrome). Keep `test` as the
  required check; add the e2e job as required once it is stable for a week.
- [ ] **A0.3** Screenshots (light, dark, phone) of every screen saved as a CI artifact, for review.
- [ ] **A0.4** Write the ADRs for D1 to D8 in `docs/adr/`, with the decision and the reason.
- [ ] **A0.5** Screen inventory and low-fidelity wireframes for the shell, menu (desktop, phone), list,
  detail, form, empty, loading, error, forbidden, sign-in, and account menu (`docs/admin-ux/`).
  The user reviews these before A1 starts.
- [ ] **A0.6** Design tokens spec: colours for both themes with measured contrast, type scale, spacing,
  radius, elevation, focus ring. One file becomes the source of truth in A5.3.
- [ ] **A0.7** Vocabulary sheet (D4) and the menu definition table (section 3.2) finalised.

**Done when:** the e2e suite passes in CI on `main`; ADRs and wireframes are merged and approved.

### A1. App shell and the menu  (M to L)

Goal: the menu experience, without changing what any page does.

- [ ] **A1.1** Move the console out of one inline file into real assets served from `/static/`
  (HTML shell, one stylesheet, ES modules). Tighten the CSP to `script-src 'self'; style-src 'self'`
  (drop `'unsafe-inline'`), update `_CONSOLE_HEADERS`, and update the CSP tests. Confirm in Chrome
  that nothing is blocked.
- [ ] **A1.2** The shell: sidebar (desktop) and drawer (phone), header with breadcrumb and title
  slots, page area, message area, footer with a build-info placeholder, skip link, landmarks.
- [ ] **A1.3** `menu.js`: the menu as data (sections, items, roles, badges, routes), rendered by role;
  current-item marking including child routes; keyboard navigation; mobile drawer with focus trap.
- [ ] **A1.4** Router module: route table, parameters, page lifecycle (`mount`, `unmount`, `dirty`),
  breadcrumb and title from route metadata, focus and scroll handling, unknown-route screen. The
  unsaved-changes guard lives here, so it applies to the menu, breadcrumbs, Back and tab close alike.
- [ ] **A1.5** Global screen states: loading, error, empty, and **forbidden** (a visitor on an admin URL
  sees "admins only" with a link to Review, not a sign-in loop).
- [ ] **A1.6** Account menu (user and role, Sign out) with placeholders for Change password and About.
- [ ] **A1.7** Move the five existing pages into the shell **without changing their behaviour**. Every
  existing e2e check still passes.
- [ ] **A1.8** New e2e: the menu per role, keyboard use, the phone drawer, deep links, the guard from
  the menu, and screenshots.

**Done when:** every console screen shows the same menu in the same place with the same behaviour;
no inline script or style remains; the e2e suite and the updated CSP test pass; Chrome logs no
violations.

### A2. Components and consistent patterns  (L)

Goal: every screen is built from the same parts, and the same job looks the same everywhere.

- [ ] **A2.1** Build the component set (section 3.5) with their states (default, hover, focus,
  disabled, loading, error).
- [ ] **A2.2** A static component gallery page, used by the e2e suite for screenshots of each
  component and state.
- [ ] **A2.3** The standard flows as helpers: list, detail, create and edit form, delete confirmation.
- [ ] **A2.4** Migrate **Users** to the pattern. Decide dialog vs page so that add and edit look the
  same as Vaults (recommendation: one full-page form for both).
- [ ] **A2.5** Migrate **Settings** (form, sticky save bar as a shared component, "overridden" badge,
  "reset to environment value").
- [ ] **A2.6** Migrate **Vaults** (list, detail, editor with Validate) and **Status**.
- [ ] **A2.7** Unify feedback: success wording templates, inline errors plus a linked summary, the
  same confirm dialog for risky changes (endpoint change today), no raw `confirm()`.
- [ ] **A2.8** Delete the bespoke CSS and markup left behind, and add a test that fails if a page
  defines its own button, dialog or colour.

**Done when:** every page is composed only of shared components; create and edit look identical
across Users and Vaults; visual snapshots are updated; keyboard flows pass.

### A3. Sign-in and account  (M to L, security sensitive; needs D2)

Goal: sign in once, everywhere, and manage your own account.

- [ ] **A3.1** Implement the chosen session model (for the recommendation: `POST /v1/session`,
  `HttpOnly` `SameSite=Strict` cookie, idle and absolute timeouts, logout, the existing custom
  header as CSRF defence). HTTP Basic keeps working for scripts. The admin failure throttle and
  its "check before hashing" order stay intact.
- [ ] **A3.2** One sign-in screen for every role, returning you to the page you asked for; clear
  wrong-password and locked-out messages using `Retry-After`.
- [ ] **A3.3** The session survives a reload; when it expires the shell says so and keeps unsaved
  edits so they can be resumed after signing in.
- [ ] **A3.4** My account: change your own password (requires the current one), see your role, sign
  out. (`POST /v1/me/password`, audited, no plaintext anywhere.)
- [ ] **A3.5** Tests for fixation, CSRF, expiry, logout, throttle behaviour, and that no credential
  or session secret appears in any response, log or audit event.
- [ ] **A3.6** Update `docs/reference.md` (security model) and the invariants in `CLAUDE.md`.
- [ ] **A3.7** Run `/security-review` on the branch before merging; treat any finding as blocking.

**Done when:** signing in once covers `/`, `/app/` and the console, survives a reload, and the
security review is clean.

### A4. Complete the admin capabilities  (L; every task ships on its own)

Goal: the console can do what the product implies.

- [ ] **A4.1** **Audit log viewer.** `GET /v1/admin/audit` (admin only, newest first, cursor
  pagination, filter by event and admin) and an Audit log screen. It returns only what is already
  stored (names, hashes, non-secret values); tests assert that document text and secrets can never
  appear.
- [ ] **A4.2** **Users: disable, enable, delete.** Rules: never yourself, never the last admin, never
  the last visitor, never an environment-defined user. Confirm dialog; audited. Update the
  invariant in `CLAUDE.md`, which currently says not to add this.
- [ ] **A4.3** **Vaults: archive and restore** vaults added in the console (D6), with a confirm that
  asks for the id. Shipped vaults are never touched.
- [ ] **A4.4** **Vault version diff** (unified view) and **Restore this version** (saved as a new
  version, so nothing is lost).
- [ ] **A4.5** **Duplicate a vault** as a new one, and **export and import** vault YAML.
- [ ] **A4.6** **About and build info.** The running version, git commit (Docker build argument),
  start time and model host, shown in the shell footer and the About screen; exposed to admins in
  `/healthz`. Add `scripts/deploy.sh` that performs the documented update and prints the before and
  after commit.
- [ ] **A4.7** **Status improvements:** a "how to fix" hint per failing check, the time of the last
  live check kept, and the warning badge on the menu when not ready.

**Done when:** each task has API tests, e2e coverage, docs, and preserves the invariants.

### A5. One product, one menu  (M; needs D3)

Goal: from any screen you can reach every screen you are allowed to, through the same menu.

- [ ] **A5.1** Add the **Review** section to the shell: choose a vault, upload, see the report. It
  reuses the shared components and the client's proven flow.
- [ ] **A5.2** Retire the built-in UI (`ui/index.html`, `ui/app.js`): `/` redirects into the shell.
  Keep `client/` as the standalone, hostable-anywhere client, restyled on the shared tokens and
  using the same menu labels, with links into the console for signed-in admins.
- [ ] **A5.3** One source of design tokens (`ui/shared/tokens.css`) used by every front-end, with a
  test that no page defines its own palette.
- [ ] **A5.4** Role-aware landing: a visitor lands on Review, an admin on Overview.
- [ ] **A5.5** Update the README, screenshots and the demo script.

**Done when:** there is one menu model across the product and no duplicate front-end remains except
the intentional standalone client.

### A6. Quality bar and polish  (M)

Goal: prove it is good, and keep it that way.

- [ ] **A6.1** Accessibility: axe in the e2e suite on every screen in both themes (zero serious or
  critical), a keyboard-only walkthrough of every flow, and a VoiceOver smoke test.
- [ ] **A6.2** Responsive matrix (phone, tablet, desktop) in the e2e screenshots; touch targets.
- [ ] **A6.3** Performance: no layout shift on load, lazy-load page modules, a size budget for the
  shell.
- [ ] **A6.4** Visual regression in CI (screenshot diffs with a tolerance) once the design is stable.
- [ ] **A6.5** Help: each screen links to its section of the reference, and empty states say what to
  do next.
- [ ] **A6.6** Rewrite the console section of `docs/reference.md` to describe the final IA, with
  screenshots.

**Done when:** axe is clean, every flow passes the keyboard walkthrough, and the docs match the
product.

## 6. Constraints that hold in every milestone

These come from `CLAUDE.md` and from problems found while building the current console.

- **Secrets stay write-only.** No API response, page, error, log or audit event ever contains a
  password, hash, key or session secret. Validation errors never echo the request.
- **Shells hold no data.** Pages are data-free HTML; data comes from the authenticated API.
- **The console loads nothing from outside its own origin** (no CDN, no web fonts) and stays under
  its CSP. After A1.1 that CSP has no `'unsafe-inline'`.
- **Untrusted text is never inserted as markup.** No `innerHTML`, no `eval`; a test enforces it today.
- **Admin writes** need the `X-Sentinel-Admin` header and JSON.
- **Author CSS must not un-hide `[hidden]` elements.** Until components replace it, keep the global
  `[hidden]` rule. (jsdom cannot see this class of bug; check layout in a real browser.)
- **Every risky change is audited by name or hash**, never by content.
- **Existing URLs keep working**, with redirects.
- **Each PR** targets `main` (never another PR's branch), passes CI, updates the docs, and for visual
  changes includes real-browser screenshots. After merging anything user-facing, remember that
  **merging does not deploy** (see the roadmap's deploy notes).

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| The rebuild competes with the hackathon deadline. | Milestones ship independently; see the roadmap's sequencing and its feature freeze. A3 to A6 can wait until after submission. |
| Auth changes are security sensitive. | Isolate them in A3 behind one `auth` module, keep Basic for scripts, run `/security-review`, and do not bundle with UI work. |
| Tightening the CSP breaks something at the end. | Do it first (A1.1), while the e2e suite can show exactly what breaks. |
| A "big bang" rewrite. | Migrate page by page under the same URLs; A1.7 changes structure only, so behaviour diffs are visible. |
| Browser tests are flaky in CI. | Pin the browser, wait on state not time, retry once, keep the jsdom layer for logic. |
| Front-ends drift again. | A5.3: one token source and a test that fails on a private palette. |

## 8. Suggested PR breakdown

About 4 PRs for A0, 4 for A1, 6 to 8 for A2, 4 for A3, 7 for A4, 3 for A5, 3 for A6: roughly 30 PRs,
each independently reviewable and revertible.
