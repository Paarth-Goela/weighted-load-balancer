/* Gateway Operations Console — Traffic Shift module.
 *
 * Every value shown comes from the management API (no demo data). The console
 * never talks to HAProxy: all changes go through the authenticated REST API.
 * API data is inserted with textContent / DOM nodes only (never parsed as HTML).
 */
"use strict";

// ---------------------------------------------------------------- API table
// Single source of the endpoints the console uses (verified by a contract test).
const API = {
  overview: "/api/v1/traffic-shift/overview",
  trafficConfig: "/api/v1/traffic/config",
  trafficHistory: "/api/v1/traffic/history",
  policies: "/api/v1/traffic/policies",
  emergency: "/api/v1/traffic/emergency",
  timeseries: "/api/v1/metrics/timeseries",
  migrations: "/api/v1/migrations",
  migrationsScheduled: "/api/v1/migrations/scheduled",
  migrationPreview: "/api/v1/migrations/preview",
  migration: (id) => `/api/v1/migrations/${id}`,
  migrationAction: (id, action) => `/api/v1/migrations/${id}/${action}`,
  migrationHistory: (id) => `/api/v1/migrations/${id}/history`,
  migrationStage: (id, seq) => `/api/v1/migrations/${id}/stages/${seq}`,
};
const ACTIONS = ["pause", "resume", "approve", "cancel", "rollback"];
const POOLS = [
  { key: "APIGEE_OPDK", label: "Apigee OPDK", cls: "opdk" },
  { key: "APIGEE_HYBRID", label: "Apigee Hybrid", cls: "hybrid" },
];
const POLL_MS = 3000;
const TOKEN_KEY = "tm_admin_token";

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
/** replaceChildren that skips null/false (the DOM would render them as text). */
function put(el, ...children) { el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false)); }
const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, text) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, v);
  if (text !== undefined) el.textContent = text;
  return el;
}
const fmtPct = (v, d = 0) => (v === null || v === undefined ? "—" : `${Number(v).toFixed(d)}%`);
const fmtMs = (v) => (v === null || v === undefined ? "—" : `${Math.round(v)} ms`);
const fmtNum = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString());
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
function fmtAgo(iso) {
  if (!iso) return "—";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const alloc = (opdk) => (opdk === null || opdk === undefined ? "—" : `${opdk}:${100 - opdk}`);

const STATUS_LEVEL = {
  HEALTHY: "good", DEGRADED: "warning", DOWN: "critical", DISABLED: "neutral", NO_POOL: "neutral",
  RUNNING: "good", WAITING_APPROVAL: "warning", PAUSED: "warning", SCHEDULED: "neutral",
  COMPLETED: "good", FAILED: "critical", ROLLED_BACK: "serious", CANCELLED: "neutral",
  PASSED: "good", APPLIED: "neutral", PENDING: "neutral",
  SUCCESS: "good", REJECTED: "warning", INFO: "neutral",
};
const ICON = { good: "✓", warning: "!", serious: "▲", critical: "✕", neutral: "•" };
const LABEL = { APPLIED: "OBSERVING", WAITING_APPROVAL: "WAITING APPROVAL", ROLLED_BACK: "ROLLED BACK" };
function badge(status) {
  const level = STATUS_LEVEL[status] || "neutral";
  return h("span", { class: `status ${level}` }, h("span", { class: "dot", "aria-hidden": "true" }),
    `${ICON[level]} ${LABEL[status] || status || "—"}`);
}

// --------------------------------------------------------------- API client
class ApiError extends Error {
  constructor(status, body) {
    let msg = body && body.detail !== undefined ? body.detail : `HTTP ${status}`;
    if (Array.isArray(msg)) msg = msg.map((e) => `${(e.loc || []).slice(1).join(".") || "body"}: ${e.msg}`).join("; ");
    super(String(msg));
    this.status = status;
    this.code = body && body.error;
  }
}
async function api(path, { method = "GET", body } = {}) {
  const token = sessionStorage.getItem(TOKEN_KEY);
  const res = await fetch(path, {
    method,
    headers: { Authorization: `Bearer ${token}`, "X-Actor": "operations-console",
               ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 || res.status === 403) {
    signOut(res.status === 403 ? "Token rejected." : "Please sign in.");
    throw new ApiError(res.status, data);
  }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

function banner(msg, bad = false) {
  const el = $("#banner");
  el.textContent = msg;
  el.classList.toggle("bad", bad);
  el.hidden = !msg;
  if (msg && !bad) setTimeout(() => { if (el.textContent === msg) el.hidden = true; }, 8000);
}

async function guarded(button, fn, errorEl) {
  if (errorEl) errorEl.textContent = "";
  if (button) button.disabled = true;     // prevents double submission
  try { return await fn(); }
  catch (e) { if (errorEl) errorEl.textContent = e.message; else banner(e.message, true); }
  finally { if (button) button.disabled = false; }
}

// -------------------------------------------------------------------- auth
function signOut(message) {
  sessionStorage.removeItem(TOKEN_KEY);
  stopPolling();
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-error").textContent = message || "";
}
async function signIn(token) {
  sessionStorage.setItem(TOKEN_KEY, token);
  await api(API.trafficConfig);               // verifies the token
  $("#login").hidden = true;
  $("#app").hidden = false;
  startPolling();
}

// ------------------------------------------------------------------- tabs
let activeTab = "dashboard";
function selectTab(name) {
  activeTab = name;
  for (const b of $$(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const t of $$(".tab")) t.hidden = t.id !== `tab-${name}`;
  if (name === "history") loadHistory();
  if (name === "manual") prefillManual();
  refresh();
}

// ----------------------------------------------------------------- polling
let overview = null;
let pollTimer = null;
let tickCount = 0;
function startPolling() { stopPolling(); refresh(true); pollTimer = setInterval(() => refresh(), POLL_MS); }
function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

async function refresh(full = false) {
  if (document.hidden || !sessionStorage.getItem(TOKEN_KEY)) return;
  tickCount += 1;
  try {
    overview = await api(`${API.overview}?window_seconds=60`);
    $("#conn").textContent = `Live · updated ${new Date().toLocaleTimeString()}`;
    renderDashboard(overview);
    renderSchedule(overview.migration);
    renderEmergencyAvailability(overview);
    if (full || tickCount % 3 === 0) await renderCharts();
  } catch (e) {
    $("#conn").textContent = `Disconnected: ${e.message}`;
  }
}

// =============================================================== DASHBOARD
function tile(label, value, sub, extra) {
  return h("div", { class: "tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    sub ? h("div", { class: "sub" }, sub) : null, extra || null);
}

function renderDashboard(o) {
  const cfg = o.traffic;
  const rt = cfg.runtime;
  const m = o.migration;
  const tiles = $("#tiles");
  const policyNote = cfg.policy ? `policy v${cfg.policy.id}${cfg.in_sync === false ? " · RUNTIME DIFFERS" : ""}` : "no policy";
  const last = o.last_successful_shift;
  const lastNew = last && last.new_state ? last.new_state : null;
  const progress = m ? m.progress_percent : null;
  tiles.replaceChildren(
    tile(h("span", {}, h("span", { class: "key opdk" }), " Apigee OPDK (live)"),
      rt ? fmtPct(rt.opdk_weight) : "—", rt ? policyNote : (cfg.haproxy_error || "unavailable")),
    tile(h("span", {}, h("span", { class: "key hybrid" }), " Apigee Hybrid (live)"),
      rt ? fmtPct(rt.hybrid_weight) : "—", rt ? policyNote : ""),
    tile("Target allocation", o.target_allocation ? `${o.target_allocation.opdk_weight}:${o.target_allocation.hybrid_weight}` : "—",
      o.target_allocation ? `OPDK:Hybrid · ${o.target_allocation.source}` : ""),
    tile("Migration stage", m ? `${m.current_stage} / ${m.total_stages}` : "—",
      m ? `${m.mode.toLowerCase()} · ${m.name}` : "No active migration"),
    tile("Migration progress", progress === null ? "—" : fmtPct(progress, 0),
      m ? `${alloc(m.initial_opdk)} → ${alloc(m.target_opdk)}` : "",
      m ? h("div", { class: "progress", role: "progressbar", "aria-valuenow": String(progress),
        "aria-valuemin": "0", "aria-valuemax": "100" }, barSpan(progress)) : null),
    tile("Migration status", m ? badge(m.status) : badge("NONE"), m && m.status_reason ? m.status_reason : ""),
    tile("Last successful shift", last ? fmtAgo(last.timestamp) : "—",
      last ? `${lastNew ? alloc(lastNew.opdk_weight) : ""} · ${last.event_type.replace("TRAFFIC_", "").toLowerCase()} · ${last.source}` : ""),
  );

  // gateway health
  const gh = h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "Gateway pool"), h("th", {}, "Status"), h("th", { class: "num" }, "Routable"),
      h("th", { class: "num" }, "Healthy"), h("th", { class: "num" }, "Unhealthy"),
      h("th", { class: "num" }, "Configured"), h("th", { class: "num" }, "Effective"))),
    h("tbody", {}, o.gateways.map((g) => {
      const p = (cfg.pools || []).find((x) => x.platform_type === g.platform_type) || {};
      const hl = g.health || {};
      return h("tr", {},
        h("td", {}, h("span", { class: `key ${g.platform_type === "APIGEE_OPDK" ? "opdk" : "hybrid"}` }), " ", g.pool || g.platform_type),
        h("td", {}, badge(hl.status || "NO_POOL")),
        h("td", { class: "num" }, `${hl.routable_instances ?? "—"} / ${hl.enabled_instances ?? "—"}`),
        h("td", { class: "num" }, fmtNum(hl.healthy_instances)),
        h("td", { class: "num" }, fmtNum(hl.unhealthy_instances)),
        h("td", { class: "num" }, fmtPct(p.configured_percent)),
        h("td", { class: "num" }, fmtPct(p.effective_percent, 1)));
    })));
  $("#gateway-health").replaceChildren(gh,
    h("p", { class: "hint" }, "Healthy = readiness probe passing. This does not prove API compatibility; migrations also gate on observed error rate and latency."));

  // pool metrics
  const mt = o.metrics;
  $("#metrics-window").textContent = `· last ${mt.window_seconds}s`;
  const col = mt.collector;
  const collectorNote = !col.listening
    ? h("p", { class: "error" }, `Metrics stream unavailable${col.error ? `: ${col.error}` : ""}`)
    : h("p", { class: "hint" }, `HAProxy per-request stream · ${fmtNum(col.received_total)} events received` +
        (col.seconds_since_last_event !== null ? ` · last ${col.seconds_since_last_event}s ago` : ""));
  $("#pool-metrics").replaceChildren(h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "Pool"), h("th", { class: "num" }, "Requests"), h("th", { class: "num" }, "Req/s"),
      h("th", { class: "num" }, "Error rate"), h("th", { class: "num" }, "p95"), h("th", { class: "num" }, "Avg"))),
    h("tbody", {}, POOLS.map((p) => {
      const w = mt.pools[p.key] || {};
      return h("tr", {}, h("td", {}, h("span", { class: `key ${p.cls}` }), " ", p.label),
        h("td", { class: "num" }, fmtNum(w.requests)), h("td", { class: "num" }, w.rps ?? "—"),
        h("td", { class: "num" }, fmtPct(w.error_rate_percent, 2)), h("td", { class: "num" }, fmtMs(w.p95_ms)),
        h("td", { class: "num" }, fmtMs(w.avg_ms)));
    }))), collectorNote);

  renderActiveMigration(m);
}

function barSpan(pct) {
  const span = h("span");
  span.style.width = `${Math.max(0, Math.min(100, pct || 0))}%`;
  return span;
}

function controlButtons(m) {
  const buttons = {
    RUNNING: ["pause", "cancel", "rollback"],
    WAITING_APPROVAL: ["approve", "pause", "cancel", "rollback"],
    PAUSED: ["resume", "cancel", "rollback"],
    SCHEDULED: ["pause", "cancel"],
    COMPLETED: ["rollback"],
  }[m.status] || [];
  const label = { pause: "Pause", resume: "Resume", approve: "Approve next stage", cancel: "Cancel", rollback: "Roll back" };
  return buttons.map((a) => h("button", {
    type: "button", class: a === "rollback" ? "danger small" : a === "approve" || a === "resume" ? "primary small" : "small",
    onclick: (ev) => migrationAction(m, a, ev.currentTarget),
  }, label[a]));
}

async function migrationAction(m, action, button) {
  let body;
  if (action === "rollback") {
    const to = m.status === "COMPLETED" ? "initial" : "last_good";
    const target = to === "initial" ? m.initial_opdk : m.last_good_opdk;
    if (!window.confirm(`Roll back migration ${m.id} to ${to.replace("_", " ")} allocation ${alloc(target)} (OPDK:Hybrid)?`)) return;
    body = { to, reason: "operator rollback from console" };
  } else if (action === "cancel") {
    if (!window.confirm(`Cancel migration ${m.id}? Traffic stays at ${alloc(m.current_opdk)}.`)) return;
    body = { reason: "cancelled from console" };
  } else {
    body = { reason: `${action} from console` };
  }
  await guarded(button, async () => {
    const out = await api(API.migrationAction(m.id, action), { method: "POST", body });
    banner(`Migration ${out.id}: ${out.status}${out.status_reason ? ` — ${out.status_reason}` : ""}`);
    await refresh(true);
  });
}

function stageTable(m, withCancel = false) {
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, m.mode === "SCHEDULED" ? "Scheduled" : "Applied"),
      h("th", {}, "Planned OPDK:Hybrid"), h("th", {}, "Actual"), h("th", {}, "Status"),
      h("th", { class: "num" }, "Requests"), h("th", { class: "num" }, "Error rate"), h("th", { class: "num" }, "p95"),
      h("th", {}, "Reason"), withCancel ? h("th", {}, "") : null)),
    h("tbody", {}, m.stages.map((st) => {
      const met = st.metrics && st.metrics.evaluated_pool ? st.metrics[st.metrics.evaluated_pool] : null;
      return h("tr", {},
        h("td", {}, String(st.seq)),
        h("td", {}, fmtTime(m.mode === "SCHEDULED" && !st.applied_at ? st.scheduled_at : st.applied_at)),
        h("td", {}, alloc(st.planned_opdk)), h("td", {}, alloc(st.actual_opdk)), h("td", {}, badge(st.status)),
        h("td", { class: "num" }, met ? fmtNum(met.requests) : "—"),
        h("td", { class: "num" }, met ? fmtPct(met.error_rate_percent, 2) : "—"),
        h("td", { class: "num" }, met ? fmtMs(met.p95_ms) : "—"),
        h("td", {}, st.reason || ""),
        withCancel ? h("td", {}, st.status === "PENDING" ? h("button", {
          type: "button", class: "small", onclick: (ev) => cancelStage(m, st.seq, ev.currentTarget) }, "Cancel stage") : "") : null);
    }))));
}

function renderActiveMigration(m) {
  const box = $("#active-migration");
  $("#active-controls").replaceChildren(...(m ? controlButtons(m) : []));
  if (!m) {
    box.replaceChildren(h("p", { class: "muted" }, "No active migration. Start one from Gradual shift or Scheduled shift."));
    return;
  }
  put(box,
    h("p", {}, badge(m.status), " ", `${m.name} · ${m.source.replace("APIGEE_", "")} → ${m.destination.replace("APIGEE_", "")} · `,
      `now ${alloc(m.current_opdk)}, last good ${alloc(m.last_good_opdk)}, target ${alloc(m.target_opdk)}`,
      m.next_action_at ? ` · next action ${fmtTime(m.next_action_at)}` : ""),
    m.status_reason ? h("p", { class: STATUS_LEVEL[m.status] === "warning" || STATUS_LEVEL[m.status] === "critical" ? "error" : "hint" }, m.status_reason) : null,
    h("p", { class: "hint" }, `Thresholds: ≥${m.min_requests} requests/window, error ≤${m.max_error_rate_percent}%, p95 ≤${m.max_p95_ms} ms, ` +
      `≥${m.min_healthy_instances} healthy destination instance(s), auto-rollback ${m.auto_rollback ? "on" : "off"}, ` +
      `approval ${m.require_approval ? "required" : "not required"}, window ${m.interval_seconds}s`),
    stageTable(m),
  );
}

// ------------------------------------------------------------------ charts
async function renderCharts() {
  try {
    const [policies, ts] = await Promise.all([api(API.policies), api(`${API.timeseries}?seconds=900&bucket_seconds=15`)]);
    const recent = policies.slice(-40);
    const now = Date.now();
    const pts = (key) => {
      const p = recent.map((v) => ({ x: new Date(v.updated_at).getTime(), y: v[key] }));
      if (p.length) p.push({ x: now, y: p[p.length - 1].y });
      return p;
    };
    lineChart($("#chart-allocation"), {
      series: [{ label: "Apigee OPDK", cls: "opdk", points: pts("opdk_weight"), step: true },
               { label: "Apigee Hybrid", cls: "hybrid", points: pts("hybrid_weight"), step: true }],
      yMax: 100, yFormat: (v) => `${v}%`, xFormat: (x) => new Date(x).toLocaleString(), xTick: shortTime,
      empty: "No traffic policy versions yet.",
    });
    const series = ts.series.map((b) => ({ x: b.t * 1000, b }));
    const metricSeries = (field) => POOLS.map((p) => ({
      label: p.label, cls: p.cls, points: series.map(({ x, b }) => ({ x, y: b[p.key][field] })),
    }));
    const errSeries = metricSeries("error_rate_percent");
    const latSeries = metricSeries("p95_ms");
    const maxOf = (ss) => Math.max(0, ...ss.flatMap((s2) => s2.points.map((p) => p.y ?? 0)));
    lineChart($("#chart-errors"), { series: errSeries, yMax: niceMax(Math.max(maxOf(errSeries), 1)),
      yFormat: (v) => `${round(v, 2)}%`, xFormat: (x) => new Date(x).toLocaleTimeString(), xTick: shortTime,
      empty: "No traffic in the last 15 minutes." });
    lineChart($("#chart-latency"), { series: latSeries, yMax: niceMax(Math.max(maxOf(latSeries), 10)),
      yFormat: (v) => `${v < 10 && v % 1 ? v.toFixed(1) : Math.round(v)} ms`, xFormat: (x) => new Date(x).toLocaleTimeString(), xTick: shortTime,
      empty: "No traffic in the last 15 minutes." });
  } catch (e) {
    $("#chart-allocation").replaceChildren(h("p", { class: "error" }, e.message));
  }
}
// seconds are shown when the visible span is short, so adjacent ticks never repeat
const shortTime = (x, span = Infinity) => new Date(x).toLocaleTimeString([], span < 15 * 60000
  ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
const round = (v, d) => Math.round(v * 10 ** d) / 10 ** d;
function niceMax(v) {
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

/* Line/step chart: one y-axis, thin 2px lines, legend for >=2 series plus
   direct end labels (<=4 series), crosshair + tooltip listing every series,
   keyboard navigation, and a table view. */
function lineChart(el, opt) {
  el.classList.toggle("refreshing", false);
  const all = opt.series.flatMap((sr) => sr.points.filter((p) => p.y !== null && p.y !== undefined));
  el._table = tableData(opt);
  if (el.dataset.view === "table") { renderTable(el); return; }
  if (!all.length) { el.replaceChildren(h("p", { class: "empty" }, opt.empty || "No data.")); return; }
  const W = Math.max(el.clientWidth || 800, 320), H = 240;
  const m = { l: 48, r: opt.series.length <= 4 ? 96 : 16, t: 12, b: 28 };
  const xs = [...new Set(opt.series.flatMap((sr) => sr.points.map((p) => p.x)))].sort((a, b) => a - b);
  const x0 = opt.xMin ?? xs[0], x1 = opt.xMax ?? xs[xs.length - 1];
  const yMax = opt.yMax || 1;
  const X = (x) => m.l + (x1 === x0 ? (W - m.l - m.r) / 2 : ((x - x0) / (x1 - x0)) * (W - m.l - m.r));
  const Y = (y) => m.t + (1 - y / yMax) * (H - m.t - m.b);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0",
    "aria-label": `${opt.series.map((sr) => sr.label).join(" and ")} chart; use arrow keys to inspect values` });
  const axis = s("g", { class: "axis" });
  for (let i = 0; i <= 4; i++) {
    const v = (yMax * i) / 4;
    axis.append(s("line", { class: i === 0 ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    axis.append(s("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, opt.yFormat(v)));
  }
  const ticks = Math.min(5, xs.length);
  for (let i = 0; i < ticks; i++) {
    const x = ticks === 1 ? x0 : x0 + ((x1 - x0) * i) / (ticks - 1);
    axis.append(s("text", { x: X(x), y: H - 8, "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle" },
      (opt.xTick || opt.xFormat)(x, x1 - x0)));
  }
  svg.append(axis);
  const labels = [];
  for (const sr of opt.series) {
    let d = "", prev = null;
    for (const p of sr.points) {
      if (p.y === null || p.y === undefined) { prev = null; continue; }
      if (!prev) d += `M${X(p.x)},${Y(p.y)}`;
      else if (sr.step) d += `H${X(p.x)}V${Y(p.y)}`;
      else d += `L${X(p.x)},${Y(p.y)}`;
      prev = p;
    }
    svg.append(s("path", { d, class: `series ${sr.cls}${sr.dashed ? " planned" : ""}` }));
    for (const p of sr.points.filter((q) => q.marker)) {
      svg.append(s("circle", { cx: X(p.x), cy: Y(p.y), r: 4, class: `marker ${p.marker}` }));
    }
    const lastPt = [...sr.points].reverse().find((p) => p.y !== null && p.y !== undefined);
    if (lastPt && opt.series.length <= 4 && !sr.noLabel) {
      labels.push({ x: X(lastPt.x) + 6, y: Y(lastPt.y) + 4, text: `${sr.label} ${opt.yFormat(lastPt.y)}` });
    }
  }
  // direct labels: keep >= 13px apart so equal end values never overprint
  labels.sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 13);
  for (const lb of labels) svg.append(s("text", { class: "direct-label", x: lb.x, y: lb.y }, lb.text));
  const cross = s("line", { class: "crosshair", y1: m.t, y2: H - m.b, visibility: "hidden" });
  svg.append(cross);
  const hit = s("rect", { x: m.l, y: m.t, width: Math.max(W - m.l - m.r, 1), height: H - m.t - m.b, fill: "transparent" });
  svg.append(hit);
  let idx = -1;
  const valueAt = (sr, x) => {
    let v = null;
    for (const p of sr.points) {
      if (sr.step ? p.x <= x : p.x === x) v = p.y;
    }
    return v;
  };
  const show = (i, clientX, clientY) => {
    idx = Math.max(0, Math.min(xs.length - 1, i));
    const x = xs[idx];
    cross.setAttribute("x1", X(x)); cross.setAttribute("x2", X(x)); cross.setAttribute("visibility", "visible");
    const tip = $("#tooltip");
    tip.replaceChildren(h("div", { class: "t" }, opt.xFormat(x)),
      ...opt.series.map((sr) => h("div", { class: "r" }, h("span", { class: `line ${sr.dashed ? "planned" : sr.cls}` }),
        h("strong", {}, valueAt(sr, x) === null ? "—" : opt.yFormat(valueAt(sr, x))), h("span", { class: "muted" }, sr.label))),
      ...(opt.extra ? opt.extra(x) : []));
    tip.hidden = false;
    const rect = svg.getBoundingClientRect();
    const px = clientX ?? rect.left + (X(x) / W) * rect.width;
    const py = clientY ?? rect.top + 20;
    tip.style.left = `${Math.min(px + 14, window.innerWidth - tip.offsetWidth - 8)}px`;
    tip.style.top = `${py + 14}px`;
  };
  const hide = () => { cross.setAttribute("visibility", "hidden"); $("#tooltip").hidden = true; };
  const nearest = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const vx = ((clientX - rect.left) / rect.width) * W;
    let best = 0, bd = Infinity;
    xs.forEach((x, i) => { const dd = Math.abs(X(x) - vx); if (dd < bd) { bd = dd; best = i; } });
    return best;
  };
  hit.addEventListener("pointermove", (e) => show(nearest(e.clientX), e.clientX, e.clientY));
  hit.addEventListener("pointerleave", hide);
  svg.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault(); show(idx < 0 ? xs.length - 1 : idx + (e.key === "ArrowRight" ? 1 : -1));
    } else if (e.key === "Escape") hide();
  });
  svg.addEventListener("blur", hide);
  const legend = opt.series.length >= 2 ? h("div", { class: "legend" }, opt.series.map((sr) =>
    h("span", { class: "item" }, h("span", { class: `line ${sr.dashed ? "dashed" : sr.cls}` }), sr.label))) : null;
  el.replaceChildren(...[legend, svg].filter(Boolean));
}

function tableData(opt) {
  const xs = [...new Set(opt.series.flatMap((sr) => sr.points.map((p) => p.x)))].sort((a, b) => a - b);
  return {
    columns: ["", ...opt.series.map((sr) => sr.label)],
    rows: xs.map((x) => [opt.xFormat(x), ...opt.series.map((sr) => {
      const p = sr.points.find((q) => q.x === x);
      return p && p.y !== null && p.y !== undefined ? opt.yFormat(p.y) : "—";
    })]),
  };
}
function renderTable(el) {
  const t = el._table || { columns: [], rows: [] };
  el.replaceChildren(h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, t.columns.map((c, i) => h("th", { class: i ? "num" : "" }, c)))),
    h("tbody", {}, t.rows.map((r) => h("tr", {}, r.map((c, i) => h("td", { class: i ? "num" : "" }, c))))))));
}
function toggleTable(button) {
  const el = document.getElementById(button.dataset.tableToggle);
  el.dataset.view = el.dataset.view === "table" ? "chart" : "table";
  button.textContent = el.dataset.view === "table" ? "Chart view" : "Table view";
  if (el.dataset.view === "table") renderTable(el);
  else if (el.id === "chart-plan" && el._render) el._render();
  else renderCharts();
}

// ================================================================== MANUAL
function linkPair(form, aName, bName, onChange) {
  const a = form.elements[aName], b = form.elements[bName];
  a.addEventListener("input", () => { if (a.value !== "") b.value = String(100 - Number(a.value)); onChange(); });
  b.addEventListener("input", () => { if (b.value !== "") a.value = String(100 - Number(b.value)); onChange(); });
}
function allocationBar(el, opdk) {
  const a = h("span", { class: "opdk" }), b = h("span", { class: "hybrid" });
  a.style.flexGrow = String(Math.max(opdk, 0)); b.style.flexGrow = String(Math.max(100 - opdk, 0));
  el.replaceChildren(...[opdk > 0 ? a : null, opdk < 100 ? b : null].filter(Boolean));
}
function manualChanged() {
  const f = $("#manual-form");
  const o = Number(f.elements.opdk.value), y = Number(f.elements.hybrid.value);
  const valid = Number.isInteger(o) && Number.isInteger(y) && o >= 0 && y >= 0 && o + y === 100;
  $("#manual-total").textContent = valid ? `Total 100% · OPDK ${o}% / Hybrid ${y}%` : `Total ${o + y}% — must equal 100%`;
  $("#manual-total").className = valid ? "hint" : "error";
  if (valid) allocationBar($("#manual-bar"), o);
  return valid;
}
function prefillManual() {
  const rt = overview && overview.traffic.runtime;
  const f = $("#manual-form");
  if (rt && f.elements.opdk.value === "") {
    f.elements.opdk.value = rt.opdk_weight; f.elements.hybrid.value = rt.hybrid_weight; manualChanged();
  }
}
async function submitManual(ev) {
  ev.preventDefault();
  const f = ev.currentTarget;
  if (!manualChanged()) { $("#manual-error").textContent = "OPDK % + Hybrid % must equal 100."; return; }
  await guarded(f.querySelector("button[type=submit]"), async () => {
    const out = await api(API.trafficConfig, { method: "PUT", body: {
      opdk_weight: Number(f.elements.opdk.value), hybrid_weight: Number(f.elements.hybrid.value),
      reason: f.elements.reason.value || undefined } });
    banner(out.message);
    await refresh(true);
  }, $("#manual-error"));
}

// ================================================================= GRADUAL
function safetyFields(container) {
  container.replaceChildren(h("legend", {}, "Safety gates (checked before and after every stage)"),
    h("div", { class: "row" },
      h("label", {}, "Min requests / window", h("input", { name: "min_requests", type: "number", min: "0", value: "100", required: true })),
      h("label", {}, "Max error rate %", h("input", { name: "max_error", type: "number", min: "0", max: "100", step: "0.1", value: "1", required: true })),
      h("label", {}, "Max p95 latency (ms)", h("input", { name: "max_p95", type: "number", min: "1", value: "1000", required: true })),
      h("label", {}, "Min healthy destination instances", h("input", { name: "min_healthy", type: "number", min: "1", value: "1", required: true }))),
    h("label", { class: "check" }, h("input", { name: "auto_rollback", type: "checkbox", checked: true }),
      "Automatically roll back to the last known-good allocation on a violation"),
    h("label", { class: "check" }, h("input", { name: "require_approval", type: "checkbox" }),
      "Require manual approval between stages"));
}
function poolSelects(form) {
  for (const [name, def] of [["source", "APIGEE_OPDK"], ["destination", "APIGEE_HYBRID"]]) {
    form.elements[name].replaceChildren(...POOLS.map((p) => h("option", { value: p.key, selected: p.key === def }, p.label)));
  }
}
function safetyBody(f) {
  return {
    min_requests: Number(f.elements.min_requests.value),
    max_error_rate_percent: Number(f.elements.max_error.value),
    max_p95_ms: Number(f.elements.max_p95.value),
    min_healthy_instances: Number(f.elements.min_healthy.value),
    auto_rollback: f.elements.auto_rollback.checked,
    require_approval: f.elements.require_approval.checked,
  };
}
function gradualBody(f) {
  const body = {
    source: f.elements.source.value, destination: f.elements.destination.value,
    target_destination_percent: Number(f.elements.target.value),
    step_percent: Number(f.elements.step.value), interval_seconds: Number(f.elements.interval.value),
    ...safetyBody(f),
  };
  if (f.elements.start.value !== "") body.start_destination_percent = Number(f.elements.start.value);
  if (f.elements.reason.value) body.reason = f.elements.reason.value;
  return body;
}
async function previewGradual(ev) {
  const f = $("#gradual-form");
  await guarded(ev.currentTarget, async () => {
    const plan = await api(API.migrationPreview, { method: "POST", body: gradualBody(f) });
    const steps = plan.stages.map((st) => `${st.opdk}:${st.hybrid}`).join(" → ");
    $("#gradual-plan").replaceChildren(h("h3", {}, `Planned progression (OPDK:Hybrid) · ${plan.stages.length - 1} stage(s)`),
      h("p", {}, steps));
  }, $("#gradual-error"));
}
async function startGradual(ev) {
  ev.preventDefault();
  const f = ev.currentTarget;
  await guarded(f.querySelector("button[type=submit]"), async () => {
    const m = await api(API.migrations, { method: "POST", body: gradualBody(f) });
    banner(`Migration ${m.id} started: ${m.status}${m.status_reason ? ` — ${m.status_reason}` : ""}`);
    selectTab("dashboard");
  }, $("#gradual-error"));
}

// =============================================================== SCHEDULED
function localInputValue(date) {
  const d = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return d.toISOString().slice(0, 16);
}
function addScheduleRow(at, opdk) {
  const tbody = $("#schedule-rows tbody");
  const o = h("input", { type: "number", min: "0", max: "100", value: String(opdk), required: true, name: "s_opdk" });
  const y = h("input", { type: "number", min: "0", max: "100", value: String(100 - opdk), required: true, name: "s_hybrid" });
  o.addEventListener("input", () => { y.value = String(100 - Number(o.value)); });
  y.addEventListener("input", () => { o.value = String(100 - Number(y.value)); });
  const row = h("tr", {},
    h("td", {}, h("input", { type: "datetime-local", value: localInputValue(at), required: true, name: "s_at" })),
    h("td", {}, o), h("td", {}, y),
    h("td", {}, h("button", { type: "button", class: "small ghost", onclick: () => row.remove() }, "Remove")));
  tbody.append(row);
}
async function submitScheduled(ev) {
  ev.preventDefault();
  const f = ev.currentTarget;
  const stages = $$("#schedule-rows tbody tr").map((r) => ({
    at: new Date($("input[name=s_at]", r).value).toISOString(),
    opdk_weight: Number($("input[name=s_opdk]", r).value),
    hybrid_weight: Number($("input[name=s_hybrid]", r).value),
  }));
  const body = { source: f.elements.source.value, destination: f.elements.destination.value,
    final_observation_seconds: Number(f.elements.final.value), stages, ...safetyBody(f) };
  if (f.elements.reason.value) body.reason = f.elements.reason.value;
  await guarded(f.querySelector("button[type=submit]"), async () => {
    const m = await api(API.migrationsScheduled, { method: "POST", body });
    banner(`Scheduled migration ${m.id} created with ${m.total_stages} stage(s).`);
    await refresh(true);
  }, $("#scheduled-error"));
}
function renderSchedule(m) {
  const box = $("#schedule-view");
  if (!m || m.mode !== "SCHEDULED") {
    box.replaceChildren(h("p", { class: "muted" }, "No active scheduled migration."));
    return;
  }
  box.replaceChildren(h("p", {}, badge(m.status), " ", m.name, m.status_reason ? ` — ${m.status_reason}` : ""),
    h("div", { class: "controls" }, controlButtons(m)), stageTable(m, true));
}
async function cancelStage(m, seq, button) {
  if (!window.confirm(`Cancel stage ${seq} of migration ${m.id}?`)) return;
  await guarded(button, async () => {
    await api(API.migrationStage(m.id, seq), { method: "DELETE" });
    banner(`Stage ${seq} cancelled.`);
    await refresh(true);
  });
}

// =============================================================== EMERGENCY
function renderEmergencyAvailability(o) {
  const f = $("#emergency-form");
  const opdk = Number(f.elements.opdk.value);
  const share = { APIGEE_OPDK: opdk, APIGEE_HYBRID: 100 - opdk };
  const rows = o.gateways.map((g) => {
    const routable = g.health ? g.health.routable_instances : 0;
    const needs = share[g.platform_type] > 0;
    const ok = !needs || routable > 0;
    return h("tr", {}, h("td", {}, g.pool || g.platform_type), h("td", { class: "num" }, `${share[g.platform_type]}%`),
      h("td", { class: "num" }, String(routable)),
      h("td", {}, needs ? badge(ok ? "HEALTHY" : "DOWN") : h("span", { class: "muted" }, "no traffic")));
  });
  $("#emergency-availability").replaceChildren(h("h3", {}, "Destination availability (live)"),
    h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "Pool"), h("th", { class: "num" }, "Would receive"),
      h("th", { class: "num" }, "Routable instances"), h("th", {}, "Available"))), h("tbody", {}, rows)));
}
function emergencyBody() {
  const f = $("#emergency-form");
  return { opdk_weight: Number(f.elements.opdk.value), hybrid_weight: Number(f.elements.hybrid.value),
    reason: f.elements.reason.value };
}
function reviewEmergency(ev) {
  ev.preventDefault();
  const b = emergencyBody();
  $("#emergency-error").textContent = "";
  if (b.opdk_weight + b.hybrid_weight !== 100) { $("#emergency-error").textContent = "Percentages must total 100."; return; }
  if (b.reason.trim().length < 3) { $("#emergency-error").textContent = "A reason is required."; return; }
  const active = overview && overview.migration;
  $("#emergency-summary").textContent = `Shift traffic NOW to OPDK ${b.opdk_weight}% / Hybrid ${b.hybrid_weight}%.` +
    (active ? ` Active migration ${active.id} (${active.status}) will be cancelled.` : "") + ` Reason: ${b.reason}`;
  $("#emergency-confirm-form").elements.confirm.value = "";
  $("#emergency-dialog").showModal();
}
async function confirmEmergency(ev) {
  ev.preventDefault();
  const confirmText = ev.currentTarget.elements.confirm.value;
  await guarded($("#emergency-go"), async () => {
    const out = await api(API.emergency, { method: "POST", body: { ...emergencyBody(), confirm: confirmText } });
    $("#emergency-dialog").close();
    banner(out.message);
    selectTab("dashboard");
  }, $("#emergency-error"));
  if ($("#emergency-error").textContent) $("#emergency-dialog").close();
}

// ================================================================= HISTORY
async function loadHistory() {
  try {
    const [list, th] = await Promise.all([api(`${API.migrations}?limit=50`), api(`${API.trafficHistory}?limit=50`)]);
    $("#migration-list").replaceChildren(list.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Name"), h("th", {}, "Mode"), h("th", {}, "Status"),
        h("th", {}, "Initial → target"), h("th", { class: "num" }, "Stages"), h("th", {}, "Created"), h("th", {}, "Finished"))),
      h("tbody", {}, list.map((m) => h("tr", { class: "clickable", tabindex: "0",
        onclick: () => showMigration(m.id), onkeydown: (e) => { if (e.key === "Enter") showMigration(m.id); } },
        h("td", {}, String(m.id)), h("td", {}, m.name), h("td", {}, m.mode.toLowerCase()), h("td", {}, badge(m.status)),
        h("td", {}, `${alloc(m.initial_opdk)} → ${alloc(m.target_opdk)}`), h("td", { class: "num" }, String(m.total_stages)),
        h("td", {}, fmtTime(m.created_at)), h("td", {}, fmtTime(m.finished_at)))))))
      : h("p", { class: "muted" }, "No migrations yet."));
    $("#traffic-history").replaceChildren(auditTable(th.entries.slice().reverse()));
  } catch (e) { banner(e.message, true); }
}
function auditTable(entries) {
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "Time"), h("th", {}, "Event"), h("th", {}, "Status"), h("th", {}, "Source"),
      h("th", {}, "Change"), h("th", {}, "Reason / error"))),
    h("tbody", {}, entries.map((e) => {
      const p = e.previous_state || {}, n = e.new_state || {};
      const change = n.opdk_weight !== undefined ? `${p.opdk_weight !== undefined ? alloc(p.opdk_weight) : "?"} → ${alloc(n.opdk_weight)}`
        : n.opdk !== undefined ? `→ ${alloc(n.opdk)}` : n.status ? `→ ${n.status}` : "";
      return h("tr", {}, h("td", {}, fmtTime(e.timestamp)), h("td", {}, e.event_type), h("td", {}, badge(e.status)),
        h("td", {}, e.source), h("td", {}, change), h("td", {}, e.error || e.reason || ""));
    }))));
}
async function showMigration(id) {
  try {
    const [m, hist] = await Promise.all([api(API.migration(id)), api(API.migrationHistory(id))]);
    $("#migration-detail-card").hidden = false;
    $("#migration-detail-title").textContent = `Migration ${m.id} · ${m.name} · ${m.status}`;
    const chartEl = $("#chart-plan");
    const stages = m.stages.filter((st) => st.status !== "CANCELLED");
    const pts = [{ x: 0, planned: 100 - m.initial_opdk, actual: 100 - m.initial_opdk, st: null }]
      .concat(stages.map((st) => ({ x: st.seq, planned: st.planned_hybrid,
        actual: st.actual_hybrid, st })));
    const byX = new Map(pts.map((p) => [p.x, p]));
    chartEl._render = () => lineChart(chartEl, {
      series: [
        { label: "Planned Hybrid %", cls: "hybrid", dashed: true, points: pts.map((p) => ({ x: p.x, y: p.planned })) },
        { label: "Actual Hybrid %", cls: "hybrid", points: pts.map((p) => ({ x: p.x, y: p.actual,
          marker: p.st ? (p.st.status === "FAILED" ? "failed" : p.actual !== null ? "hybrid" : null) : "hybrid" })) },
      ],
      yMax: 100, yFormat: (v) => `${v}%`, xFormat: (x) => (x === 0 ? "Start" : `Stage ${x}`), xTick: (x) => (x === 0 ? "Start" : `S${Math.round(x)}`),
      extra: (x) => {
        const p = byX.get(x);
        if (!p || !p.st) return [];
        const met = p.st.metrics && p.st.metrics.evaluated_pool ? p.st.metrics[p.st.metrics.evaluated_pool] : null;
        return [h("div", { class: "t" }, `${p.st.status} · ${fmtTime(p.st.applied_at || p.st.scheduled_at)}`),
          met ? h("div", { class: "t" }, `error ${fmtPct(met.error_rate_percent, 2)} · p95 ${fmtMs(met.p95_ms)} · ${fmtNum(met.requests)} req`) : null]
          .filter(Boolean);
      },
    });
    chartEl._render();
    $("#migration-stages").replaceChildren(stageTable(m));
    $("#migration-audit").replaceChildren(auditTable(hist.entries));
  } catch (e) { banner(e.message, true); }
}

// ==================================================================== init
function init() {
  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const token = $("#token").value.trim();
    try { await signIn(token); $("#token").value = ""; }
    catch (e) { if (!$("#login-error").textContent) $("#login-error").textContent = e.message; }
  });
  $("#logout").addEventListener("click", () => signOut("Signed out."));
  for (const b of $$(".tabs button")) b.addEventListener("click", () => selectTab(b.dataset.tab));
  for (const b of $$("[data-table-toggle]")) b.addEventListener("click", () => toggleTable(b));

  const manual = $("#manual-form");
  linkPair(manual, "opdk", "hybrid", manualChanged);
  manual.addEventListener("submit", submitManual);

  const gradual = $("#gradual-form");
  poolSelects(gradual); safetyFields($("#gradual-safety"));
  gradual.addEventListener("submit", startGradual);
  $("#gradual-preview").addEventListener("click", previewGradual);

  const sched = $("#scheduled-form");
  poolSelects(sched); safetyFields($("#scheduled-safety"));
  const base = Date.now();
  [[5, 90], [10, 75], [15, 50]].forEach(([min, o]) => addScheduleRow(new Date(base + min * 60000), o));
  $("#add-stage").addEventListener("click", () => addScheduleRow(new Date(Date.now() + 20 * 60000), 50));
  sched.addEventListener("submit", submitScheduled);

  const em = $("#emergency-form");
  linkPair(em, "opdk", "hybrid", () => overview && renderEmergencyAvailability(overview));
  for (const b of $$("[data-emergency]")) b.addEventListener("click", () => {
    em.elements.opdk.value = b.dataset.emergency; em.elements.hybrid.value = String(100 - Number(b.dataset.emergency));
    if (overview) renderEmergencyAvailability(overview);
  });
  em.addEventListener("submit", reviewEmergency);
  $("#emergency-confirm-form").addEventListener("submit", confirmEmergency);
  $("#emergency-cancel").addEventListener("click", () => $("#emergency-dialog").close());

  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
  window.addEventListener("resize", () => { if (activeTab === "dashboard") renderCharts(); });

  if (sessionStorage.getItem(TOKEN_KEY)) {
    signIn(sessionStorage.getItem(TOKEN_KEY)).catch(() => signOut("Please sign in."));
  } else {
    signOut();
  }
}
document.addEventListener("DOMContentLoaded", init);
