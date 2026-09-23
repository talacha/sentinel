"use strict";

// Sentinel client: a small static app that talks to the Sentinel API.
// No build step, no dependencies. All server data is rendered with textContent, never innerHTML.

const CONFIG = window.SENTINEL_CONFIG || {};
const $ = (id) => document.getElementById(id);

const VERDICTS = {
  no_cumple: { label: "Fails", icon: "✕", group: "Failed", order: 0 },
  revisar: { label: "Needs review", icon: "!", group: "Needs review", order: 1 },
  cumple: { label: "Passes", icon: "✓", group: "Passed", order: 2 },
};

const PUBLIC_CHECK = {
  confirmed: "A public source confirmed the reference this rule relies on.",
  contradicted: "A public source contradicts the reference this rule relies on.",
  unclear: "No clear public source was found, so the reference was not assumed.",
  unavailable: "The public check could not be run.",
};

const state = {
  apiBase: resolveApiBase(),
  auth: loadAuth(),
  health: null,
  vaults: [],
  samples: [],
  vaultId: null,
  file: null,
  report: null,
  busy: false,
};

// ------------------------------------------------------------------------------ helpers

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function resolveApiBase() {
  let base = typeof CONFIG.apiBase === "string" ? CONFIG.apiBase : "";
  if (CONFIG.allowApiParam) {
    const fromQuery = new URLSearchParams(location.search).get("api");
    if (fromQuery && /^https?:\/\//i.test(fromQuery)) base = fromQuery;
  }
  return base.replace(/\/+$/, "");
}

function safeHref(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

function toBase64(text) {
  return btoa(String.fromCharCode(...new TextEncoder().encode(text)));
}

const AUTH_KEY = "sentinel.auth";

function loadAuth() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
    return saved && saved.base === resolveApiBase() ? { user: saved.user, pass: saved.pass } : null;
  } catch {
    return null;
  }
}

function saveAuth(auth) {
  try {
    if (auth) sessionStorage.setItem(AUTH_KEY, JSON.stringify({ ...auth, base: state.apiBase }));
    else sessionStorage.removeItem(AUTH_KEY);
  } catch {
    // Storage can be unavailable (private windows); the login then lasts for this page load only.
  }
}

function showError(message) {
  const box = $("error");
  box.textContent = message || "";
  box.hidden = !message;
}

// ------------------------------------------------------------------------------ API

function requestLogin(message) {
  return new Promise((resolve) => {
    const dialog = $("login");
    $("login-msg").textContent = message || "This service asks for a username and password.";
    $("login-pass").value = "";
    const done = (value) => {
      dialog.removeEventListener("cancel", onCancel);
      $("login-form").removeEventListener("submit", onSubmit);
      $("login-cancel").removeEventListener("click", onCancelClick);
      if (dialog.open) dialog.close();
      resolve(value);
    };
    const onSubmit = (event) => {
      event.preventDefault();
      done({ user: $("login-user").value, pass: $("login-pass").value });
    };
    const onCancel = (event) => {
      event.preventDefault();
      done(null);
    };
    const onCancelClick = () => done(null);
    dialog.addEventListener("cancel", onCancel);
    $("login-form").addEventListener("submit", onSubmit);
    $("login-cancel").addEventListener("click", onCancelClick);
    dialog.showModal();
    $("login-user").focus();
  });
}

function withAuth(headers) {
  if (state.auth) headers.set("Authorization", "Basic " + toBase64(`${state.auth.user}:${state.auth.pass}`));
  return headers;
}

// This app's own files (the bundled samples). If the API serves the app behind a login, they are
// protected too, so send the credentials, but only to the API's own origin.
function assetFetch(relativeUrl) {
  const url = new URL(relativeUrl, document.baseURI);
  const apiOrigin = new URL(state.apiBase || location.href, location.href).origin;
  const headers = new Headers();
  if (url.origin === apiOrigin) withAuth(headers);
  return fetch(url, { headers });
}

// fetch() against the API. On a 401 it asks for credentials and retries until it succeeds or the
// person cancels.
async function api(path, options = {}, { login = true } = {}) {
  for (;;) {
    const headers = withAuth(new Headers(options.headers || {}));
    headers.set("Accept", "application/json");
    let resp;
    try {
      resp = await fetch(state.apiBase + path, { ...options, headers });
    } catch {
      throw new Error(
        "Could not reach the review service. Check your connection and try again."
      );
    }
    if (resp.status !== 401 || !login) return resp;
    const creds = await requestLogin(state.auth ? "That username or password was not accepted." : "");
    if (!creds) throw new Error("Sign in is required to use this service.");
    state.auth = creds;
    saveAuth(creds);
  }
}

async function friendly(resp) {
  let detail = "";
  try {
    const data = await resp.json();
    if (typeof data.detail === "string") detail = data.detail;
  } catch {
    // no JSON body
  }
  switch (resp.status) {
    case 400:
      return detail || "That document couldn't be read.";
    case 404:
      return detail || "That review type isn't available.";
    case 413:
      return `That file is too large. The limit is ${state.health?.max_upload_mb ?? 20} MB.`;
    case 429: {
      const seconds = Number(resp.headers.get("Retry-After")) || 0;
      const wait = seconds >= 90 ? `about ${Math.ceil(seconds / 60)} minutes` : "a moment";
      return `The review limit for this service has been reached. Please try again in ${wait}.`;
    }
    case 503:
      return "The review service isn't fully set up yet. Please try again later.";
    default:
      return detail || `Something went wrong (HTTP ${resp.status}).`;
  }
}

async function loadHealth() {
  try {
    const resp = await api("/healthz", {}, { login: false });
    if (!resp.ok) return;
    state.health = await resp.json();
  } catch {
    return;
  }
  const privacy = $("privacy");
  const { health } = state;
  if (health.max_upload_mb) $("max-mb").textContent = String(health.max_upload_mb);
  if (health.llm_host) {
    privacy.textContent = "";
    privacy.append("Your document is processed by ", h("strong", {}, health.llm_host), ". Nothing from it is sent anywhere else.");
  } else if (health.auth_required && !state.auth) {
    privacy.textContent = "Sign in to continue.";
  } else if (health.llm_configured === false) {
    privacy.textContent = "The review service is not configured yet.";
  } else {
    privacy.textContent = "";
  }
}

// ------------------------------------------------------------------------------ setup form

function updateRunState() {
  $("run").disabled = state.busy || !(state.vaultId && state.file);
}

function renderVaults() {
  const box = $("vaults");
  box.replaceChildren();
  if (!state.vaults.length) {
    box.append(h("p", { class: "muted" }, "No review types are available."));
    return;
  }
  for (const vault of state.vaults) {
    const input = h("input", { type: "radio", name: "vault", value: vault.id, checked: vault.id === state.vaultId });
    input.addEventListener("change", () => {
      state.vaultId = vault.id;
      updateRunState();
    });
    box.append(
      h("label", { class: "choice" }, input,
        h("span", { class: "choice-title" }, vault.title),
        h("span", { class: "choice-desc" }, vault.description),
        h("span", { class: "choice-meta" }, `${vault.rules.length} checks`))
    );
  }
  if (state.vaults.length === 1) {
    state.vaultId = state.vaults[0].id;
    box.querySelector("input").checked = true;
  }
}

async function loadVaults() {
  const resp = await api("/v1/vaults");
  if (!resp.ok) throw new Error(await friendly(resp));
  state.vaults = await resp.json();
  renderVaults();
  updateRunState();
  await loadHealth(); // refresh after a login so the privacy note can show the model host
}

async function loadSamples() {
  try {
    const resp = await assetFetch("samples/index.json");
    if (!resp.ok) return;
    const known = new Set(state.vaults.map((v) => v.id));
    state.samples = (await resp.json()).filter((s) => known.has(s.vault));
  } catch {
    return;
  }
  const buttons = $("sample-buttons");
  buttons.replaceChildren();
  for (const sample of state.samples) {
    const label = sample.note ? `${sample.label} (${sample.note})` : sample.label;
    const button = h("button", { type: "button", class: "chip" }, label);
    button.addEventListener("click", () => runSample(sample));
    buttons.append(button);
  }
  $("samples").hidden = state.samples.length === 0;
}

function setFile(file) {
  state.file = file || null;
  $("drop").classList.toggle("has-file", Boolean(file));
  $("drop-title").textContent = file ? file.name : "Drop a PDF or text file here, or click to choose";
  $("drop-hint").hidden = Boolean(file);
  updateRunState();
  if (file && state.health?.max_upload_mb && file.size > state.health.max_upload_mb * 1024 * 1024) {
    showError(`That file is too large. The limit is ${state.health.max_upload_mb} MB.`);
  } else if (file) {
    showError("");
  }
}

async function runSample(sample) {
  if (state.busy) return;
  showError("");
  try {
    const resp = await assetFetch(`samples/${sample.file}`);
    if (!resp.ok) throw new Error("The sample could not be loaded.");
    const text = await resp.text();
    state.vaultId = sample.vault;
    renderVaults();
    setFile(new File([text], sample.file, { type: "text/plain" }));
  } catch (err) {
    showError(err.message);
    return;
  }
  await runReview();
}

// ------------------------------------------------------------------------------ review

function setBusy(busy, text) {
  state.busy = busy;
  $("bar").hidden = !busy;
  $("progress").textContent = text || "";
  updateRunState();
}

async function runReview() {
  if (state.busy || !state.vaultId || !state.file) return;
  showError("");
  $("results").hidden = true;
  const vault = state.vaults.find((v) => v.id === state.vaultId);
  const started = Date.now();
  const message = () =>
    `Checking ${vault ? vault.rules.length : ""} rules… ${Math.round((Date.now() - started) / 1000)}s`;
  setBusy(true, message());
  const timer = setInterval(() => ($("progress").textContent = message()), 1000);
  try {
    const body = new FormData();
    body.append("vault_id", state.vaultId);
    body.append("file", state.file, state.file.name);
    const resp = await api("/v1/reviews", { method: "POST", body });
    if (!resp.ok) throw new Error(await friendly(resp));
    renderReport(await resp.json());
  } catch (err) {
    showError(err.message);
  } finally {
    clearInterval(timer);
    setBusy(false);
  }
}

// ------------------------------------------------------------------------------ results

function evidenceList(evidence) {
  return h("div", { class: "evidence" },
    h("div", { class: "label" }, "Evidence from your document"),
    evidence.map((e) =>
      h("blockquote", {}, e.quote, e.page ? h("span", { class: "page" }, `page ${e.page}`) : null)));
}

function publicCheck(external) {
  const sources = external.sources.map((s) => {
    const href = safeHref(s.url);
    return h("li", {},
      href ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, s.title || s.url) : (s.title || s.url),
      h("span", { class: "muted" }, ` (${s.role})`));
  });
  return h("div", { class: "external" },
    h("div", { class: "label" }, "Checked against public sources"),
    h("p", {}, PUBLIC_CHECK[external.status] || external.status),
    external.query
      ? h("p", { class: "muted" }, "The only thing sent outside was this search: ", h("code", {}, external.query))
      : null,
    sources.length ? h("ul", {}, sources) : null);
}

function ruleCard(result) {
  const v = VERDICTS[result.verdict];
  return h("article", { class: `rule v-${result.verdict}` },
    h("div", { class: "rule-head" },
      h("h4", {}, result.title),
      h("span", { class: `badge v-${result.verdict}` }, h("span", { "aria-hidden": "true" }, v.icon), ` ${v.label}`)),
    h("p", { class: "rationale" }, result.rationale),
    result.evidence.length ? evidenceList(result.evidence) : null,
    result.external ? publicCheck(result.external) : null);
}

function renderReport(report) {
  state.report = report;
  const { summary } = report;
  const banner = $("banner");
  banner.className = "banner " + (summary.no_cumple ? "v-no_cumple" : summary.revisar ? "v-revisar" : "v-cumple");
  $("results-title").textContent = summary.no_cumple
    ? "Issues found"
    : summary.revisar ? "Needs human review" : "All checks passed";
  const total = report.results.length;
  $("results-sub").textContent = summary.no_cumple || summary.revisar
    ? `${summary.no_cumple} of ${total} checks failed and ${summary.revisar} need a person to look.`
    : `All ${total} checks passed, each with evidence from your document.`;
  $("counts").replaceChildren(
    ...["no_cumple", "revisar", "cumple"].map((key) =>
      h("li", { class: `pill v-${key}` }, h("strong", {}, String(summary[key])), ` ${VERDICTS[key].group.toLowerCase()}`)));
  $("meta").textContent =
    `${report.document.filename} · ${report.document.pages} page${report.document.pages === 1 ? "" : "s"} · ` +
    `${report.vault_title} · fingerprint ${report.document.sha256.slice(0, 12)}`;

  const groups = $("groups");
  groups.replaceChildren();
  for (const key of ["no_cumple", "revisar", "cumple"]) {
    const items = report.results.filter((r) => r.verdict === key);
    if (!items.length) continue;
    const section = h("details", { class: `group v-${key}`, open: key !== "cumple" },
      h("summary", {}, h("span", { class: `badge v-${key}` }, VERDICTS[key].group), ` ${items.length}`),
      h("div", { class: "rules" }, items.map(ruleCard)));
    groups.append(section);
  }
  $("results").hidden = false;
  $("results").focus({ preventScroll: true });
  $("results").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

function downloadReport() {
  if (!state.report) return;
  const blob = new Blob([JSON.stringify(state.report, null, 2)], { type: "application/json" });
  const link = h("a", { href: URL.createObjectURL(blob), download: `sentinel-${state.report.document.sha256.slice(0, 12)}.json` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function reset() {
  state.report = null;
  $("results").hidden = true;
  $("file").value = "";
  setFile(null);
  showError("");
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------------------ start

let openedForPrint = [];
window.addEventListener("beforeprint", () => {
  openedForPrint = [...document.querySelectorAll("details:not([open])")];
  for (const d of openedForPrint) d.open = true;
});
window.addEventListener("afterprint", () => {
  for (const d of openedForPrint) d.open = false;
  openedForPrint = [];
});

async function init() {
  $("form").addEventListener("submit", (event) => {
    event.preventDefault();
    runReview();
  });
  $("file").addEventListener("change", () => setFile($("file").files[0]));
  $("download").addEventListener("click", downloadReport);
  $("print").addEventListener("click", () => window.print());
  $("again").addEventListener("click", reset);

  const drop = $("drop");
  for (const type of ["dragenter", "dragover"]) {
    drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.remove("over"); });
  }
  drop.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
  });

  await loadHealth();
  try {
    await loadVaults();
    await loadSamples();
  } catch (err) {
    $("vaults").replaceChildren(h("p", { class: "muted" }, "Review types could not be loaded."));
    showError(err.message);
  }
}

init();
