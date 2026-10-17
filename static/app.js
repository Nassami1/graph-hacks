/* Frontend: polls /api/state and renders the live graph + coordination log. */

const $ = (id) => document.getElementById(id);

const COLORS = {
  agent: "#a06bff",
  skill: "#2c8cc9",
  run: "#c94f6d",
  task: {
    pending: "#5b6472",
    in_progress: "#f39c12",
    done: "#2ecc71",
    error: "#e74c3c",
  },
  edge: {
    DEPENDS_ON: "#4a5568",
    ASSIGNED_TO: "#f39c12",
    COMPLETED_BY: "#2ecc71",
    HAS_SKILL: "#2c8cc9",
    PART_OF: "#5b6472",
    COORDINATES: "#a06bff",
  },
};

const nodes = new vis.DataSet([]);
const edges = new vis.DataSet([]);
const network = new vis.Network($("graph"), { nodes, edges }, {
  physics: {
    enabled: true,
    solver: "forceAtlas2Based",
    forceAtlas2Based: { gravitationalConstant: -60, springLength: 120, damping: 0.4 },
    stabilization: { iterations: 150 },
  },
  nodes: {
    font: { color: "#e6ebf4", size: 13, face: "Segoe UI" },
    borderWidth: 2,
    shadow: true,
  },
  edges: {
    smooth: { type: "continuous" },
    arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    font: { size: 9, color: "#8b96a8", align: "top" },
  },
  interaction: { hover: true, tooltipDelay: 120 },
});

function nodeStyle(n) {
  const base = { id: n.id, label: n.label, title: n.label };
  if (n.type === "Agent") {
    return { ...base, shape: "box", color: { background: COLORS.agent, border: "#fff", highlight: { background: "#b58aff" } } };
  }
  if (n.type === "Skill") {
    return { ...base, shape: "hexagon", size: 16, color: { background: COLORS.skill, border: "#2c8cc9" } };
  }
  if (n.type === "Run") {
    return { ...base, shape: "diamond", size: 22, color: { background: COLORS.run, border: "#fff" } };
  }
  if (n.type === "Task") {
    const c = COLORS.task[n.status] || COLORS.task.pending;
    return { ...base, shape: "box", color: { background: c, border: "#0000", highlight: { background: c } } };
  }
  return base;
}

function renderGraph(state) {
  const nArr = state.nodes.map(nodeStyle);
  const eArr = state.edges.map((e) => ({
    id: `${e.from}->${e.to}->${e.type}`,
    from: e.from,
    to: e.to,
    label: e.type,
    color: { color: COLORS.edge[e.type] || "#888", highlight: "#fff" },
    dashes: e.type === "DEPENDS_ON" || e.type === "COORDINATES",
  }));
  nodes.clear(); nodes.add(nArr);
  edges.clear(); edges.add(eArr);
}

function renderLog(events, activeRun) {
  const log = $("log");
  const shown = events.filter((e) => !activeRun || e.run_id === activeRun).slice(-60).reverse();
  log.innerHTML = shown.map((e) =>
    `<div class="entry ${e.kind}"><span class="who">${escapeHtml(e.agent)}</span> — ${escapeHtml(e.message)}</div>`
  ).join("");
  log.scrollTop = 0;
}

function setStatus(kind, text) {
  const b = $("statusBadge");
  b.className = "badge " + (kind === "busy" ? "badge-busy" : kind === "error" ? "badge-error" : "badge-idle");
  b.textContent = text;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function refresh() {
  const res = await fetch("/api/state");
  const state = await res.json();

  $("backendBadge").textContent = "backend: " + state.backend;
  $("llmBadge").textContent = "llm: " + (state.llm.available
    ? state.llm.provider + " · " + state.llm.model
    : "off (offline fallback)");

  renderGraph(state);
  renderLog(state.events, state.active_run);

  const running = state.runs.some((r) => r.status === "running");
  const hasError = state.runs.some((r) => r.status === "error");
  $("runBtn").disabled = running;
  if (hasError) setStatus("error", "error");
  else if (running) setStatus("busy", "running…");
  else setStatus("idle", "idle");
}

async function runCampaign() {
  const goal = $("goal").value.trim() || "Launch a marketing campaign for a new smart water bottle";
  $("goal").value = goal;
  $("runBtn").disabled = true;
  setStatus("busy", "planning…");
  await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal }),
  });
}

async function resetAll() {
  await fetch("/api/reset", { method: "POST" });
  await refresh();
}

$("runBtn").addEventListener("click", runCampaign);
$("resetBtn").addEventListener("click", resetAll);

refresh();
setInterval(refresh, 1200);
