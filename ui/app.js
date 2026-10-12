"use strict";

// All server data is rendered with textContent (never innerHTML).

const VERDICTS = {
  cumple: { symbol: "✓", gloss: "passes" },
  no_cumple: { symbol: "✕", gloss: "fails" },
  revisar: { symbol: "!", gloss: "needs human review" },
};

// A report is read to find the exceptions, so the screen leads with them. The downloaded
// JSON keeps the vault's own rule order, which is the stable one for a record.
const VERDICT_RANK = { no_cumple: 0, revisar: 1, cumple: 2 };
const SEVERITY_RANK = { high: 0, medium: 1, low: 2 };

function triageOrder(results) {
  return [...results].sort((a, b) =>
    (VERDICT_RANK[a.verdict] - VERDICT_RANK[b.verdict]) ||
    (SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]));
}

const SHIELD = {
  dedicated: "M12 2.5 4.5 5.5v6c0 4.5 3.1 8.6 7.5 10 4.4-1.4 7.5-5.5 7.5-10v-6Z|m9 11.8 2.1 2.2L15.2 10",
  warn: "M12 2.5 4.5 5.5v6c0 4.5 3.1 8.6 7.5 10 4.4-1.4 7.5-5.5 7.5-10v-6Z|M12 8.2v4.1|M12 15.6h.01",
  none: "M12 2.5 4.5 5.5v6c0 4.5 3.1 8.6 7.5 10 4.4-1.4 7.5-5.5 7.5-10v-6Z|M9.2 9.2h5.6",
};

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(paths) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  node.setAttribute("viewBox", "0 0 24 24");
  node.setAttribute("fill", "none");
  node.setAttribute("stroke", "currentColor");
  node.setAttribute("stroke-width", "1.5");
  node.setAttribute("stroke-linecap", "round");
  node.setAttribute("stroke-linejoin", "round");
  node.setAttribute("aria-hidden", "true");
  for (const d of paths.split("|")) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    node.append(path);
  }
  return node;
}

function safeHref(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

let vaults = [];
let samples = [];
let lastReport = null;

// What the operator is told about where document text goes. The server classifies the endpoint
// (Settings.llm_perimeter); the UI only renders that verdict, and never softens "unknown".
function perimeterCopy(health) {
  const host = health.llm_host || "the configured endpoint";
  const verification = health.search_configured
    ? "Scoped verification is on: only a templated query from the vault leaves, never the document."
    : "Scoped verification is off: rules needing a public source resolve to revisar.";
  if (!health.llm_configured) {
    return { kind: "none", icon: SHIELD.none, headline: "No model endpoint configured",
      detail: "Set LLM_BASE_URL and LLM_MODEL before running a review.", verification: null };
  }
  if (health.llm_perimeter === "dedicated") {
    return { kind: "dedicated", icon: SHIELD.dedicated, headline: "Dedicated perimeter",
      detail: ["This document is sent only to ", host, " — an address that is not reachable from the public internet. Nothing else leaves."],
      verification };
  }
  if (health.llm_perimeter === "shared") {
    return { kind: "shared", icon: SHIELD.warn, headline: "Shared endpoint",
      detail: ["Document text is sent to ", host, " — a multi-tenant service. Use synthetic documents only, never client data."],
      verification };
  }
  return { kind: "unknown", icon: SHIELD.warn, headline: "Endpoint not verified",
    detail: ["Sentinel cannot confirm that ", host, " is compute you control. Verify it before sending client data."],
    verification };
}

function renderPerimeter(copy) {
  const box = $("perimeter");
  box.className = `perimeter ${copy.kind}`;
  const detail = h("p", { class: "detail" });
  if (Array.isArray(copy.detail)) {
    const [before, host, after] = copy.detail.length === 3 ? copy.detail : ["", copy.detail[0], copy.detail[1]];
    detail.append(before, h("code", {}, host), after);
  } else {
    detail.append(copy.detail);
  }
  box.replaceChildren(
    svg(copy.icon),
    h("div", {},
      h("p", { class: "headline" }, copy.headline),
      detail,
      copy.verification ? h("p", { class: "detail" }, copy.verification) : null));
}

async function loadStatus() {
  try {
    const health = await (await fetch("/healthz")).json();
    renderPerimeter(perimeterCopy(health));
    $("file-limits").textContent =
      `PDF or plain text, up to ${health.max_upload_mb} MB. Scanned PDFs are not read: there is no OCR.`;
  } catch {
    renderPerimeter({ kind: "none", icon: SHIELD.none, headline: "Status unavailable",
      detail: "Could not reach the server to report where documents are sent.", verification: null });
  }
}

async function loadVaults() {
  const select = $("vault");
  try {
    const resp = await fetch("/v1/vaults");
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    vaults = await resp.json();
  } catch (err) {
    showError(`Could not load vaults: ${err.message}`);
    return;
  }
  select.replaceChildren(...vaults.map((v) => h("option", { value: v.id }, v.title)));
  renderVault();
  updateRunState();
}

function renderVault() {
  const vault = vaults.find((v) => v.id === $("vault").value);
  $("vault-desc").textContent = vault ? vault.description : "";
  const list = $("vault-rules-list");
  const rules = vault ? vault.rules : [];
  list.replaceChildren(...rules.map((r) =>
    h("li", {}, h("span", { class: `sev ${r.severity}` }, r.severity), h("span", {}, r.title))));
  $("vault-rules-summary").textContent =
    rules.length === 1 ? "1 rule in this vault" : `${rules.length} rules in this vault`;
  $("vault-rules").hidden = !vault;
  renderSamples();
}

async function loadSamples() {
  try {
    const resp = await fetch("/v1/samples");
    samples = resp.ok ? await resp.json() : [];
  } catch {
    samples = [];
  }
  renderSamples();
}

// Picking a sample goes through the file input, so the review runs on exactly the path an
// uploaded file takes. Nothing here is a shortcut around the engine.
function chooseSample(sample) {
  const file = new File([sample.text], sample.filename, { type: "text/plain" });
  const bag = new DataTransfer();
  bag.items.add(file);
  $("file").files = bag.files;
  if ($("vault").value !== sample.vault_id) {
    $("vault").value = sample.vault_id;
    renderVault();
  }
  setFile(file, sample.id);
  for (const button of $("sample-list").querySelectorAll(".sample")) {
    button.setAttribute("aria-pressed", String(button.dataset.id === sample.id));
  }
}

function renderSamples() {
  const box = $("samples");
  box.hidden = samples.length === 0;
  if (!samples.length) return;
  const forVault = samples.filter((s) => s.vault_id === $("vault").value);
  const shown = forVault.length ? forVault : samples;
  $("samples-intro").textContent = forVault.length
    ? "No document at hand? Review a synthetic one for this vault:"
    : "No document at hand? Review a synthetic one:";
  $("sample-list").replaceChildren(...shown.map((sample) => {
    const button = h("button", {
      type: "button", class: "sample", "aria-pressed": String(chosenSample === sample.id),
      // The label and note render as separate blocks, which screen readers may run together.
      "aria-label": `${sample.label}. ${sample.note}`,
    },
      h("span", { class: "sample-label" }, sample.label),
      forVault.length ? null : h("span", { class: "sample-vault" }, sample.vault_id),
      h("span", { class: "sample-note" }, sample.note));
    button.dataset.id = sample.id;
    button.addEventListener("click", () => chooseSample(sample));
    return h("li", {}, button);
  }));
}

function updateRunState() {
  $("run").disabled = !($("vault").value && $("file").files.length);
}

function showError(message) {
  const box = $("error");
  box.textContent = message;
  box.hidden = !message;
}

let chosenSample = null;

function setFile(file, sampleId = null) {
  chosenSample = sampleId;
  $("drop-text").textContent = file ? `${file.name} (${Math.max(1, Math.round(file.size / 1024))} KB)` : "Drop a file here or click to choose";
  $("drop").classList.toggle("has-file", Boolean(file));
  if (!sampleId) {
    for (const button of $("sample-list").querySelectorAll(".sample")) {
      button.setAttribute("aria-pressed", "false");
    }
  }
  updateRunState();
}

function verdictBadge(verdict) {
  const v = VERDICTS[verdict];
  return h("span", { class: `badge ${verdict}` }, `${v.symbol} ${verdict}`, h("span", { class: "role" }, ` ${v.gloss}`));
}

function renderCard(result) {
  const card = h("article", { class: `card ${result.verdict}` },
    h("div", { class: "card-head" },
      h("div", {},
        h("h3", {}, result.title),
        h("p", { class: "rule-id" },
          h("span", { class: `sev ${result.severity}` }, result.severity),
          h("span", {}, result.rule_id))),
      verdictBadge(result.verdict)),
    h("p", { class: "rationale" }, result.rationale));

  if (result.evidence.length) {
    card.append(h("div", { class: "evidence" },
      h("div", { class: "label" }, "Evidence from the document"),
      result.evidence.map((e) => h("blockquote", {}, e.quote, e.page ? h("span", { class: "page" }, `page ${e.page}`) : null))));
  }

  if (result.external) {
    const ext = result.external;
    card.append(h("div", { class: "external" },
      h("div", { class: "label" }, "Public verification"),
      h("div", {}, `Status: ${ext.status}. `, ext.rationale),
      ext.query ? h("div", {}, "Only this query left the perimeter: ", h("code", {}, ext.query)) : null,
      ext.sources.length ? h("ul", {}, ext.sources.map((s) => {
        const href = safeHref(s.url);
        return h("li", {},
          href ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, s.title || s.url) : (s.title || s.url),
          " ", h("span", { class: "role" }, `(${s.role})`));
      })) : null));
  }

  if (result.facts.length) {
    const rows = result.facts.map(factRow);
    card.append(h("details", {},
      h("summary", {}, `Extracted facts (${result.facts.length})`),
      h("table", { class: "facts" }, h("tbody", {}, rows))));
  }
  return card;
}

function factRow(fact) {
  return h("tr", {},
    h("th", { scope: "row" }, fact.name),
    h("td", {}, fact.found ? String(fact.value) : "not established"),
    h("td", {}, fact.found ? `“${fact.quote}”` : (fact.note || "")));
}

function custodyRow(term, value, mono = false) {
  return [h("dt", {}, term), h("dd", mono ? { class: "mono" } : {}, value)];
}

// What a reviewer must be able to point at later: which file, proved by its hash, judged
// against which policy, by which model, and when.
function renderCustody(report) {
  const when = new Date(report.created_at);
  $("results-meta").replaceChildren(h("dl", { class: "custody" },
    custodyRow("Document", report.document.filename),
    custodyRow("SHA-256", report.document.sha256, true),
    custodyRow("Extent", `${report.document.pages} page(s) · ${report.document.chars.toLocaleString()} characters`),
    custodyRow("Vault", `${report.vault_title} (${report.vault_id})`),
    custodyRow("Model", report.model || "not reported", true),
    custodyRow("Reviewed", isNaN(when) ? report.created_at : when.toLocaleString())));
}

function renderReport(report) {
  lastReport = report;
  renderCustody(report);
  const ordered = triageOrder(report.results);
  const open = ordered.filter((r) => r.verdict !== "cumple").length;
  $("order-note").hidden = false;
  $("order-note").textContent = open
    ? `${open} of ${report.results.length} rules need a person. Listed first.`
    : "Every rule passed, each with cited evidence.";
  $("summary").replaceChildren(...Object.entries(report.summary).map(([verdict, count]) =>
    h("li", {}, h("span", { class: `badge ${verdict}` }, `${VERDICTS[verdict].symbol} ${verdict}: ${count}`))));
  $("cards").replaceChildren(...ordered.map(renderCard));
  revealResults();
}

// Rules are checked in parallel, so no rule is "next" and none can be reported as done early.
// What is true during the wait: which rules are outstanding, and how long it has been.
function revealResults() {
  $("results").hidden = false;
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  $("results").scrollIntoView({ behavior: motion });
}

function pendingCard(rule) {
  return h("article", { class: "card pending" },
    h("div", { class: "card-head" },
      h("div", {},
        h("h3", {}, rule.title),
        h("p", { class: "rule-id" },
          h("span", { class: `sev ${rule.severity}` }, rule.severity),
          h("span", {}, rule.id))),
      h("span", { class: "badge pending" }, "… checking")),
    h("div", { class: "skeleton", "aria-hidden": "true" }, h("span", {}), h("span", {}), h("span", {})));
}

function renderPending(file) {
  const vault = vaults.find((v) => v.id === $("vault").value);
  const rules = vault ? vault.rules : [];
  $("results-meta").textContent = `${file.name} · ${vault ? vault.title : ""}`;
  $("order-note").hidden = true;
  $("summary").replaceChildren(
    h("li", {}, h("span", { class: "badge pending" },
      rules.length === 1 ? "checking 1 rule" : `checking ${rules.length} rules`)),
    h("li", { class: "elapsed", id: "elapsed", "aria-hidden": "true" }, "0s"));
  $("cards").replaceChildren(...rules.map(pendingCard));
  revealResults();
  return rules.length;
}

function startClock() {
  const started = Date.now();
  return setInterval(() => {
    const node = $("elapsed");
    if (!node) return;
    const total = Math.floor((Date.now() - started) / 1000);
    node.textContent = total < 60
      ? `${total}s`
      : `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
  }, 1000);
}

async function submit(event) {
  event.preventDefault();
  showError("");
  const file = $("file").files[0];
  const body = new FormData();
  body.append("vault_id", $("vault").value);
  body.append("file", file);
  $("run").disabled = true;
  const count = renderPending(file);
  // Set once: this is an aria-live region, so a per-second update would talk over the user.
  $("progress").textContent =
    `Checking ${count} rule${count === 1 ? "" : "s"}. This can take a minute on a large document.`;
  const clock = startClock();
  try {
    const resp = await fetch("/v1/reviews", { method: "POST", body });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(payload.detail || `${resp.status} ${resp.statusText}`);
    renderReport(payload);
  } catch (err) {
    $("results").hidden = true;
    showError(err.message);
  } finally {
    clearInterval(clock);
    $("progress").textContent = "";
    updateRunState();
  }
}

function download() {
  if (!lastReport) return;
  const blob = new Blob([JSON.stringify(lastReport, null, 2)], { type: "application/json" });
  const link = h("a", { href: URL.createObjectURL(blob), download: `sentinel-${lastReport.document.sha256.slice(0, 12)}.json` });
  link.click();
  URL.revokeObjectURL(link.href);
}

const drop = $("drop");
$("vault").addEventListener("change", () => { renderVault(); updateRunState(); });
$("file").addEventListener("change", () => setFile($("file").files[0]));
$("review-form").addEventListener("submit", submit);
$("download").addEventListener("click", download);
for (const type of ["dragenter", "dragover"]) drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); });
for (const type of ["dragleave", "drop"]) drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.remove("over"); });
drop.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) {
    $("file").files = e.dataTransfer.files;
    setFile($("file").files[0]);
  }
});

loadStatus();
loadVaults();
loadSamples();
