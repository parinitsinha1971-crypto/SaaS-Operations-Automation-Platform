/* Ops console: small, dependency-free client. Live pages poll the JSON API. */
(function () {
  "use strict";
  const POLL_MS = 5000;
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---- API key (kept in this tab only) ----------------------------------------
  const store = {
    get(k) { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage blocked */ } },
  };
  let memKey = "", memWho = "";
  const getKey = () => memKey || store.get("ops_key");
  const getWho = () => memWho || store.get("ops_who") || "operator";

  function initKeyDialog() {
    const btn = $("#key-btn"), dlg = $("#key-dialog");
    if (!btn || !dlg) return;
    const sync = () => { btn.textContent = getKey() ? "API key set" : "Set API key"; btn.classList.toggle("has-key", !!getKey()); };
    sync();
    btn.addEventListener("click", () => { $("#key-input").value = getKey(); $("#who-input").value = store.get("ops_who"); dlg.showModal(); });
    $("#key-cancel").addEventListener("click", () => dlg.close());
    $("#key-form").addEventListener("submit", () => {
      memKey = $("#key-input").value.trim(); memWho = $("#who-input").value.trim();
      store.set("ops_key", memKey); store.set("ops_who", memWho); sync(); toast("API key saved for this tab.");
    });
  }

  function askKey() { return new Promise((resolve) => {
    const dlg = $("#key-dialog");
    const done = () => { dlg.removeEventListener("close", done); resolve(!!getKey()); };
    dlg.addEventListener("close", done); $("#key-input").value = ""; dlg.showModal();
  }); }

  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    if (opts.method && opts.method !== "GET") {
      if (!getKey() && !(await askKey())) throw new Error("An API key is needed for this action.");
      headers["X-API-Key"] = getKey();
      headers["X-Operator"] = getWho();
    }
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401 && opts.method && opts.method !== "GET") {
      memKey = ""; store.set("ops_key", "");
      throw new Error("The API key was rejected. Check OPS_API_KEY in your .env and set it again.");
    }
    const ct = res.headers.get("content-type") || "";
    const body = ct.includes("json") ? await res.json() : await res.text();
    if (!res.ok) throw new Error((body && body.detail) || `Request failed (${res.status})`);
    return body;
  }

  let toastTimer;
  function toast(msg, isError = false) {
    const t = $("#toast"); if (!t) return;
    t.textContent = msg; t.classList.toggle("error", isError); t.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), isError ? 7000 : 3500);
  }

  // ---- formatting -------------------------------------------------------------------
  const fmt = {
    ms: (v) => (v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${Math.round(v)}ms`),
    pct: (v, d = 0) => (v == null ? "–" : `${Number(v).toFixed(d)}%`),
    ratio: (v) => (v == null ? "–" : `${(v * 100).toFixed(v < 0.01 && v > 0 ? 2 : 1)}%`),
    bytes: (v) => {
      if (v == null) return "–";
      const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
      return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`;
    },
    ago: (iso) => {
      if (!iso) return "";
      const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
      if (s < 60) return `${Math.round(s)}s ago`;
      if (s < 3600) return `${Math.round(s / 60)}m ago`;
      if (s < 86400) return `${Math.round(s / 3600)}h ago`;
      return `${Math.round(s / 86400)}d ago`;
    },
    dur: (s) => {
      s = Math.round(s || 0);
      if (s < 60) return `${s}s`;
      if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
      return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    },
    time: (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : ""),
  };
  const level = (v, warn, crit) => (v == null ? "" : v >= crit ? "crit" : v >= warn ? "warn" : "");

  function ribbon(history, slots = 90) {
    const pad = Math.max(0, slots - history.length);
    let html = "";
    for (let i = 0; i < pad; i++) html += '<i class="pad"></i>';
    for (const h of history.slice(-slots)) {
      html += `<i class="${esc(h.state)}" title="${esc(fmt.time(h.ts))}: ${esc(h.state)}${h.latency_ms != null ? ", " + fmt.ms(h.latency_ms) : ""}"></i>`;
    }
    return html;
  }

  function sparkline(values) {
    const W = 300, H = 64;
    const pts = values.map((v, i) => [i, v]);
    const nums = values.filter((v) => v != null);
    if (nums.length < 2) return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"></svg>`;
    const max = Math.max(...nums) * 1.15 || 1;
    const x = (i) => (values.length < 2 ? 0 : (i / (values.length - 1)) * W);
    const y = (v) => H - (v / max) * H;
    let line = "", area = "", gaps = "", start = null;
    pts.forEach(([i, v]) => {
      if (v == null) { gaps += `<rect class="gap" x="${x(i) - 1}" y="0" width="${W / values.length + 1}" height="${H}"/>`; start = null; return; }
      line += `${start == null ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      if (start == null) start = i;
    });
    const valid = pts.filter(([, v]) => v != null);
    area = `M${x(valid[0][0])},${H}` + valid.map(([i, v]) => `L${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("") + `L${x(valid[valid.length - 1][0])},${H}Z`;
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${gaps}<path class="area" d="${area}"/><path class="line" d="${line}"/></svg>`;
  }

  function incidentItem(i) {
    return `<li><a href="/incidents/${i.id}">${esc(i.title)}</a> <span class="sev ${esc(i.severity)}">${esc(i.severity)}</span>
      <span class="when">${i.status === "open" ? "open for " + fmt.dur(i.duration_s) : "resolved after " + fmt.dur(i.duration_s)}${i.restarts ? `, ${i.restarts} restart${i.restarts > 1 ? "s" : ""}` : ""}</span></li>`;
  }

  function actionItem(a) {
    const verb = { success: "Restarted", failed: "Restart failed for", skipped: "Held off restarting" }[a.status] || a.status;
    const who = a.initiated_by === "auto" ? `automatically (${esc(a.reason)})` : `by ${esc(a.initiated_by)}`;
    const extra = a.status === "success" ? ` in ${Math.round(a.duration_ms || 0)}ms` : a.error ? `: ${esc(a.error)}` : "";
    return `<li><b>${verb} ${esc(a.service)}</b> ${who}${extra}
      <span class="when">${fmt.ago(a.started_at)}${a.incident_id ? ` · <a href="/incidents/${a.incident_id}">incident #${a.incident_id}</a>` : ""}</span></li>`;
  }

  async function poll(fn) {
    const tick = async () => {
      if (document.hidden) return;
      try { await fn(); } catch (e) { console.warn(e); }
    };
    await tick();
    setInterval(tick, POLL_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  }

  async function restart(service) {
    if (!confirm(`Restart ${service} now? Requests in flight will fail.`)) return;
    try {
      const r = await api(`/api/services/${encodeURIComponent(service)}/restart`, { method: "POST" });
      toast(`${service}: ${r.detail}`);
    } catch (e) { toast(e.message, true); }
  }

  // ---- pages ------------------------------------------------------------------------
  const Ops = {};

  Ops.overview = function () {
    poll(async () => {
      const d = await api("/api/overview");
      const c = d.counts, total = d.services.length;
      const bad = d.services.filter((s) => s.state === "down" || s.state === "degraded");
      let head;
      if (c.unknown === total) head = "Waiting for the first checks…";
      else if (!bad.length) head = `All ${total} services are healthy.`;
      else head = `${c.healthy} of ${total} services healthy. ` + bad.map((s) => `${s.label} is ${s.state}`).join(", ") + ".";
      $("#headline").textContent = head;
      const open = d.open_incidents.length;
      $("#subline").textContent = `${open ? `${open} open incident${open > 1 ? "s" : ""}` : "No open incidents"}. Checked ${fmt.ago(d.services[0]?.last_check)}.`;

      $("#board").innerHTML = d.services.map((s) => {
        const r = s.resources || {}, api_ = s.api || {}, last = s.checks.find((x) => x.critical) || s.checks[0] || {};
        return `<a class="svc-row" href="/services/${esc(s.name)}">
          <div><span class="svc-name">${esc(s.label)}</span>
            <span class="state-word ${esc(s.state)}">${esc(s.state)}</span>
            <span class="svc-meta">${s.state_since ? " since " + fmt.time(s.state_since) : ""}${s.remediation?.suspended ? " · auto-restart paused" : ""}</span></div>
          <div class="ribbon" aria-label="${esc(s.label)} recent check history">${ribbon(s.history)}</div>
          <div class="stats">
            <div class="stat"><b>${fmt.ms(last.latency_ms)}</b><span>check</span></div>
            <div class="stat ${level(r.cpu_percent, 80, 95)}"><b>${fmt.pct(r.cpu_percent)}</b><span>CPU</span></div>
            <div class="stat ${level(r.memory_percent, 80, 92)}"><b>${r.memory_percent != null ? fmt.pct(r.memory_percent) : fmt.bytes(r.memory_bytes)}</b><span>memory</span></div>
            <div class="stat ${level(r.disk_percent, 80, 90)}"><b>${fmt.pct(r.disk_percent)}</b><span>disk</span></div>
            <div class="stat ${level(api_.error_ratio, 0.05, 0.25)}"><b>${fmt.ratio(api_.error_ratio)}</b><span>5xx</span></div>
          </div></a>`;
      }).join("");

      $("#open-incidents").innerHTML = d.open_incidents.length ? d.open_incidents.map(incidentItem).join("") : '<li class="muted">None. Everything the platform is tracking has recovered.</li>';
      $("#actions").innerHTML = d.recent_actions.length ? d.recent_actions.map(actionItem).join("") : '<li class="muted">No restarts yet. They appear here when the platform fixes something on its own.</li>';
      const a = d.agent;
      $("#agent").textContent = `Agent: ${a.cycles} cycles every ${a.interval_s}s, last took ${a.last_cycle_ms ?? "–"}ms. Container runtime: ${a.runtime}. Alertmanager: ${a.alertmanager ? "connected" : "off"}.`;
    });
  };

  Ops.service = function (name) {
    $("#restart-btn")?.addEventListener("click", () => restart(name));
    $("#resume-btn").addEventListener("click", async () => {
      try { await api(`/api/services/${name}/remediation/resume`, { method: "POST" }); toast("Auto-restart resumed."); }
      catch (e) { toast(e.message, true); }
    });
    poll(async () => {
      const d = await api(`/api/services/${encodeURIComponent(name)}`);
      const s = d.status, r = s.resources || {};
      const st = $("#svc-state"); st.textContent = s.state; st.className = `state-word ${s.state}`;
      const slo = d.slo;
      $("#svc-sub").textContent = [
        s.state_since ? `In this state since ${fmt.time(s.state_since)}` : "",
        slo && slo.availability_24h != null ? `${(slo.availability_24h * 100).toFixed(3)}% available over 24h (target ${slo.target}%)` : "",
        slo && slo.budget_remaining != null ? `${Math.round(slo.budget_remaining * 100)}% of the 30-day error budget left` : "",
      ].filter(Boolean).join(". ") + ".";
      $("#ribbon").innerHTML = ribbon(s.history);

      document.querySelectorAll(".chart").forEach((el) => {
        const key = el.dataset.metric, unit = el.dataset.unit;
        let vals = s.history.map((h) => h[key]);
        let altBytes = false;
        if (vals.every((v) => v == null) && el.dataset.alt) { vals = s.history.map((h) => h[el.dataset.alt]); altBytes = true; }
        const nowV = vals.filter((v) => v != null).pop();
        const shown = nowV == null ? "–" : altBytes ? fmt.bytes(nowV) : unit === "ms" ? fmt.ms(nowV) : fmt.pct(nowV, 1);
        el.innerHTML = `<div class="chart-now">${shown}</div>${sparkline(vals)}`;
      });

      $("#checks tbody").innerHTML = s.checks.map((c) => `<tr>
        <td>${esc(c.name)}${c.critical ? "" : ' <span class="muted">(non-critical)</span>'}</td><td>${esc(c.type)}</td>
        <td>${c.ok ? (c.slow ? '<span class="state-word degraded">slow</span>' : '<span class="ok">pass</span>') : '<span class="bad">fail</span>'} <span class="muted">${esc(c.detail)}</span></td>
        <td class="num">${fmt.ms(c.latency_ms)}</td></tr>`).join("");
      $("#reasons").innerHTML = s.reasons.length ? s.reasons.map((x) => `<li>${esc(x)}</li>`).join("")
        : `<li class="muted">${s.state === "healthy" ? "Every check passes and resources are within limits." : "Waiting for more check cycles."}</li>`;

      const cont = s.container;
      $("#resources").innerHTML = `
        <dt>CPU</dt><dd>${fmt.pct(r.cpu_percent, 1)}</dd>
        <dt>Memory</dt><dd>${fmt.bytes(r.memory_bytes)}${r.memory_limit_bytes ? ` of ${fmt.bytes(r.memory_limit_bytes)} (${fmt.pct(r.memory_percent)})` : ""}</dd>
        <dt>Disk</dt><dd>${r.disk_quota_bytes ? `${fmt.bytes(r.disk_used_bytes)} of ${fmt.bytes(r.disk_quota_bytes)} quota (${fmt.pct(r.disk_percent, 1)})` : "–"}</dd>
        <dt>API traffic</dt><dd>${s.api.requests} requests last cycle, ${fmt.ratio(s.api.error_ratio)} 5xx, p95 ${fmt.ms(s.api.p95_ms)}</dd>
        <dt>Log errors</dt><dd>${s.logs.errors ?? 0} last cycle (${s.logs.errors_per_min ?? 0}/min)</dd>
        <dt>Container</dt><dd>${cont ? `${esc(cont.state)}${cont.health ? ", " + esc(cont.health) : ""}` : "not tracked"}</dd>`;
      const rem = s.remediation || {};
      $("#remediation").innerHTML = `
        <dt>Status</dt><dd>${!rem.enabled ? "off for this service" : rem.suspended ? '<span class="bad">paused after too many restarts</span>' : '<span class="ok">armed</span>'}</dd>
        <dt>Restarts in window</dt><dd>${rem.attempts_in_window ?? 0} of ${rem.max_restarts ?? "–"} allowed per ${rem.window_minutes ?? "–"} min</dd>
        <dt>Last restart</dt><dd>${rem.last_attempt ? fmt.ago(rem.last_attempt) : "never"}</dd>
        <dt>Depends on</dt><dd>${s.depends_on.length ? s.depends_on.map((x) => `<a href="/services/${esc(x)}">${esc(x)}</a>`).join(", ") : "nothing"}</dd>`;
      $("#resume-btn").hidden = !rem.suspended;

      $("#sigs tbody").innerHTML = d.log_signatures.length ? d.log_signatures.map((g) => `<tr><td><code>${esc(g.template)}</code></td><td class="num">${g.count}</td><td>${fmt.ago(g.last_seen)}</td></tr>`).join("")
        : '<tr><td colspan="3" class="muted">No errors logged today.</td></tr>';
      $("#svc-incidents").innerHTML = d.incidents.length ? d.incidents.map(incidentItem).join("") : '<li class="muted">No incidents.</li>';
      $("#svc-actions").innerHTML = d.actions.length ? d.actions.map(actionItem).join("") : '<li class="muted">No restarts.</li>';
    });
  };

  Ops.incident = function () {
    document.querySelectorAll("[data-inc-action]").forEach((b) => b.addEventListener("click", async () => {
      const id = b.dataset.id, action = b.dataset.incAction;
      if (action === "resolve" && !confirm("Resolve this incident manually? It will reopen if the problem is still detected.")) return;
      try { await api(`/api/incidents/${id}/${action}`, { method: "POST", body: "{}" }); location.reload(); }
      catch (e) { toast(e.message, true); }
    }));
    document.querySelectorAll("[data-restart]").forEach((b) => b.addEventListener("click", () => restart(b.dataset.restart)));
    const form = $("form[data-note]");
    form?.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try {
        await api(`/api/incidents/${form.dataset.note}/notes`, { method: "POST", body: JSON.stringify({ text: form.text.value }) });
        location.reload();
      } catch (e) { toast(e.message, true); }
    });
    document.querySelectorAll("time.ts").forEach((t) => { t.textContent = fmt.time(t.dataset.ts); });
  };

  Ops.reports = function () {
    $("#gen-report")?.addEventListener("click", async () => {
      try {
        const r = await api("/api/reports/generate", { method: "POST" });
        location.href = `/reports/${r.day}`;
      } catch (e) { toast(e.message, true); }
    });
  };

  const FAULTS = [
    { group: "Slow it down", items: [
      { action: "latency", label: "Add 800ms latency for 2 min", body: { ms: 800, seconds: 120 } },
      { action: "errors", label: "Fail 40% of requests for 2 min", body: { rate: 0.4, seconds: 120 } },
    ] },
    { group: "Exhaust resources", items: [
      { action: "cpu", label: "Burn 2 CPU cores for 90s", body: { workers: 2, seconds: 90 } },
      { action: "memory", label: "Leak 320 MB of memory", body: { mb: 320 } },
      { action: "disk", label: "Fill 460 MB of disk", body: { mb: 460 } },
    ] },
    { group: "Take it down", items: [
      { action: "unhealthy", label: "Fail health checks for 60s", body: { seconds: 60 } },
      { action: "hang", label: "Hang the health endpoint", danger: true },
      { action: "crash", label: "Crash the process", danger: true },
    ] },
  ];

  Ops.chaos = function () {
    const root = $("#chaos");
    root.addEventListener("click", async (ev) => {
      const b = ev.target.closest("button[data-action]"); if (!b) return;
      const { service, action } = b.dataset;
      const body = b.dataset.body ? JSON.parse(b.dataset.body) : {};
      if (b.classList.contains("danger") && !confirm(`${b.textContent} on ${service}?`)) return;
      b.disabled = true;
      try { await api(`/api/chaos/${service}/${action}`, { method: "POST", body: JSON.stringify(body) }); toast(`${b.textContent}: started on ${service}. Watch the Overview.`); }
      catch (e) { toast(e.message, true); }
      finally { b.disabled = false; }
    });
    poll(async () => {
      const list = await api("/api/chaos");
      if (!list.length) { root.innerHTML = '<p class="empty">No services have chaos enabled. Set <code>chaos.enabled: true</code> in services.yaml.</p>'; return; }
      root.innerHTML = list.map((s) => {
        const st = s.state || {};
        const active = [];
        if (st.latency_ms) active.push(`+${st.latency_ms}ms latency (${st.latency_remaining_s}s left)`);
        if (st.error_rate) active.push(`${Math.round(st.error_rate * 100)}% errors (${st.errors_remaining_s}s left)`);
        if (st.cpu_burn_remaining_s) active.push(`CPU burn (${st.cpu_burn_remaining_s}s left)`);
        if (st.memory_held_mb) active.push(`${st.memory_held_mb} MB leaked`);
        if (st.disk_fill_mb) active.push(`${st.disk_fill_mb} MB disk filled`);
        if (st.unhealthy) active.push("health checks failing");
        if (st.hanging) active.push("health endpoint hung");
        const groups = s.http ? FAULTS.map((g) => `<div class="chaos-group"><p>${g.group}</p><div class="row-actions" style="margin:0">${g.items.map((f) =>
          `<button type="button" class="${f.danger ? "danger" : ""}" data-service="${esc(s.service)}" data-action="${f.action}" ${f.body ? `data-body='${JSON.stringify(f.body)}'` : ""}>${f.label}</button>`).join("")}</div></div>`).join("") : "";
        return `<section class="chaos-svc"><h2>${esc(s.label)}</h2>
          <p class="muted" style="margin:0">${s.http ? (s.state ? "Reachable." : "Not answering right now.") : "No chaos endpoint; you can kill its container."}</p>
          ${active.length ? `<p class="chaos-active">Running: ${esc(active.join(", "))}</p>` : ""}
          ${groups}
          <div class="chaos-group"><div class="row-actions" style="margin:0">
            ${s.http ? `<button type="button" data-service="${esc(s.service)}" data-action="reset">Stop all experiments</button>` : ""}
            ${s.container ? `<button type="button" class="danger" data-service="${esc(s.service)}" data-action="kill">Kill the container</button>` : ""}
          </div></div></section>`;
      }).join("");
    });
  };

  document.addEventListener("DOMContentLoaded", initKeyDialog);
  if (document.readyState !== "loading") initKeyDialog();
  window.Ops = Ops;
})();
