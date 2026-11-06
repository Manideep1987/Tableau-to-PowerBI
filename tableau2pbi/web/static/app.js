/* Tableau -> Power BI Migrator UI (no dependencies). */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const state = { job: null, file: null, tab: "calcs", validation: null, catalog: null };

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
    if (["pass", "converted", "match"].includes(s)) return "pass";
    if (["warn", "warning", "converted with notes", "null_vs_zero", "extra_blank_row"].includes(s)) return "warn";
    if (["fail", "error", "failed", "manual review", "value_mismatch", "mismatch", "null_mismatch", "missing_in_powerbi", "missing_in_tableau"].includes(s)) return "fail";
    if (["info", "skip"].includes(s)) return "info";
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
    $("#steps").hidden = !["convert", "validate"].includes(view);
    if (view === "reference") loadCatalog();
    if (view === "validate") renderValidateForm();
    if (view === "translate") $("#use-job-wrap").hidden = !state.job;
    history.replaceState(null, "", "#" + view);
    updateSteps();
  }
  $$("nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
  function updateSteps() {
    const done = { 1: !!state.job, 2: !!state.job, 3: !!state.validation, 4: false };
    const view = (location.hash || "#convert").slice(1);
    const active = !state.job ? 1 : view === "validate" ? 3 : state.validation ? 4 : 2;
    $$("#steps li").forEach((li) => {
      const n = +li.dataset.step;
      li.classList.toggle("active", n === active);
      li.classList.toggle("done", done[n] && n !== active);
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
      $("#samples").innerHTML = s.map((x) => `<div class="sample"><div><b>${esc(x.name)}</b><span>${esc(x.description)}</span></div>
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
    $("#job-sub").textContent = `${j.filename} · Tableau ${j.workbook.version || "?"} · converted in ${j.duration_s}s`;
    $("#dl-pbip").href = `/api/jobs/${j.job_id}/pbip`;
    $("#dl-report").href = `/api/jobs/${j.job_id}/report`;
    $("#dl-csv").href = `/api/jobs/${j.job_id}/mapping.csv`;
    const conv = st.converted + st.converted_with_notes, total = conv + st.manual;
    const errors = j.issues.filter((i) => i.severity === "error").length;
    const warns = j.issues.filter((i) => i.severity === "warning").length;
    const cards = [
      ["Auto-converted", `${total ? Math.round((100 * conv) / total) : 100}%`, `${conv} of ${total} calculations & parameters`],
      ["Data sources", st.datasources, `${st.tables} tables · ${st.relationships} relationships`],
      ["Measures", st.measures, `${st.calc_columns} calculated columns`],
      ["Parameters", st.parameters, "→ parameter tables"],
      ["Report", `${st.pages} pages`, `${st.visuals} visuals from ${st.worksheets} sheets`],
      ["Review items", errors + warns, `${errors} need action · ${warns} warnings`],
    ];
    $("#stats").innerHTML = cards.map(([k, v, s]) => `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(s)}</div></div>`).join("");
    $("#c-calcs").textContent = j.calculations.length;
    $("#c-data").textContent = j.datasources.length;
    $("#c-model").textContent = j.model.tables.length;
    $("#c-layout").textContent = j.pages.length;
    $("#c-filters").textContent = j.worksheets.reduce((a, w) => a + w.filters.length, 0) + j.parameters.length;
    $("#c-issues").textContent = j.issues.length;
    renderTab(state.tab);
    updateSteps();
  }
  $$("#result-tabs button").forEach((b) => b.addEventListener("click", () => renderTab(b.dataset.tab)));

  function renderTab(tab) {
    state.tab = tab;
    $$("#result-tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
    const body = $("#tab-body"), j = state.job;
    if (tab === "calcs") {
      const statuses = ["All", "Converted", "Converted with notes", "Manual review", "Failed"];
      body.innerHTML = `<div class="toolbar"><input type="search" id="calc-q" placeholder="Search name, formula or DAX" aria-label="Search calculations">
        <div class="chips">${statuses.map((s, i) => `<button class="chip" data-st="${esc(s)}" aria-pressed="${i === 0}">${esc(s)}</button>`).join("")}</div></div>
        <div class="table-wrap"><table><thead><tr><th style="width:18%">Tableau field</th><th style="width:28%">Tableau formula</th><th>Power BI DAX</th><th style="width:110px">Status</th></tr></thead><tbody id="calc-body"></tbody></table></div>`;
      let filt = "All";
      const draw = () => {
        const q = $("#calc-q").value.toLowerCase();
        const rows = j.calculations.filter((m) => (filt === "All" || m.status === filt) &&
          (!q || [m.tableau_name, m.tableau_formula, m.dax, m.pbi_object].join(" ").toLowerCase().includes(q)));
        $("#calc-body").innerHTML = rows.map((m) => `<tr><td><b>${esc(m.tableau_name)}</b><div class="muted">${esc(m.kind)} · ${esc(m.datasource)}</div></td>
          <td>${code(m.tableau_formula)}</td>
          <td><div class="muted" style="margin-bottom:4px">${esc(m.pbi_table)} → <b>${esc(m.pbi_object)}</b></div>${code(m.dax || "—")}
          ${m.warnings.length ? `<ul class="notes">${m.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}</td>
          <td>${pill(m.status)}</td></tr>`).join("") || `<tr><td colspan="4" class="empty">No calculations match.</td></tr>`;
      };
      $("#calc-q").addEventListener("input", draw);
      $$(".chip[data-st]").forEach((c) => c.addEventListener("click", () => {
        filt = c.dataset.st;
        $$(".chip[data-st]").forEach((x) => x.setAttribute("aria-pressed", x === c));
        draw();
      }));
      draw();
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
    const s = v.summary, cls = pillClass(s.overall);
    const msg = { pass: "All numbers match between Tableau and Power BI.", warn: "Numbers match, with warnings to review.",
      fail: "Some numbers differ. Review the failed checks below." }[cls];
    const layers = [...new Set(v.checks.map((c) => c.layer))];
    $("#validate-result").innerHTML = `<div class="banner ${cls}"><div class="big">${esc(s.overall)}</div>
      <div><b>${esc(msg)}</b><p>${s.PASS} passed · ${s.WARN} warnings · ${s.FAIL} failed · ${s.ERROR} errors · ${s.SKIP} skipped · pass rate ${s.pass_rate}% · ${v.duration_s}s</p></div>
      <div class="actions" style="margin-left:auto"><a class="btn" href="/api/jobs/${state.job.job_id}/validation/report" target="_blank" rel="noopener">Validation report</a>
      <a class="btn primary" href="/api/jobs/${state.job.job_id}/pbip">Download PBIP</a></div></div>
      <div class="toolbar"><div class="chips">${["All", "FAIL", "WARN", "PASS", "ERROR", "SKIP"].map((x, i) => `<button class="chip" data-vf="${x}" aria-pressed="${i === 0}">${x}</button>`).join("")}</div></div>
      ${layers.map((L) => `<h3>${esc(L)}</h3><div class="checks" data-layer="${esc(L)}">${v.checks.map((c, i) => c.layer !== L ? "" : `
        <details class="expander" data-status="${esc(c.status)}"><summary><span>${pill(c.status)} ${esc(c.name)}</span><span class="muted" style="text-align:right">${esc(c.detail)}</span></summary>
        <div>${checkDetail(c)}</div></details>`).join("")}</div>`).join("")}`;
    $$(".chip[data-vf]").forEach((c) => c.addEventListener("click", () => {
      $$(".chip[data-vf]").forEach((x) => x.setAttribute("aria-pressed", x === c));
      $$("#validate-result details[data-status]").forEach((d) => (d.hidden = c.dataset.vf !== "All" && d.dataset.status !== c.dataset.vf));
    }));
  }
  function checkDetail(c) {
    const d = c.data || {};
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
  loadSamples();
  show((location.hash || "#convert").slice(1).replace(/[^a-z]/g, "") || "convert");
})();
