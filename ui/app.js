"use strict";

// All server data is rendered with textContent (never innerHTML).

const VERDICTS = {
  cumple: { symbol: "✓", gloss: "passes" },
  no_cumple: { symbol: "✕", gloss: "fails" },
  revisar: { symbol: "!", gloss: "needs human review" },
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

function safeHref(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

let vaults = [];
let lastReport = null;

async function loadStatus() {
  const list = $("status");
  try {
    const health = await (await fetch("/healthz")).json();
    const pill = (on, text) => h("li", {}, h("span", { class: "dot " + (on ? "on" : "off") }), text);
    list.replaceChildren(
      pill(health.llm_configured, health.llm_configured
        ? `Model: ${health.llm_model} @ ${health.llm_host}`
        : "Model: not configured"),
      pill(health.search_configured, health.search_configured
        ? "Scoped verification: on" : "Scoped verification: off"),
    );
  } catch {
    list.replaceChildren(h("li", {}, "Status unavailable"));
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
  list.replaceChildren(...(vault ? vault.rules.map((r) => h("li", {}, r.title)) : []));
  $("vault-rules").hidden = !vault;
}

function updateRunState() {
  $("run").disabled = !($("vault").value && $("file").files.length);
}

function showError(message) {
  const box = $("error");
  box.textContent = message;
  box.hidden = !message;
}

function setFile(file) {
  $("drop-text").textContent = file ? `${file.name} (${Math.max(1, Math.round(file.size / 1024))} KB)` : "Drop a file here or click to choose";
  $("drop").classList.toggle("has-file", Boolean(file));
  updateRunState();
}

function verdictBadge(verdict) {
  const v = VERDICTS[verdict];
  return h("span", { class: `badge ${verdict}` }, `${v.symbol} ${verdict}`, h("span", { class: "role" }, ` ${v.gloss}`));
}

function renderCard(result) {
  const card = h("article", { class: `card ${result.verdict}` },
    h("div", { class: "card-head" },
      h("div", {}, h("h3", {}, result.title), h("span", { class: "rule-id" }, `${result.rule_id} · ${result.severity} severity`)),
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

function renderReport(report) {
  lastReport = report;
  $("results-meta").textContent = `${report.document.filename} · sha256 ${report.document.sha256} · ${report.document.pages} page(s) · model ${report.model || "n/a"}`;
  $("summary").replaceChildren(...Object.entries(report.summary).map(([verdict, count]) =>
    h("li", {}, h("span", { class: `badge ${verdict}` }, `${VERDICTS[verdict].symbol} ${verdict}: ${count}`))));
  $("cards").replaceChildren(...report.results.map(renderCard));
  $("results").hidden = false;
  $("results").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

async function submit(event) {
  event.preventDefault();
  showError("");
  $("results").hidden = true;
  const body = new FormData();
  body.append("vault_id", $("vault").value);
  body.append("file", $("file").files[0]);
  $("run").disabled = true;
  $("progress").textContent = "Reviewing… this can take a minute on a large document.";
  try {
    const resp = await fetch("/v1/reviews", { method: "POST", body });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(payload.detail || `${resp.status} ${resp.statusText}`);
    renderReport(payload);
  } catch (err) {
    showError(err.message);
  } finally {
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
