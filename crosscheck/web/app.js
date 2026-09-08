/* CrossCheck UI. No framework, no build step — the setup instructions are pip and uvicorn. */

const $ = (s, r = document) => r.querySelector(s);
const main = $("#main");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async (p, o) => {
  const r = await fetch("/api" + p, o);
  if (!r.ok) throw new Error((await r.text()).slice(0, 200));
  return r.json();
};

let view = "documents";
const state = { relType: "", crossOnly: false, factQuery: "", factDoc: "" };

/* ------------------------------------------------------------------ chrome */
async function refreshCounts() {
  try {
    const s = await api("/stats");
    const findings = Object.entries(s.by_type || {})
      .filter(([k]) => k !== "CORROBORATES")
      .reduce((a, [, v]) => a + v, 0) + (s.by_type?.CORROBORATES || 0);
    $("#c-findings").textContent = findings || "";
    $("#c-facts").textContent = s.facts || "";
    $("#c-schema").textContent = s.attributes || "";
    $("#c-review").textContent = s.rejected || "";
  } catch (_) { /* first run, empty database */ }
}

async function health() {
  try {
    const h = await api("/health");
    $("#health").textContent = [h.extract, h.reason].filter(Boolean).join("  ·  ")
      || "no model configured";
  } catch (_) { $("#health").textContent = ""; }
}

$("#nav").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-view]");
  if (!b) return;
  view = b.dataset.view;
  [...$("#nav").children].forEach((x) =>
    x.setAttribute("aria-current", String(x === b)));
  render();
});

/* ------------------------------------------------------------------ documents */
async function viewDocuments() {
  const [docs, s] = await Promise.all([api("/documents"), api("/stats").catch(() => ({}))]);
  main.innerHTML = `
    <h2>Documents</h2>
    <p class="lede">Drop a PDF here and the whole pipeline runs on it: layout reconstruction,
      fact extraction, grounding against the source text, vocabulary consolidation, then
      reconciliation against everything already known.</p>
    <div class="card">
      <div class="drop" id="drop">
        <b>Drop a PDF</b> or click to choose one
        <input type="file" id="file" accept="application/pdf" hidden>
      </div>
      <div id="jobs"></div>
    </div>
    <div class="stats">
      ${[["documents", "documents"], ["facts", "grounded facts"],
         ["attributes", "attributes discovered"], ["relations", "relationships"],
         ["rejected", "in review queue"]]
        .map(([k, label]) => `<div class="stat"><b>${s[k] ?? 0}</b><span>${label}</span></div>`)
        .join("")}
    </div>
    <div class="card">
      <table>
        <thead><tr><th>Document</th><th>Publisher</th><th>Published</th>
          <th>Pages</th><th>Facts</th><th>Rejected</th><th>Status</th></tr></thead>
        <tbody>${docs.map((d) => `
          <tr>
            <td><b>${esc(d.title || d.filename)}</b><div class="q">${esc(d.filename)}</div></td>
            <td>${esc(d.publisher || "—")}</td>
            <td class="mono">${esc(d.published_on || "—")}</td>
            <td>${d.page_count}</td><td>${d.facts}</td><td>${d.rejected}</td>
            <td><span class="chip">${esc(d.status)}</span></td>
          </tr>`).join("") || `<tr><td colspan="7" class="empty">No documents yet.</td></tr>`}
        </tbody>
      </table>
    </div>`;

  const drop = $("#drop"), file = $("#file");
  drop.onclick = () => file.click();
  file.onchange = () => file.files[0] && upload(file.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => {
    e.preventDefault(); drop.classList.remove("over");
    if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
  };
}

async function upload(f) {
  const body = new FormData();
  body.append("file", f);
  const jobs = $("#jobs");
  jobs.innerHTML = `<p class="lede">Uploading ${esc(f.name)}…</p>`;
  try {
    const { job_id } = await api("/documents", { method: "POST", body });
    poll(job_id, f.name);
  } catch (e) {
    jobs.innerHTML = `<p class="lede" style="color:var(--contradict)">${esc(e.message)}</p>`;
  }
}

async function poll(id, name) {
  const jobs = $("#jobs");
  const tick = async () => {
    const j = await api("/jobs/" + id);
    const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
    jobs.innerHTML = `
      <div style="margin-top:14px">
        <b>${esc(name)}</b> — ${esc(j.stage)}
        ${j.total ? `<span class="chip">${j.done}/${j.total}</span>` : ""}
        <div class="bar"><i style="width:${j.stage === "done" ? 100 : pct}%"></i></div>
        <div class="q" style="margin-top:6px">${esc(j.error || j.message || "")}</div>
      </div>`;
    if (j.stage !== "done" && j.stage !== "failed") setTimeout(tick, 1500);
    else { refreshCounts(); if (view === "documents") setTimeout(viewDocuments, 800); }
  };
  tick();
}

/* ------------------------------------------------------------------ findings */
const TYPE_BLURB = {
  "": "Every relationship found between facts, across and within documents.",
  CORROBORATES: "Facts that agree. Same claim and same value, or the same value reached a different way.",
  CONTRADICTS: "The same claim with genuinely incompatible values, and nothing in the evidence explains the difference.",
  RECONCILED_BY_CONTEXT: "Values that look like a conflict until you see what differs — a period, a scope, a basis, a unit. The differing component is the explanation.",
  DERIVED_CONSISTENT: "Corroboration between facts that are not the same claim at all: a stated ratio matching the amounts that imply it, or a stated growth rate matching the two levels it spans.",
  SUPERSEDES: "A later document revising an earlier figure for the same claim.",
};

async function viewFindings() {
  const qs = new URLSearchParams({ limit: "60" });
  if (state.relType) qs.set("type", state.relType);
  if (state.crossOnly) qs.set("cross_document", "true");
  const data = await api("/relations?" + qs);
  const counts = data.counts || {};

  main.innerHTML = `
    <h2>Findings</h2>
    <p class="lede">${esc(TYPE_BLURB[state.relType] || TYPE_BLURB[""])}</p>
    <div class="card">
      <div class="row">
        <select id="type">
          <option value="">All types (${Object.values(counts).reduce((a, b) => a + b, 0)})</option>
          ${["CONTRADICTS", "RECONCILED_BY_CONTEXT", "CORROBORATES", "DERIVED_CONSISTENT", "SUPERSEDES"]
            .filter((t) => counts[t])
            .map((t) => `<option value="${t}" ${state.relType === t ? "selected" : ""}>
              ${t.replace(/_/g, " ")} (${counts[t]})</option>`).join("")}
        </select>
        <label class="row" style="gap:6px">
          <input type="checkbox" id="cross" ${state.crossOnly ? "checked" : ""}>
          cross-document only
        </label>
      </div>
    </div>
    ${data.relations.map(card).join("") ||
      `<div class="card empty">No relationships of this kind yet.</div>`}`;

  $("#type").onchange = (e) => { state.relType = e.target.value; render(); };
  $("#cross").onchange = (e) => { state.crossOnly = e.target.checked; render(); };
}

function side(r, k) {
  const quals = [r[k + "_period"], r[k + "_scope"], r[k + "_basis"]].filter(Boolean);
  return `
    <div class="side">
      <div class="val">${esc(r[k + "_value"])}</div>
      ${quals.map((q) => `<span class="chip k">${esc(q)}</span>`).join(" ")}
      <div class="src">${esc(r[k + "_publisher"] || r[k + "_doc_title"])}
        · page ${r[k + "_page"] + 1}</div>
      <blockquote>${esc(r[k + "_quote"])}</blockquote>
      <div style="margin-top:8px">
        <button class="act" data-fact="${r[k + "_id"]}">Show on page</button>
      </div>
    </div>`;
}

function card(r) {
  return `
    <div class="card">
      <span class="by">decided by ${esc(r.decided_by)}${
        r.rule_label ? " · " + esc(r.rule_label) : ""}</span>
      <span class="tag ${r.type}">${r.type.replace(/_/g, " ")}</span>
      ${r.discriminator ? `<span class="chip">${esc(r.discriminator)}</span>` : ""}
      <span class="chip">confidence ${Number(r.confidence || 0).toFixed(2)}</span>
      <div style="margin-top:6px"><b>${esc(r.a_subject)}</b> — ${esc(r.attribute)}</div>
      <div class="pair">${side(r, "a")}${side(r, "b")}</div>
      ${r.explanation ? `<div class="why">${esc(r.explanation)}</div>` : ""}
      <div class="evidence" data-slot></div>
    </div>`;
}

/* ------------------------------------------------------------------ facts */
async function viewFacts() {
  const qs = new URLSearchParams({ limit: "150" });
  if (state.factQuery) qs.set("q", state.factQuery);
  if (state.factDoc) qs.set("doc", state.factDoc);
  const [data, docs] = await Promise.all([api("/facts?" + qs), api("/documents")]);

  main.innerHTML = `
    <h2>Facts</h2>
    <p class="lede">Every fact survived a grounding check: its quote was found in the text the
      model was shown, and its value was found inside that quote. Select a row to see it on
      the page it came from.</p>
    <div class="card">
      <div class="row">
        <input type="search" id="q" placeholder="Search subject, attribute, value or evidence"
          value="${esc(state.factQuery)}">
        <select id="doc">
          <option value="">All documents</option>
          ${docs.map((d) => `<option value="${d.id}" ${state.factDoc == d.id ? "selected" : ""}>
            ${esc(d.title || d.filename)}</option>`).join("")}
        </select>
        <span class="grow"></span>
        <span class="chip">${data.total} matching</span>
      </div>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>Subject</th><th>Attribute</th><th>Value</th><th>Qualifiers</th>
          <th>Source</th><th>Grounding</th></tr></thead>
        <tbody>${data.facts.map((f) => `
          <tr data-fact="${f.id}">
            <td>${esc(f.subject)}</td>
            <td>${esc(f.attribute)}</td>
            <td><b>${esc(f.value_raw)}</b></td>
            <td>${[f.period_label, f.scope, f.basis].filter(Boolean)
                  .map((x) => `<span class="chip k">${esc(x)}</span>`).join(" ") || "—"}</td>
            <td>${esc(f.publisher || f.doc_title)}<div class="q">p${f.evidence_page + 1}</div></td>
            <td><span class="chip">${esc(f.grounding)}</span></td>
          </tr>`).join("") || `<tr><td colspan="6" class="empty">No facts yet.</td></tr>`}
        </tbody>
      </table>
    </div>
    <div id="detail"></div>`;

  let t;
  $("#q").oninput = (e) => {
    clearTimeout(t);
    const v = e.target.value;
    t = setTimeout(() => { state.factQuery = v; render(); }, 300);
  };
  $("#doc").onchange = (e) => { state.factDoc = e.target.value; render(); };
}

async function showFact(id, slot) {
  const { fact, relations } = await api("/facts/" + id);
  slot.innerHTML = `
    <div class="card">
      <b>${esc(fact.subject)}</b> — ${esc(fact.attribute)} =
      <b>${esc(fact.value_raw)}</b>
      <div class="src q">${esc(fact.publisher || fact.doc_title)} · page ${fact.evidence_page + 1}
        · grounded ${esc(fact.grounding)}</div>
      <blockquote>${esc(fact.evidence_quote)}</blockquote>
      ${relations.length ? `<details open style="margin-top:10px">
        <summary>${relations.length} relationship(s)</summary>
        ${relations.map((r) => `<div style="margin-top:8px">
          <span class="tag ${r.type}">${r.type.replace(/_/g, " ")}</span>
          <span class="chip">${esc(r.other_value)}</span>
          ${r.other_period ? `<span class="chip k">${esc(r.other_period)}</span>` : ""}
          <span class="q">${esc(r.other_doc)}</span>
          ${r.explanation ? `<div class="why">${esc(r.explanation)}</div>` : ""}
        </div>`).join("")}
      </details>` : ""}
      <div class="evidence"><img loading="lazy" src="/api/facts/${id}/evidence.png"
        alt="source page with the evidence highlighted"></div>
    </div>`;
  slot.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ------------------------------------------------------------------ schema */
async function viewSchema() {
  const s = await api("/schema");
  main.innerHTML = `
    <h2>Schema</h2>
    <p class="lede">The attribute vocabulary is a table, not a fixed list. It starts empty and
      grows as documents introduce measures nobody anticipated, which is what lets the system
      work on PDFs it has never seen. Variants are merged into one canonical attribute so the
      same measure from different publishers can be compared — but a level is never merged
      with a rate of change.</p>
    <div class="card">
      <table>
        <thead><tr><th>Canonical attribute</th><th>Unit family</th><th>Facts</th>
          <th>Also written as</th></tr></thead>
        <tbody>${s.attributes.map((a) => `
          <tr>
            <td><b>${esc(a.canon_name)}</b></td>
            <td><span class="chip">${esc(a.unit_family || "—")}</span></td>
            <td>${a.n_facts}</td>
            <td class="q">${a.aliases.slice(0, 6).map(esc).join(" · ") || "—"}</td>
          </tr>`).join("") || `<tr><td colspan="4" class="empty">Nothing yet.</td></tr>`}
        </tbody>
      </table>
    </div>`;
}

/* ------------------------------------------------------------------ review */
async function viewReview() {
  const r = await api("/review");
  main.innerHTML = `
    <h2>Review</h2>
    <p class="lede">Everything the system declined to believe, kept rather than discarded.
      A proposed fact is rejected when its quote cannot be found in the source text, or when
      the value it reports is not inside the quote it cites — the failure that otherwise looks
      exactly like a real fact. Low-confidence and fuzzily-matched facts are listed too.</p>
    <div class="card">
      <b>Why extractions were rejected</b>
      <table style="margin-top:8px">
        <thead><tr><th>Reason</th><th>Count</th></tr></thead>
        <tbody>${r.reasons.map((x) => `<tr><td>${esc(x.reason)}</td><td>${x.n}</td></tr>`)
          .join("") || `<tr><td colspan="2" class="empty">Nothing rejected.</td></tr>`}</tbody>
      </table>
    </div>
    <div class="card">
      <b>Rejected extractions</b>
      <table style="margin-top:8px">
        <thead><tr><th>Proposed</th><th>Reason</th><th>Document</th></tr></thead>
        <tbody>${r.rejected.slice(0, 40).map((x) => `
          <tr>
            <td>${esc(x.payload.attribute || "—")} = <b>${esc(x.payload.value || "—")}</b>
              <div class="q">${esc((x.payload.evidence_quote || "").slice(0, 150))}</div></td>
            <td><span class="chip">${esc(x.reason)}</span><div class="q">${esc(x.detail || "")}</div></td>
            <td class="q">${esc(x.doc_title || "—")}</td>
          </tr>`).join("") || `<tr><td colspan="3" class="empty">Nothing rejected.</td></tr>`}
        </tbody>
      </table>
    </div>
    <div class="card">
      <b>Accepted, but worth a second look</b>
      <table style="margin-top:8px">
        <thead><tr><th>Fact</th><th>Confidence</th><th>Grounding</th><th>Document</th></tr></thead>
        <tbody>${r.low_confidence.map((x) => `
          <tr data-fact="${x.id}">
            <td>${esc(x.subject)} — ${esc(x.attribute_raw)} = <b>${esc(x.value_raw)}</b></td>
            <td>${Number(x.confidence).toFixed(2)}</td>
            <td><span class="chip">${esc(x.grounding)}</span></td>
            <td class="q">${esc(x.doc_title)}</td>
          </tr>`).join("") || `<tr><td colspan="4" class="empty">Nothing flagged.</td></tr>`}
        </tbody>
      </table>
    </div>
    <div id="detail"></div>`;
}

/* ------------------------------------------------------------------ wiring */
main.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-fact]");
  if (btn) {
    const slot = btn.closest(".card").querySelector("[data-slot]");
    slot.innerHTML = `<img loading="lazy" src="/api/facts/${btn.dataset.fact}/evidence.png"
      alt="source page with the evidence highlighted">`;
    return;
  }
  const row = e.target.closest("tr[data-fact]");
  if (row && $("#detail")) showFact(row.dataset.fact, $("#detail"));
});

const VIEWS = {
  documents: viewDocuments, findings: viewFindings,
  facts: viewFacts, schema: viewSchema, review: viewReview,
};

async function render() {
  main.innerHTML = `<div class="card empty">Loading…</div>`;
  try { await VIEWS[view](); }
  catch (e) {
    main.innerHTML = `<div class="card"><b>Could not load.</b>
      <div class="q">${esc(e.message)}</div></div>`;
  }
  refreshCounts();
}

health();
render();
