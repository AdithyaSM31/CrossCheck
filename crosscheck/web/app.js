/* CrossCheck UI. No framework, no build step — the setup instructions are pip and uvicorn. */

const $ = (s, r = document) => r.querySelector(s);
const main = $("#main");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
/* Counts here run to five figures. Grouped digits are the difference between
   reading 5,835 and counting the characters in 5835. */
const num = (n) => Number(n ?? 0).toLocaleString("en-US");
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
    // UNRELATED is bookkeeping -- the record that a pair was already adjudicated -- so it
    // is never counted as a finding, here or on the Findings screen.
    const findings = Object.entries(s.by_type || {})
      .filter(([k]) => k !== "UNRELATED")
      .reduce((a, [, v]) => a + v, 0);
    $("#c-findings").textContent = findings ? num(findings) : "";
    $("#c-facts").textContent = s.facts ? num(s.facts) : "";
    $("#c-schema").textContent = s.attributes ? num(s.attributes) : "";
    $("#c-review").textContent = s.rejected ? num(s.rejected) : "";
  } catch (_) { /* first run, empty database */ }
}

/* Which model does which job is the first question anyone asks of a system like this,
   so it is stated in the header rather than buried in a config file — but as two
   labelled rows, not one long run of monospace that wraps at an arbitrary point. */
async function health() {
  const box = $("#health");
  try {
    const h = await api("/health");
    const models = h.models || [];
    box.innerHTML = models.length
      ? models.map((m) => `
          <span class="role">${esc(m.role)}</span>
          <span class="what"><b>${esc(m.model)}</b>
            <span>on ${esc(m.vendor)}${m.effort ? `, reasoning ${esc(m.effort)}` : ""}</span>
          </span>`).join("")
      : `<span class="none">No model configured — set one in <b>.env</b> to ingest a PDF.</span>`;
  } catch (_) { box.innerHTML = ""; }
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
        .map(([k, label]) =>
          `<div class="stat"><b>${num(s[k])}</b><span>${label}</span></div>`)
        .join("")}
    </div>
    <div class="card">
      <div class="scroll">
        <table>
          <thead><tr><th>Document</th><th>Publisher</th><th class="tight">Published</th>
            <th class="num">Pages</th><th class="num">Facts</th><th class="num">Rejected</th>
            <th class="tight">Status</th></tr></thead>
          <tbody>${docs.map((d) => `
            <tr>
              <td class="wrap-any"><b>${esc(d.title || d.filename)}</b>
                <div class="q">${esc(d.filename)}</div></td>
              <td>${esc(d.publisher || "—")}</td>
              <td class="tight mono">${esc(d.published_on || "—")}</td>
              <td class="num">${num(d.page_count)}</td>
              <td class="num">${num(d.facts)}</td>
              <td class="num">${num(d.rejected)}</td>
              <td class="tight"><span class="chip">${esc(d.status)}</span></td>
            </tr>`).join("") || `<tr><td colspan="7" class="empty">No documents yet.</td></tr>`}
          </tbody>
        </table>
      </div>
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
    jobs.innerHTML = `<p class="lede err">${esc(e.message)}</p>`;
  }
}

async function poll(id, name) {
  const jobs = $("#jobs");
  const tick = async () => {
    const j = await api("/jobs/" + id);
    const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
    jobs.innerHTML = `
      <div class="job">
        <div class="job-head">
          <b>${esc(name)}</b>
          <span class="stage">${esc(j.stage)}</span>
          ${j.total ? `<span class="chip">${num(j.done)} / ${num(j.total)} blocks</span>` : ""}
        </div>
        <div class="bar"><i style="width:${j.stage === "done" ? 100 : pct}%"></i></div>
        <div class="q${j.error ? " err" : ""}">${esc(j.error || j.message || "")}</div>
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
          <option value="">All types (${num(Object.values(counts).reduce((a, b) => a + b, 0))})</option>
          ${["CONTRADICTS", "RECONCILED_BY_CONTEXT", "CORROBORATES", "DERIVED_CONSISTENT", "SUPERSEDES"]
            .filter((t) => counts[t])
            .map((t) => `<option value="${t}" ${state.relType === t ? "selected" : ""}>${
              t.replace(/_/g, " ")} (${num(counts[t])})</option>`).join("")}
        </select>
        <label class="row" style="gap:6px">
          <input type="checkbox" id="cross" ${state.crossOnly ? "checked" : ""}>
          Cross-document only
        </label>
        <span class="grow"></span>
        <span class="chip">showing ${num(data.relations.length)}</span>
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
      ${quals.length ? `<div class="chips">${
        quals.map((q) => `<span class="chip k">${esc(q)}</span>`).join("")}</div>` : ""}
      <div class="src">${esc(r[k + "_publisher"] || r[k + "_doc_title"])}
        · page ${r[k + "_page"] + 1}</div>
      <blockquote>${esc(r[k + "_quote"])}</blockquote>
      <div class="act-row">
        <button class="act" data-fact="${r[k + "_id"]}">Show on page</button>
      </div>
    </div>`;
}

function card(r) {
  return `
    <div class="card">
      <div class="card-head">
        <span class="tag ${r.type}">${r.type.replace(/_/g, " ")}</span>
        ${r.discriminator ? `<span class="chip">${esc(r.discriminator)}</span>` : ""}
        <span class="chip">confidence ${Number(r.confidence || 0).toFixed(2)}</span>
        <span class="by">decided by ${esc(r.decided_by)}${
          r.rule_label ? " · " + esc(r.rule_label) : ""}</span>
      </div>
      <div class="claim"><b>${esc(r.a_subject)}</b><span class="sep">—</span>${esc(r.attribute)}</div>
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
        <span class="chip">${num(data.total)} matching</span>
      </div>
    </div>
    <div class="card">
      <div class="scroll">
        <table class="fixed">
          <colgroup><col style="width:20%"><col style="width:17%"><col style="width:18%">
            <col style="width:14%"><col style="width:19%"><col style="width:12%"></colgroup>
          <thead><tr><th>Subject</th><th>Attribute</th><th class="val-col">Value</th>
            <th>Qualifiers</th><th>Source</th><th>Grounding</th></tr></thead>
          <tbody>${data.facts.map((f) => `
            <tr data-fact="${f.id}">
              <td class="wrap-any">${esc(f.subject)}</td>
              <td class="wrap-any">${esc(f.attribute)}</td>
              <td class="val-col"><b>${esc(f.value_raw)}</b></td>
              <td>${[f.period_label, f.scope, f.basis].filter(Boolean).length
                    ? `<div class="chips">${[f.period_label, f.scope, f.basis].filter(Boolean)
                        .map((x) => `<span class="chip k">${esc(x)}</span>`).join("")}</div>`
                    : "—"}</td>
              <td>${esc(f.publisher || f.doc_title)}
                <div class="q">page ${f.evidence_page + 1}</div></td>
              <td><span class="chip">${esc(f.grounding)}</span></td>
            </tr>`).join("") || `<tr><td colspan="6" class="empty">No facts yet.</td></tr>`}
          </tbody>
        </table>
      </div>
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
      <div class="claim"><b>${esc(fact.subject)}</b><span class="sep">—</span>${
        esc(fact.attribute)}<span class="sep">=</span><b>${esc(fact.value_raw)}</b></div>
      <div class="q">${esc(fact.publisher || fact.doc_title)} · page ${fact.evidence_page + 1}
        · grounded ${esc(fact.grounding)}</div>
      <blockquote>${esc(fact.evidence_quote)}</blockquote>
      ${relations.length ? `<details open style="margin-top:12px">
        <summary>${relations.length === 1 ? "1 relationship" : num(relations.length) + " relationships"}</summary>
        ${relations.map((r) => `
          <div class="rel-line">
            <span class="tag ${r.type}">${r.type.replace(/_/g, " ")}</span>
            <span class="chip">${esc(r.other_value)}</span>
            ${r.other_period ? `<span class="chip k">${esc(r.other_period)}</span>` : ""}
            <span class="q">${esc(r.other_doc)}</span>
          </div>
          ${r.explanation ? `<div class="why">${esc(r.explanation)}</div>` : ""}`).join("")}
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
      <div class="scroll">
        <table class="fixed">
          <colgroup><col style="width:28%"><col style="width:13%"><col style="width:8%">
            <col style="width:51%"></colgroup>
          <thead><tr><th>Canonical attribute</th><th>Unit family</th>
            <th class="num">Facts</th><th>Also written as</th></tr></thead>
          <tbody>${s.attributes.map((a) => `
            <tr>
              <td class="wrap-any"><b>${esc(a.canon_name)}</b></td>
              <td><span class="chip">${esc(a.unit_family || "—")}</span></td>
              <td class="num">${num(a.n_facts)}</td>
              <td class="q wrap-any">${a.aliases.slice(0, 6).map(esc).join(" · ") || "—"}</td>
            </tr>`).join("") || `<tr><td colspan="4" class="empty">Nothing yet.</td></tr>`}
          </tbody>
        </table>
      </div>
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
      <div class="scroll" style="margin-top:8px">
        <table class="fixed">
          <colgroup><col style="width:22%"><col style="width:62%"><col style="width:16%"></colgroup>
          <thead><tr><th>Reason</th><th>What went wrong</th><th class="num">Count</th></tr></thead>
          <tbody>${r.reasons.map((x) => `
            <tr>
              <td><span class="chip">${esc(x.reason)}</span></td>
              <td>${esc(x.detail || "—")}</td>
              <td class="num">${num(x.n)}</td>
            </tr>`).join("") ||
            `<tr><td colspan="3" class="empty">Nothing rejected.</td></tr>`}</tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <b>Rejected extractions</b>
      <div class="scroll" style="margin-top:8px">
        <table class="fixed">
          <colgroup><col style="width:45%"><col style="width:31%"><col style="width:24%"></colgroup>
          <thead><tr><th>Proposed</th><th>Reason</th><th>Document</th></tr></thead>
          <tbody>${r.rejected.slice(0, 40).map((x) => `
            <tr>
              <td class="wrap-any">${esc(x.payload.attribute || "—")} =
                <b>${esc(x.payload.value || "—")}</b>
                <div class="q">${esc((x.payload.evidence_quote || "").slice(0, 150))}</div></td>
              <td><span class="chip">${esc(x.reason)}</span>
                <div class="q">${esc(x.detail || "")}</div></td>
              <td class="q">${esc(x.doc_title || "—")}</td>
            </tr>`).join("") || `<tr><td colspan="3" class="empty">Nothing rejected.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <b>Accepted, but worth a second look</b>
      <div class="scroll" style="margin-top:8px">
        <table class="fixed">
          <colgroup><col style="width:46%"><col style="width:13%"><col style="width:13%">
            <col style="width:28%"></colgroup>
          <thead><tr><th>Fact</th><th class="num">Confidence</th>
            <th>Grounding</th><th>Document</th></tr></thead>
          <tbody>${r.low_confidence.map((x) => `
            <tr data-fact="${x.id}">
              <td class="wrap-any">${esc(x.subject)} — ${esc(x.attribute_raw)} =
                <b>${esc(x.value_raw)}</b></td>
              <td class="num">${Number(x.confidence).toFixed(2)}</td>
              <td><span class="chip">${esc(x.grounding)}</span></td>
              <td class="q">${esc(x.doc_title)}</td>
            </tr>`).join("") || `<tr><td colspan="4" class="empty">Nothing flagged.</td></tr>`}
          </tbody>
        </table>
      </div>
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
