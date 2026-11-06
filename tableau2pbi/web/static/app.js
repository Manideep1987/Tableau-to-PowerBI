/* Tableau -> Power BI Migrator UI (no dependencies). */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const state = { job: null, file: null, tab: "inventory", validation: null, catalog: null, health: {} };

  // ------------------------------------------------------------- helpers
  function toast(msg, error = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast" + (error ? " error" : "");
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), error ? 6000 : 2500);
  }
  async function api(url, opts = {}) {
    const r = await fetch(url, opts);
    const ct = r.headers.get("content-type") || "";
    const body = ct.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new Error((body && body.detail) || r.statusText);
    return body;
  }
  const pillClass = (s) => {
    s = String(s || "").toLowerCase();
    if (["pass", "converted", "match", "supported", "approved"].includes(s)) return "pass";
    if (["warn", "warning", "converted with notes", "null_vs_zero", "extra_blank_row", "review", "approved-with-redesign"].includes(s)) return "warn";
    if (["fail", "error", "failed", "manual review", "value_mismatch", "mismatch", "null_mismatch", "missing_in_powerbi", "missing_in_tableau", "unsupported", "blocked", "rejected"].includes(s)) return "fail";
    if (["info", "skip", "pending"].includes(s)) return "info";
    return "neutral";
  };
  const pill = (s, label) => `<span class="pill ${pillClass(s)}">${esc(label ?? s)}</span>`;
  const code = (txt) => `<div class="code-wrap"><pre class="code">${esc(txt)}</pre><button class="btn small copy" data-copy="${esc(txt)}" aria-label="Copy">Copy</button></div>`;
  const fmt = (v) => {
    if (v === null || v === undefined) return "∅";
    if (typeof v === "number") return Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(+v.toFixed(6));
    return esc(v);
  };
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (b) { navigator.clipboard?.writeText(b.dataset.copy).then(() => toast("Copied")); }
    const g = e.target.closest("[data-goto]");
    if (g) show(g.dataset.goto);
  });

  // ------------------------------------------------------------- routing
  function show(view) {
    $$("nav button").forEach((b) => (b.dataset.view === view ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current")));
    $$(".view").forEach((v) => (v.hidden = v.id !== "view-" + view));
    $("#steps").hidden = !["convert", "validate", "export"].includes(view);
    if (view === "reference") loadCatalog();
    if (view === "validate") renderValidateForm();
    if (view === "export") renderExport();
    if (view === "translate") $("#use-job-wrap").hidden = !state.job;
    history.replaceState(null, "", "#" + view);
    updateSteps();
  }
  $$("nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
  function updateSteps() {
    const view = (location.hash || "#convert").slice(1);
    let active = 1;
    if (state.job) {
      if (view === "validate") active = 5;
      else if (view === "export") active = 6;
      else if ($("#convert-result").hidden) active = 1;
      else active = state.tab === "inventory" ? 2 : state.tab === "calcs" ? 3 : 4;
    }
    $$("#steps li").forEach((li) => {
      const n = +li.dataset.step;
      const done = !!state.job && (n < active || (n === 5 && !!state.validation) || (n <= 4 && active >= 5));
      li.classList.toggle("active", n === active);
      li.classList.toggle("done", done && n !== active);
    });
  }

  // ------------------------------------------------------------- convert
  const drop = $("#drop"), fileIn = $("#file");
  function setFile(f) {
    if (!f) return;
    if (!/\.(twbx?|TWBX?)$/.test(f.name)) { toast("Please choose a .twb or .twbx file", true); return; }
    state.file = f;
    $("#file-chip").innerHTML = `<span class="file-chip">📄 ${esc(f.name)} <span class="muted">${(f.size / 1024).toFixed(0)} KB</span></span>`;
    $("#btn-convert").disabled = false;
  }
  fileIn.addEventListener("change", () => setFile(fileIn.files[0]));
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileIn.click(); } });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));

  async function convert(form, btn) {
    const label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> Converting…`;
    try {
      form.append("join_strategy", $("input[name=join]:checked").value);
      const job = await api("/api/convert", { method: "POST", body: form });
      state.job = job;
      state.validation = null;
      renderJob();
      toast("Conversion complete");
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
    }
  }
  $("#btn-convert").addEventListener("click", (e) => {
    if (!state.file) return;
    const f = new FormData();
    f.append("file", state.file);
    convert(f, e.currentTarget);
  });
  $("#btn-new").addEventListener("click", () => {
    $("#convert-result").hidden = true;
    $("#convert-start").hidden = false;
    updateSteps();
  });
  $("#btn-to-validate").addEventListener("click", () => show("validate"));

  async function loadSamples() {
    try {
      const s = await api("/api/samples");
      $("#samples").innerHTML = s.map((x) => `<div class="sample"><div><b>${esc(x.name)} ${x.showcase ? '<span class="pill info">Finance showcase</span>' : ""}</b><span>${esc(x.description)}</span></div>
        <button class="btn small" data-sample="${esc(x.name)}">Convert</button></div>`).join("") || `<div class="muted">No samples found.</div>`;
      $$("[data-sample]").forEach((b) => b.addEventListener("click", (e) => {
        const f = new FormData();
        f.append("sample", b.dataset.sample);
        convert(f, e.currentTarget);
      }));
    } catch (e) { $("#samples").textContent = e.message; }
  }

  function renderJob() {
    const j = state.job, st = j.stats;
    $("#convert-start").hidden = true;
    $("#convert-result").hidden = false;
    $("#job-title").textContent = j.workbook.name;
    $("#job-sub").textContent = `${j.filename} · Tableau ${j.workbook.version || "?"} · analyzed and converted in ${j.duration_s}s · processed locally`;
    const cls = { PASS: 0, REVIEW: 0, UNSUPPORTED: 0 };
    j.calculations.filter((m) => !["base-measure", "date-part"].includes(m.kind)).forEach((m) => (cls[m.classification] = (cls[m.classification] || 0) + 1));
    const errors = j.issues.filter((i) => i.severity === "error").length;
    const warns = j.issues.filter((i) => i.severity === "warning").length;
    const cards = [
      ["Parse completeness", `${j.parse_stats.completeness ?? "–"}%`, `${j.parse_stats.unknown_elements || 0} unknown XML nodes preserved`],
      ["Calculations", `${cls.PASS} PASS`, `${cls.REVIEW} REVIEW · ${cls.UNSUPPORTED} UNSUPPORTED`],
      ["Model", `${st.tables} tables`, `${st.relationships} relationships · ${st.measures} measures · ${st.calc_columns} calc columns`],
      ["Report", `${st.pages} pages`, `${st.visuals} visuals from ${st.worksheets} sheets`],
      ["Security", j.roles.length ? `${j.roles.length} RLS role${j.roles.length > 1 ? "s" : ""}` : "None", j.security_findings.length ? "user filters found: live test required" : "no user filters"],
      ["Review items", errors + warns, `${errors} need action · ${warns} warnings`],
    ];
    $("#stats").innerHTML = cards.map(([k, v, s]) => `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(s)}</div></div>`).join("");
    $("#c-calcs").textContent = j.calculations.length;
    $("#c-data").textContent = j.datasources.length;
    $("#c-model").textContent = j.model.tables.length;
    $("#c-layout").textContent = j.pages.length;
    $("#c-filters").textContent = j.worksheets.reduce((a, w) => a + w.filters.length, 0) + j.parameters.length;
    $("#c-issues").textContent = j.issues.length;
    $("#c-inventory").textContent = j.inventory.length;
    $("#c-security").textContent = j.roles.length + j.security_findings.length;
    renderTab(state.tab);
    updateSteps();
  }
  $$("#result-tabs button").forEach((b) => b.addEventListener("click", () => renderTab(b.dataset.tab)));

  function renderTab(tab) {
    state.tab = tab;
    $$("#result-tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
    updateSteps();
    const body = $("#tab-body"), j = state.job;
    if (tab === "inventory") {
      const cats = [...new Set(j.inventory.map((r) => r.category))];
      const ps = j.parse_stats;
      body.innerHTML = `<div class="grid-2"><div class="panel"><h2>Parse completeness: ${esc(ps.completeness)}%</h2>
          <div class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${esc(ps.completeness)}"><span style="width:${esc(ps.completeness)}%"></span></div>
          <p class="muted">${esc(ps.known_elements)} known XML elements · ${esc(ps.unknown_elements)} unknown. Unknown nodes are preserved with their XPath for review, never dropped silently.</p>
          <ul class="notes"><li>Package checks passed: no path traversal or symbolic links, size and compression limits respected.</li>
          <li>XML parsed with external entities and DTDs disabled.</li><li>No connection credentials are stored or exported.</li></ul></div>
        <div class="panel"><h2>Workbook</h2><div class="table-wrap"><table><tbody>
          <tr><td>Tableau version</td><td>${esc(j.workbook.version)} (${esc(j.workbook.build || "")})</td></tr>
          <tr><td>Packaged files</td><td>${esc(j.workbook.packaged_files.join(", ") || "none")}</td></tr>
          <tr><td>Data sources</td><td>${j.datasources.map((d) => esc(d.caption) + " (" + esc(d.model_style) + ")").join(", ")}</td></tr>
          <tr><td>Worksheets / dashboards</td><td>${j.worksheets.length} / ${j.dashboards.length}</td></tr></tbody></table></div></div></div>
        ${cats.map((c) => `<h3>${esc(c)}</h3><div class="table-wrap"><table><thead><tr><th style="width:34%">Item</th><th style="width:80px">Count</th><th style="width:120px">Support</th><th>Notes</th></tr></thead><tbody>
          ${j.inventory.filter((r) => r.category === c).map((r) => `<tr><td>${esc(r.item)}</td><td class="num">${esc(r.count)}</td><td>${pill(r.status)}</td><td class="muted">${esc(r.note)}</td></tr>`).join("")}</tbody></table></div>`).join("")}
        ${j.unknown_nodes.length ? `<h3>Unknown nodes (preserved for review)</h3><div class="table-wrap"><table><thead><tr><th>XPath</th><th>Tag</th></tr></thead><tbody>
          ${j.unknown_nodes.slice(0, 100).map((u) => `<tr><td><code>${esc(u.path)}</code></td><td>${esc(u.tag)}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
    } else if (tab === "calcs") {
      const statuses = ["All", "PASS", "REVIEW", "UNSUPPORTED", "Overridden"];
      body.innerHTML = `<p class="muted" style="margin-top:0">Each calculation is classified by rule. <b>PASS</b>: a tested deterministic pattern.
          <b>REVIEW</b>: converted, but its semantics need checking (LOD, table calc, security, overrides). <b>UNSUPPORTED</b>: needs a manual rewrite.
          You can override any DAX here. Overrides are checked against the model and keep the original for lineage.</p>
        <div class="toolbar"><input type="search" id="calc-q" placeholder="Search name, formula or DAX" aria-label="Search calculations">
        <div class="chips">${statuses.map((s, i) => `<button class="chip" data-st="${esc(s)}" aria-pressed="${i === 0}">${esc(s)}</button>`).join("")}</div></div>
        <div class="table-wrap"><table><thead><tr><th style="width:17%">Tableau field</th><th style="width:26%">Tableau formula</th><th>Power BI DAX</th><th style="width:150px">Classification</th></tr></thead><tbody id="calc-body"></tbody></table></div>`;
      let filt = "All";
      const draw = () => {
        const q = $("#calc-q").value.toLowerCase();
        const rows = j.calculations.filter((m) => (filt === "All" || m.classification === filt || (filt === "Overridden" && m.overridden)) &&
          (!q || [m.tableau_name, m.tableau_formula, m.dax, m.pbi_object].join(" ").toLowerCase().includes(q)));
        $("#calc-body").innerHTML = rows.map((m, i) => `<tr data-obj="${esc(m.pbi_object)}"><td><b>${esc(m.tableau_name)}</b><div class="muted">${esc(m.kind)} · ${esc(m.datasource)}</div>
            ${m.xml_path ? `<div class="muted" style="font-size:11px;word-break:break-all"><code>${esc(m.xml_path)}</code></div>` : ""}</td>
          <td>${code(m.tableau_formula)}</td>
          <td><div class="muted" style="margin-bottom:4px">${esc(m.pbi_table)} → <b>${esc(m.pbi_object)}</b> · <code>${esc(m.strategy)}</code></div>${code(m.dax || "—")}
          ${m.overridden ? `<div class="muted">Original: <code>${esc(m.original_dax)}</code>${m.override_note ? " · " + esc(m.override_note) : ""}</div>` : ""}
          ${m.warnings.length ? `<ul class="notes">${m.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
          ${["base-measure", "date-part", "parameter"].includes(m.kind) || !m.pbi_object ? "" : `<div class="actions" style="margin-top:6px">
            <button class="btn small" data-edit="${esc(m.pbi_object)}">Override DAX</button>
            ${state.health.ai_available ? `<button class="btn small" data-ai="${esc(m.pbi_object)}">Suggest with AI</button>` : ""}</div>
            <div class="override" data-editor="${esc(m.pbi_object)}" hidden><label class="field" for="ov-${i}">New DAX for ${esc(m.pbi_object)}</label>
            <textarea id="ov-${i}" spellcheck="false">${esc(m.dax)}</textarea><input type="text" placeholder="Reason / note (kept in lineage)" data-note>
            <div class="actions" style="margin-top:6px"><button class="btn small primary" data-save="${esc(m.pbi_object)}">Apply override</button>
            <button class="btn small" data-cancel="${esc(m.pbi_object)}">Cancel</button></div><div class="muted" data-msg></div></div>`}</td>
          <td>${pill(m.classification)}<div class="muted" style="margin-top:4px">${esc(m.status)} · ${esc(m.confidence)} confidence</div>${m.overridden ? pill("review", "overridden") : ""}</td></tr>`).join("") ||
          `<tr><td colspan="4" class="empty">No calculations match.</td></tr>`;
      };
      $("#calc-q").addEventListener("input", draw);
      $$(".chip[data-st]").forEach((c) => c.addEventListener("click", () => {
        filt = c.dataset.st;
        $$(".chip[data-st]").forEach((x) => x.setAttribute("aria-pressed", x === c));
        draw();
      }));
      body.onclick = async (e) => {
        const ed = e.target.closest("[data-edit]"), cancel = e.target.closest("[data-cancel]"), save = e.target.closest("[data-save]"), ai = e.target.closest("[data-ai]");
        const box = (name) => body.querySelector(`[data-editor="${CSS.escape(name)}"]`);
        if (ed) box(ed.dataset.edit).hidden = false;
        if (cancel) box(cancel.dataset.cancel).hidden = true;
        if (ai) {
          const b = box(ai.dataset.ai);
          b.hidden = false;
          b.querySelector("[data-msg]").innerHTML = `<span class="spinner"></span> Asking Claude (formula and model names only; identifiers redacted)…`;
          try {
            const r = await api(`/api/jobs/${j.job_id}/ai-suggest`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ object: ai.dataset.ai }) });
            b.querySelector("textarea").value = r.dax;
            b.querySelector("[data-note]").value = "AI suggestion (" + r.confidence + "): " + r.explanation.slice(0, 160);
            b.dataset.source = "ai";
            b.querySelector("[data-msg]").innerHTML = `${pill("review", "REVIEW")} ${r.parse_ok && r.references_ok ? "Parses and references model objects." : esc(r.problems.join("; "))} ${esc(r.explanation)}`;
          } catch (err) { b.querySelector("[data-msg]").textContent = err.message; }
        }
        if (save) {
          const b = box(save.dataset.save);
          try {
            const r = await api(`/api/jobs/${j.job_id}/override`, { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ object: save.dataset.save, dax: b.querySelector("textarea").value, note: b.querySelector("[data-note]").value, source: b.dataset.source || "manual" }) });
            state.job = { ...(await api(`/api/jobs/${j.job_id}`)), validation: null };
            if (state.validation) state.validation.stale = true;
            toast(r.message);
            renderJob();
          } catch (err) { b.querySelector("[data-msg]").textContent = err.message; }
        }
      };
      draw();
    } else if (tab === "security") {
      body.innerHTML = `<div class="panel"><h2>Row-level security</h2>${j.roles.length ? `<div class="table-wrap"><table><thead><tr><th>Power BI role</th><th>Table</th><th>Filter (DAX)</th><th>From Tableau</th></tr></thead><tbody>
          ${j.roles.map((r) => Object.entries(r.table_permissions).map(([t, d]) => `<tr><td><b>${esc(r.name)}</b></td><td>${esc(t)}</td><td>${code(d)}</td>
            <td>${esc((j.security_findings.find((f) => f.calculation === r.name) || {}).formula || r.tableau)}</td></tr>`).join("")).join("")}</tbody></table></div>
          <ul class="notes"><li>Security parity is a hard gate (gate 4). The Validation center simulates each identity against both models.</li>
          <li>Before go-live, assign Entra ID users or groups to the role and test permitted and forbidden users with "View as".</li></ul>`
          : `<p class="muted">No Tableau user filters or USERNAME() functions found. Confirm the published content has no server-side permissions that need reproducing.</p>`}</div>
        ${j.security_findings.length ? `<div class="panel"><h2>Findings</h2><div class="table-wrap"><table><thead><tr><th>Calculation</th><th>Formula</th><th>Applied as</th><th>Source</th></tr></thead><tbody>
          ${j.security_findings.map((f) => `<tr><td>${esc(f.calculation)}</td><td><code>${esc(f.formula)}</code></td><td>${esc(f.applied_as)}</td><td><code>${esc(f.xml_path)}</code></td></tr>`).join("")}</tbody></table></div></div>` : ""}`;
    } else if (tab === "data") {
      body.innerHTML = j.datasources.map((ds) => `<div class="panel"><h2>${esc(ds.caption)} <span class="pill neutral">${esc(ds.model_style)}</span>
          ${ds.is_extract ? '<span class="pill info">extract</span>' : ""}</h2>
        <div class="table-wrap"><table><thead><tr><th>Connection</th><th>Class</th><th>Server</th><th>Database</th><th>File</th></tr></thead><tbody>
          ${ds.connections.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(c.class)}</td><td>${esc(c.server || "")}</td><td>${esc(c.database || "")}</td><td>${esc(c.file || "")}</td></tr>`).join("")}
        </tbody></table></div>
        <h3>Tables</h3><div class="table-wrap"><table><thead><tr><th>Logical table</th><th>Type</th><th>Physical</th><th>Columns</th></tr></thead><tbody>
          ${ds.tables.map((t) => `<tr><td>${esc(t.name)}</td><td>${esc(t.type === "text" ? "custom SQL" : t.type)}</td><td>${esc(t.table || "")}</td>
            <td>${ds.columns.filter((c) => c.table === t.name).length}</td></tr>`).join("")}</tbody></table></div>
        ${ds.relationships.length ? `<h3>${ds.model_style === "joins" ? "Joins" : "Relationships"}</h3><ul>${ds.relationships.map((r) =>
          `<li>${esc(r.left_table)}.${esc(r.left_column)} = ${esc(r.right_table)}.${esc(r.right_column)} <span class="muted">(${esc(r.join_type)})</span></li>`).join("")}</ul>` : ""}
      </div>`).join("") + (j.sql.length ? `<div class="panel"><h2>Custom SQL → Power Query</h2>${j.sql.map((s) => `<h3>${esc(s.table)}</h3>
        <div class="grid-2"><div><div class="muted">Tableau SQL</div>${code(s.sql)}</div><div><div class="muted">Power Query (M)</div>${code(s.m || "")}</div></div>
        <ul class="notes">${(s.findings.length ? s.findings : ["No best-practice issues found."]).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`).join("")}</div>` : "");
    } else if (tab === "model") {
      const rels = j.model.relationships;
      body.innerHTML = `<div class="panel"><h2>Relationships</h2>${rels.length ? `<div class="table-wrap"><table><thead><tr><th>From (many)</th><th>To (one)</th><th>Filter direction</th><th>From Tableau</th></tr></thead><tbody>
        ${rels.map((r) => `<tr><td>${esc(r.from_table)}[${esc(r.from_column)}]</td><td>${esc(r.to_table)}[${esc(r.to_column)}]</td><td>${esc(r.cross_filter)}</td><td>${esc(r.tableau_join)}</td></tr>`).join("")}
        </tbody></table></div>` : '<div class="muted">Single-table model.</div>'}</div>` +
        j.model.tables.map((t) => `<details class="expander"><summary><span>${esc(t.name)} <span class="pill neutral">${esc(t.kind)}</span></span>
          <span class="muted">${t.columns.length} columns · ${t.measures.length} measures</span></summary><div>
          ${t.m ? `<h3>Power Query</h3>${code(t.m)}` : ""}${t.dax_source ? `<h3>Calculated table</h3>${code(t.dax_source)}` : ""}
          ${t.measures.length ? `<h3>Measures</h3><div class="table-wrap"><table><tbody>${t.measures.map((m) => `<tr><td style="width:25%"><b>${esc(m.name)}</b>
            <div class="muted">${esc(m.folder || "")}</div></td><td>${code(m.expression)}</td></tr>`).join("")}</tbody></table></div>` : ""}
          <h3>Columns</h3><div class="table-wrap"><table><thead><tr><th>Name</th><th>Type</th><th>Summarize</th><th>Definition</th></tr></thead><tbody>
          ${t.columns.map((c) => `<tr><td>${esc(c.name)}${c.hidden ? ' <span class="pill neutral">hidden</span>' : ""}</td><td>${esc(c.datatype)}</td><td>${esc(c.summarize_by)}</td>
            <td>${c.expression ? code(c.expression) : '<span class="muted">source column</span>'}</td></tr>`).join("")}</tbody></table></div></div></details>`).join("");
    } else if (tab === "layout") {
      body.innerHTML = `<div class="toolbar"><label for="page-sel" class="visually-hidden">Page</label><select id="page-sel" style="max-width:360px">
        ${j.pages.map((p, i) => `<option value="${i}">${esc(p.display_name)} (${esc(p.source)})</option>`).join("")}</select></div><div id="page-view"></div>`;
      const draw = () => {
        const p = j.pages[+$("#page-sel").value];
        const vis = p.visuals.map((v) => `<div class="vis ${esc(v.type)}" style="left:${(100 * v.x) / p.width}%;top:${(100 * v.y) / p.height}%;width:${(100 * v.w) / p.width}%;height:${(100 * v.h) / p.height}%">
          <b>${esc(v.type)}</b>${esc(v.title || v.text || "")}</div>`).join("");
        $("#page-view").innerHTML = `<div class="canvas" role="img" aria-label="Wireframe of page ${esc(p.display_name)}">${vis}</div>
          <h3>Visuals</h3><div class="table-wrap"><table><thead><tr><th>Visual</th><th>Source worksheet</th><th>Fields</th><th>Filters</th><th>Notes</th></tr></thead><tbody>
          ${p.visuals.map((v) => `<tr><td>${pill("info", v.type)}</td><td>${esc(v.worksheet || "—")}</td>
            <td>${Object.entries(v.roles).map(([r, items]) => `<div><span class="muted">${esc(r)}:</span> ${items.map((i) => esc(i.name)).join(", ")}</div>`).join("")}</td>
            <td>${v.filters.map((f) => `<div>${esc(f.kind)} ${esc(f.column || f.measure || "")}</div>`).join("")}</td>
            <td class="muted">${esc((v.notes || []).join(" "))}</td></tr>`).join("")}</tbody></table></div>`;
      };
      $("#page-sel").addEventListener("change", draw);
      draw();
    } else if (tab === "filters") {
      body.innerHTML = `<div class="panel"><h2>Parameters</h2>${j.parameters.length ? `<div class="table-wrap"><table><thead><tr><th>Parameter</th><th>Type</th><th>Current</th><th>Domain</th><th>Power BI</th></tr></thead><tbody>
        ${j.parameters.map((p) => `<tr><td><b>${esc(p.name)}</b></td><td>${esc(p.datatype)}</td><td>${esc(p.value)}</td>
          <td>${esc(p.domain)} ${p.domain === "range" ? `${esc(p.min)} – ${esc(p.max)} step ${esc(p.step)}` : esc(p.members.join(", "))}</td>
          <td>Table '${esc(p.name)}' + measure [${esc(p.name)} Value]</td></tr>`).join("")}</tbody></table></div>` : '<div class="muted">No parameters.</div>'}</div>
        <div class="panel"><h2>Worksheet filters</h2><div class="table-wrap"><table><thead><tr><th>Worksheet</th><th>Tableau filter</th><th>Power BI visual filter</th></tr></thead><tbody>
        ${j.worksheets.flatMap((w) => w.filters.map((f, i) => {
          const pf = (w.visual && w.visual.filters) || [];
          const m = pf[i] || pf.find((x) => x.kind === f.kind) || null;
          const t = `${f.kind}${f.context ? " (context)" : ""} on ${f.field}` + (f.members.length ? ` ${f.exclude ? "NOT IN" : "IN"} (${f.members.join(", ")})` : "") +
            (f.min !== null || f.max !== null ? ` [${f.min ?? "…"}, ${f.max ?? "…"}]` : "") + (f.top_n ? ` top ${f.top_n} by ${f.top_by}` : "");
          const p = m ? `${m.kind} on ${m.table ? m.table + "[" + m.column + "]" : "[" + m.measure + "]"}` + (m.values && m.values.length ? ` ${m.exclude ? "NOT IN" : "IN"} (${m.values.join(", ")})` : "") +
            (m.top_n ? ` top ${m.top_n} by [${m.by_measure}]` : "") + (m.min !== null || m.max !== null ? ` [${m.min ?? "…"}, ${m.max ?? "…"}]` : "") : "—";
          return `<tr><td>${esc(w.name)}</td><td><code>${esc(t)}</code></td><td><code>${esc(p)}</code></td></tr>`;
        })).join("") || '<tr><td colspan="3" class="muted">No worksheet filters.</td></tr>'}</tbody></table></div></div>`;
    } else if (tab === "issues") {
      const order = { error: 0, warning: 1, info: 2 };
      const issues = [...j.issues].sort((a, b) => order[a.severity] - order[b.severity]);
      body.innerHTML = `<div class="table-wrap"><table><thead><tr><th style="width:100px">Severity</th><th>Area</th><th>Object</th><th>What to do</th></tr></thead><tbody>
        ${issues.map((i) => `<tr><td>${pill(i.severity === "error" ? "fail" : i.severity === "warning" ? "warn" : "info", i.severity)}</td><td>${esc(i.area)}</td><td>${esc(i.object)}</td><td>${esc(i.message)}</td></tr>`).join("") ||
        '<tr><td colspan="4" class="empty">No issues.</td></tr>'}</tbody></table></div>`;
    }
  }

  // ------------------------------------------------------------- validate
  function renderValidateForm() {
    const j = state.job;
    $("#validate-empty").hidden = !!j;
    $("#validate-form").hidden = !j;
    if (!j) return;
    $("#opt-sample").hidden = !j.sample;
    if (!j.sample) $("input[name=src][value=sqlite]").checked = true;
    if (j.sample && !$("#as-of").value) $("#as-of").value = "2025-12-31";
    srcChange();
    if (state.validation) renderValidation(state.validation);
  }
  function srcChange() {
    const v = $("input[name=src]:checked").value;
    $("#src-sqlite").hidden = v !== "sqlite";
    $("#src-csv").hidden = v !== "csv";
  }
  $$("input[name=src]").forEach((r) => r.addEventListener("change", srcChange));
  $("#btn-validate").addEventListener("click", async (e) => {
    const btn = e.currentTarget, j = state.job, f = new FormData();
    const src = $("input[name=src]:checked").value;
    if (src === "sample") f.append("use_sample_data", "true");
    if (src === "sqlite") {
      if (!$("#in-sqlite").files[0] && !j.workbook.packaged_files.length) return toast("Choose a SQLite file", true);
      if ($("#in-sqlite").files[0]) f.append("sqlite", $("#in-sqlite").files[0]);
    }
    if (src === "csv") for (const file of $("#in-csv").files) f.append("csvs", file);
    f.append("abs_tol", $("#tol-abs").value || "0.01");
    f.append("rel_tol", $("#tol-rel").value || "0.000001");
    if ($("#as-of").value) f.append("as_of", $("#as-of").value);
    f.append("scenarios", $("#scen").checked ? "true" : "false");
    const label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> Validating…`;
    $("#validate-result").innerHTML = `<div class="panel empty"><span class="spinner"></span> Running both engines and comparing results…</div>`;
    try {
      state.validation = await api(`/api/jobs/${j.job_id}/validate`, { method: "POST", body: f });
      state.job.history = state.validation.history;
      renderValidation(state.validation);
      updateSteps();
    } catch (err) {
      $("#validate-result").innerHTML = "";
      toast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
    }
  });

  function renderValidation(v) {
    const s = v.summary;
    const gates = v.gates || [];
    const failed = gates.find((g) => ["FAIL", "BLOCKED"].includes(g.status));
    const cls = failed ? "fail" : gates.some((g) => ["PENDING", "WARN"].includes(g.status)) ? "warn" : "pass";
    const hist = v.history || state.job.history || [];
    const rd = v.readiness || { metrics: [] };
    const so = state.job.signoff;
    $("#validate-result").innerHTML = `${v.stale ? `<div class="banner warn"><div><b>Results are stale.</b><p>DAX was overridden after this run. Run the validation again.</p></div></div>` : ""}
      <div class="banner ${cls}"><div class="big">${esc(failed ? "FAIL" : s.overall)}</div>
      <div><b>${esc(v.verdict || "")}</b><p>${s.PASS} passed · ${s.WARN} warnings · ${s.FAIL} failed · ${s.ERROR} errors · ${s.PENDING} pending human checks · ${s.SKIP} skipped · ${v.duration_s}s</p></div>
      <div class="actions" style="margin-left:auto"><a class="btn" href="/api/jobs/${state.job.job_id}/validation/report" target="_blank" rel="noopener">Validation report</a>
      <a class="btn" href="/api/jobs/${state.job.job_id}/artifact/validation_results.csv">validation_results.csv</a></div></div>
      <div class="gates">${gates.map((g) => `<div class="gate ${pillClass(g.status)}"><div class="k">Gate ${g.gate}</div><b>${esc(g.name)}</b>
        <div>${pill(g.status)}</div><div class="s">${g.automated_checks} automated · ${g.passed} pass${g.failed ? " · " + g.failed + " fail" : ""}${g.warnings ? " · " + g.warnings + " warn" : ""}</div>
        ${g.pending_human_checks.length ? `<div class="s">Human: ${esc(g.pending_human_checks.join("; "))}</div>` : ""}${g.blocked_by ? `<div class="s">Blocked by gate ${g.blocked_by}</div>` : ""}</div>`).join("")}</div>
      <h2>Readiness score ${rd.index != null ? `<span class="pill neutral">index ${rd.index}</span>` : ""}</h2>
      <div class="stats">${rd.metrics.map((m) => `<div class="stat"><div class="k">${esc(m.metric)}</div><div class="v">${m.value == null ? "n/a" : esc(m.value) + (m.unit ? " min" : "%")}</div><div class="s">${esc(m.detail)}</div></div>`).join("")}</div>
      ${(v.kpi_register || []).length ? `<h2>KPI regression register</h2><div class="table-wrap" style="max-height:360px"><table><thead><tr><th>Scenario</th><th>KPI</th><th>Tableau</th><th>Power BI</th><th>Delta</th><th>Tolerance</th><th>Status</th></tr></thead><tbody>
        ${v.kpi_register.map((r) => `<tr><td>${esc(r.scenario)}</td><td>${esc(r.metric)}</td><td class="num">${fmt(r.tableau)}</td><td class="num">${fmt(r.powerbi)}</td><td class="num">${fmt(r.delta)}</td><td class="num">${fmt(r.tolerance)}</td><td>${pill(r.status)}${r.note ? `<div class="muted">${esc(r.note)}</div>` : ""}</td></tr>`).join("")}</tbody></table></div>` : ""}
      ${(v.identity_matrix || []).length ? `<h2>Security identity matrix (simulated)</h2><div class="table-wrap"><table><thead><tr><th>User</th><th>Metric</th><th>Tableau USERNAME() filter</th><th>Power BI role</th><th>Status</th></tr></thead><tbody>
        ${v.identity_matrix.map((r) => `<tr><td>${esc(r.user)}</td><td>${esc(r.metric)}</td><td class="num">${fmt(r.tableau)}</td><td class="num">${fmt(r.powerbi)}</td><td>${pill(r.status)}</td></tr>`).join("")}</tbody></table></div>
        <p class="muted">Simulation proves the role logic. Live testing with real users and SSO is still required (pending in gate 4).</p>` : ""}
      <h2>Gate 5: business UAT sign-off</h2>
      <div class="panel">${so ? `<p>${pill(so.decision)} by <b>${esc(so.approver)}</b> on ${esc(so.recorded_at)} (validation run ${esc(so.validation_run)}). ${esc(so.notes)}</p>` : ""}
        <div class="row"><div><label class="field" for="so-who">Business owner</label><input type="text" id="so-who" placeholder="Name and role"></div>
        <div><label class="field" for="so-dec">Decision</label><select id="so-dec"><option value="approved">Approve</option><option value="approved-with-redesign">Approve with recorded redesign</option><option value="rejected">Reject</option></select></div></div>
        <label class="field" for="so-notes">Decision notes</label><input type="text" id="so-notes" placeholder="Scenarios reviewed, accepted limitations, redesign decisions">
        <div class="actions" style="margin-top:10px"><button class="btn primary" id="btn-signoff">Record decision</button></div></div>
      <h2>Checks</h2>
      <div class="toolbar"><div class="chips">${["All", "FAIL", "WARN", "PENDING", "PASS", "ERROR", "SKIP", "N/A"].map((x, i) => `<button class="chip" data-vf="${x}" aria-pressed="${i === 0}">${x}</button>`).join("")}</div></div>
      ${gates.map((g) => `<h3>Gate ${g.gate}: ${esc(g.name)} ${pill(g.status)}</h3><div class="checks">${v.checks.map((c) => c.gate !== g.gate ? "" : `
        <details class="expander" data-status="${esc(c.status)}"><summary><span>${pill(c.status)} ${esc(c.name)}</span><span class="muted" style="text-align:right">${esc(c.detail)}</span></summary>
        <div>${checkDetail(c)}</div></details>`).join("")}</div>`).join("")}
      ${hist.length ? `<h2>Run history</h2><div class="table-wrap"><table><thead><tr><th>Run</th><th>Started</th><th>Result</th><th>Pass rate</th><th>Verdict</th><th>Overrides</th></tr></thead><tbody>
        ${hist.map((h) => `<tr><td>${h.run}</td><td>${esc(h.at)}</td><td>${pill(h.overall)}</td><td>${h.pass_rate}%</td><td>${esc(h.verdict)}</td><td>${h.overrides}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
    $$(".chip[data-vf]").forEach((c) => c.addEventListener("click", () => {
      $$(".chip[data-vf]").forEach((x) => x.setAttribute("aria-pressed", x === c));
      $$("#validate-result details[data-status]").forEach((d) => (d.hidden = c.dataset.vf !== "All" && d.dataset.status !== c.dataset.vf));
    }));
    $("#btn-signoff").addEventListener("click", async () => {
      try {
        const r = await api(`/api/jobs/${state.job.job_id}/signoff`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approver: $("#so-who").value, decision: $("#so-dec").value, notes: $("#so-notes").value }) });
        state.job.signoff = r.signoff;
        v.gates = r.gates;
        v.verdict = r.verdict;
        v.checks.filter((c) => c.gate === 5).forEach((c) => { c.status = r.gates[4].status === "BLOCKED" ? r.gates[4].status_if_unblocked : r.gates[4].status; c.detail = `${r.signoff.decision} by ${r.signoff.approver}`; });
        toast("Decision recorded");
        renderValidation(v);
        updateSteps();
      } catch (e) { toast(e.message, true); }
    });
  }

  function renderExport() {
    const body = $("#export-body"), j = state.job;
    if (!j) return;
    const id = j.job_id, v = state.validation;
    const items = [
      ["Power BI project (PBIP)", `/api/jobs/${id}/pbip`, "Zip with .pbip, TMDL semantic model (incl. RLS roles), PBIR report and every file below. Open it in Power BI Desktop.", true],
      ["migration_report.json", `/api/jobs/${id}/artifact/migration_report.json`, "Inventory, mapping manifest, model, pages, issues, validation, sign-off and audit trail.", true],
      ["lineage.csv", `/api/jobs/${id}/artifact/lineage.csv`, "Every Power BI column, measure, role and visual traced back to its Tableau source (XPath), rule, confidence, evidence and override.", true],
      ["ir.json", `/api/jobs/${id}/artifact/ir.json`, "Canonical intermediate representation v0.1.0 (JSON Schema validated): sources, fields, calculations with AST and dependencies.", true],
      ["DAX_MAPPING.csv", `/api/jobs/${id}/mapping.csv`, "Tableau formula next to its DAX, with PASS/REVIEW/UNSUPPORTED classification.", true],
      ["MIGRATION_REPORT.html", `/api/jobs/${id}/report`, "Readable migration dossier.", true],
      ["validation_results.csv", `/api/jobs/${id}/artifact/validation_results.csv`, "Gate, scenario, metric, Tableau value, Power BI value, delta, tolerance, root cause, status, owner and approval notes.", !!v],
      ["VALIDATION_REPORT.html", `/api/jobs/${id}/validation/report`, "Gates, readiness and cell-level evidence.", !!v],
      ["uat_signoff.json", `/api/jobs/${id}/artifact/uat_signoff.json`, "Business owner decision (gate 5).", !!j.signoff],
    ];
    body.innerHTML = `<div class="panel"><h2>${esc(j.workbook.name)}</h2><div class="table-wrap"><table><thead><tr><th>Artifact</th><th>Contents</th><th></th></tr></thead><tbody>
      ${items.map(([n, href, d, ok]) => `<tr><td><b>${esc(n)}</b></td><td class="muted">${esc(d)}</td><td>${ok ? `<a class="btn small" href="${href}" ${href.endsWith("report") ? 'target="_blank" rel="noopener"' : ""}>Download</a>` : '<span class="muted">Run validation first</span>'}</td></tr>`).join("")}
      </tbody></table></div>
      <h3>Before go-live</h3><ol class="notes"><li>Open the .pbip in Power BI Desktop and record openability (not testable in this environment).</li>
      <li>Set data source credentials and refresh. Re-run the validation against the same refresh as Tableau.</li>
      <li>Complete the pending human checks: interactions (gate 3) and live identity tests (gate 4).</li>
      <li>Record the business sign-off (gate 5). Publishing to the Power BI service needs explicit approval.</li></ol></div>`;
  }
  function checkDetail(c) {
    const d = c.data || {};
    if (d.register) {
      return `<div class="table-wrap"><table><thead><tr><th>KPI</th><th>Power BI measure</th><th>Tableau</th><th>Power BI</th><th>Delta</th><th>Status</th></tr></thead><tbody>
        ${d.register.map((r) => `<tr><td>${esc(r.metric)}</td><td>${esc(r.measure)}</td><td class="num">${fmt(r.tableau)}</td><td class="num">${fmt(r.powerbi)}</td><td class="num">${fmt(r.delta)}</td><td>${pill(r.status)}</td></tr>`).join("")}</tbody></table></div>`;
    }
    if (d.missing && !d.rows) {
      return d.missing.length ? `<ul class="notes">${d.missing.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : `<p class="muted">${esc(c.detail)}</p>`;
    }
    if (d.permissions) return Object.entries(d.permissions).map(([t, x]) => `<p>${esc(t)}</p>${code(x)}`).join("");
    if (d.columns) {
      const keys = Object.keys(d.columns[0] || {});
      return `<div class="table-wrap"><table><thead><tr>${keys.map((k) => `<th>${esc(k)}</th>`).join("")}</tr></thead><tbody>
        ${d.columns.map((r) => `<tr>${keys.map((k) => `<td class="${typeof r[k] === "number" ? "num" : ""}">${k === "status" ? pill(r[k]) : fmt(r[k])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    }
    if (!d.rows) return `<p class="muted">${esc(c.detail)}</p>${d.manual && d.manual.length ? `<p>Manual: ${esc(d.manual.join(", "))}</p>` : ""}`;
    const totals = d.summary.totals || {};
    const mm = d.measure_map || {};
    const lb = d.labels || {};
    return `<div class="row" style="align-items:flex-start"><div>
      <h3>Totals</h3><div class="table-wrap"><table><thead><tr><th>Tableau pill → Power BI measure</th><th>Tableau</th><th>Power BI</th><th>Diff</th></tr></thead><tbody>
      ${Object.entries(totals).map(([m, t]) => `<tr><td>${esc(lb[m] || m)} → <b>${esc(mm[m] || "")}</b></td><td class="num">${fmt(t.tableau)}</td><td class="num">${fmt(t.powerbi)}</td><td class="num">${fmt(t.diff)}</td></tr>`).join("")}
      </tbody></table></div></div></div>
      <h3>Rows ${d.summary.mismatched_rows ? `<span class="pill fail">${d.summary.mismatched_rows} mismatched</span>` : ""} <span class="muted">(match rate ${d.summary.match_rate}%)</span></h3>
      <div class="table-wrap" style="max-height:360px"><table><thead><tr>${d.keys.map((k) => `<th>${esc(lb[k] || k)}</th>`).join("")}${d.measures.map((m) => `<th>${esc(lb[m] || mm[m] || m)}<br>Tableau | Power BI</th>`).join("")}<th>Status</th></tr></thead><tbody>
      ${d.rows.map((r) => `<tr>${r.key.map((k) => `<td>${esc(k)}</td>`).join("")}${r.values.map((x) => `<td class="num" ${x.status && x.status !== "MATCH" ? 'style="color:var(--fail);font-weight:700"' : ""}>${fmt(x.tableau)} | ${fmt(x.powerbi)}</td>`).join("")}<td>${pill(r.status)}</td></tr>`).join("")}
      </tbody></table></div>${d.dax_query ? `<h3>DAX query (run in DAX Studio or the executeQueries API)</h3>${code(d.dax_query)}` : ""}`;
  }

  // ------------------------------------------------------------- translate
  const EXAMPLES = ["SUM([Profit]) / SUM([Sales])", "{FIXED [Customer Name] : MIN([Order Date])}", "{INCLUDE [Customer Name] : SUM([Sales])}",
    "RUNNING_SUM(SUM([Sales]))", "RANK(SUM([Sales]))", "IF [Profit] > 0 THEN 'Profit' ELSE 'Loss' END", "DATEDIFF('day', [Order Date], [Ship Date])",
    "DATETRUNC('quarter', [Order Date])", "ZN([Discount]) * [Sales]", "COUNTD([Order ID])", "LOOKUP(SUM([Sales]), -1)"];
  $("#examples").innerHTML = EXAMPLES.map((x) => `<button class="chip" data-ex="${esc(x)}">${esc(x.length > 34 ? x.slice(0, 32) + "…" : x)}</button>`).join("");
  $$("[data-ex]").forEach((b) => b.addEventListener("click", () => { $("#formula").value = b.dataset.ex; translate(); }));
  async function translate() {
    const out = $("#translate-out");
    out.innerHTML = `<div class="empty"><span class="spinner"></span></div>`;
    try {
      const body = { formula: $("#formula").value };
      if (state.job && $("#use-job").checked) body.job_id = state.job.job_id;
      const r = await api("/api/translate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) { out.innerHTML = `<h2>${pill("fail", "Not converted")}</h2><p>${esc(r.error)}</p>`; return; }
      out.innerHTML = `<h2 style="display:flex;gap:8px;align-items:center">DAX ${pill(r.status)} <span class="pill neutral">${esc(r.object)}</span></h2>
        ${code(r.dax)}${r.warnings.length ? `<h3>Notes</h3><ul class="notes">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
        <p class="muted" style="margin-top:12px">${esc(r.schema)} · functions: ${esc(r.functions.join(", "))}</p>`;
    } catch (e) { out.innerHTML = `<p>${esc(e.message)}</p>`; }
  }
  $("#btn-translate").addEventListener("click", translate);
  $("#formula").addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") translate(); });

  // ------------------------------------------------------------- reference
  async function loadCatalog() {
    if (state.catalog) return;
    try {
      state.catalog = await api("/api/catalog");
    } catch (e) { toast(e.message, true); return; }
    const c = state.catalog;
    $("#ref-schema").textContent = "Each example was generated by the converter itself. " + c.schema;
    const cats = ["All", ...new Set(c.functions.map((f) => f.category))];
    let cat = "All";
    $("#ref-cats").innerHTML = cats.map((x, i) => `<button class="chip" data-cat="${esc(x)}" aria-pressed="${i === 0}">${esc(x)}</button>`).join("");
    const draw = () => {
      const q = $("#ref-search").value.toLowerCase();
      $("#ref-body").innerHTML = c.functions.filter((f) => (cat === "All" || f.category === cat) && (!q || JSON.stringify(f).toLowerCase().includes(q)))
        .map((f) => `<tr><td>${esc(f.category)}</td><td>${code(f.tableau)}</td><td>${code(f.dax)}</td><td>${esc(f.object)}</td><td class="muted">${esc(f.notes)}</td></tr>`).join("");
    };
    $$("[data-cat]").forEach((b) => b.addEventListener("click", () => {
      cat = b.dataset.cat;
      $$("[data-cat]").forEach((x) => x.setAttribute("aria-pressed", x === b));
      draw();
    }));
    $("#ref-search").addEventListener("input", draw);
    $("#ref-struct").innerHTML = c.structural.map((s) => `<tr><td>${esc(s.area)}</td><td><b>${esc(s.tableau)}</b></td><td>${esc(s.powerbi)}</td></tr>`).join("");
    draw();
  }

  // ------------------------------------------------------------- compare
  $("#btn-compare").addEventListener("click", async () => {
    const t = $("#cmp-t").files[0], p = $("#cmp-p").files[0];
    if (!t || !p) return toast("Choose both files", true);
    const f = new FormData();
    f.append("tableau", t);
    f.append("powerbi", p);
    if ($("#cmp-keys").value.trim()) f.append("keys", $("#cmp-keys").value);
    f.append("abs_tol", $("#cmp-tol").value || "0.01");
    try {
      const r = await api("/api/compare", { method: "POST", body: f });
      const cls = pillClass(r.status);
      $("#compare-out").innerHTML = `<div class="banner ${cls}"><div class="big">${esc(r.status)}</div><div><b>${r.summary.matched_cells}/${r.summary.total_cells} cells match</b>
        <p>Keys: ${esc(r.matched_columns.keys.join(", "))} · measures: ${esc(r.matched_columns.measures.join(", "))}</p></div></div>
        ${checkDetail({ data: { ...r, measure_map: {} } })}`;
    } catch (e) { toast(e.message, true); }
  });

  // ------------------------------------------------------------- init
  api("/api/health").then((h) => (state.health = h)).catch(() => {});
  loadSamples();
  show((location.hash || "#convert").slice(1).replace(/[^a-z]/g, "") || "convert");
})();
