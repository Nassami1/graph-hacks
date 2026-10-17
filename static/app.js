/* Frontend: polls /api/state and renders the live graph + coordination log.
 *
 * LAYOUT (deterministic — no physics jitter, positions never wobble):
 *
 *   SKILLS │ AGENTS │  WAVE 1      WAVE 2      WAVE 3     │ CAMPAIGN
 *     hex  │  box   │  [task] ──▶  [task] ──▶  [task] ──▶ │  (Run)
 *          │   ▲    │      └── DEPENDS_ON ──┘              │
 *          └───┴────│  ASSIGNED_TO / COMPLETED_BY (curved) │
 *
 * Every campaign ("Run") gets its own horizontal lane; tasks inside a lane are
 * placed in dependency waves, so the flow always reads left → right. Agents
 * and their skills live in the two left columns. Nodes are draggable; the
 * Reset button restores the computed layout.
 */

const $ = (id) => document.getElementById(id);

const C = {
  agent: { bg: "#a06bff", bgBusy: "#ffbf47", fg: "#170d2b", border: "#ffffff" },
  skill: { bg: "#2c8cc9", fg: "#f2fbff", border: "#8fd4ff" },
  run:   { bg: "#c94f6d", fg: "#2b0713", border: "#ffffff" },
  task: {
    pending:     { bg: "#39415a", fg: "#dfe6f5", border: "#626d8a" },
    in_progress: { bg: "#f0a12a", fg: "#241300", border: "#ffd489" },
    done:        { bg: "#2fbf71", fg: "#03200f", border: "#9df0c4" },
    error:       { bg: "#e05a5a", fg: "#2a0505", border: "#ffb3b3" },
  },
  edge: {
    DEPENDS_ON:   { color: { color: "#93a3ba", opacity: 0.95 }, width: 1.7, dashes: [7, 5], smooth: false },
    ASSIGNED_TO:  { color: { color: "#f0a12a", opacity: 0.9 },  width: 1.7, smooth: { type: "curved", roundness: 0.18 } },
    COMPLETED_BY: { color: { color: "#2fbf71", opacity: 0.9 },  width: 1.7, smooth: { type: "curved", roundness: -0.18 } },
    HAS_SKILL:    { color: { color: "#3ea3dd", opacity: 0.7 },  width: 1.2, smooth: false, arrows: { to: { enabled: false } } },
    PART_OF:      { color: { color: "#7b8699", opacity: 0.5 },  width: 1.1, dashes: [3, 6], smooth: false },
    COORDINATES:  { color: { color: "#a06bff", opacity: 0.75 }, width: 1.3, dashes: [5, 5], smooth: false },
  },
};

const ICON = {
  pending: "⏳", in_progress: "⚙️", done: "✅", error: "✕",
  running: "🔄", idle: "💤", busy: "⚙️",
};

/* ------------------------------------------------------------------ helpers */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function tip(lines) {
  return lines.filter(Boolean).map((l) => `<div style="margin:2px 0">${l}</div>`).join("");
}
function wrapTitle(text, max = 46) {
  const t = String(text || "");
  return t.length > max ? t.slice(0, max - 1).trimEnd() + "…" : t;
}
/** word-wrap into <= maxLines lines of ~max chars (nodes clip long single lines). */
function wrapLines(text, max = 20, maxLines = 3) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  const lines = [];
  let cur = "";
  words.forEach((w) => {
    if (!cur) cur = w;
    else if ((cur + " " + w).length <= max) cur += " " + w;
    else { lines.push(cur); cur = w; }
  });
  if (cur) lines.push(cur);
  if (lines.length > maxLines) {
    const last = lines.slice(maxLines - 1).join(" ");
    return lines.slice(0, maxLines - 1)
      .concat([last.length > max ? last.slice(0, max - 1) + "…" : last]);
  }
  return lines;
}

/* ------------------------------------------------------- dependency waves */
function computeWaves(tasks, depEdges) {
  const deps = {};
  depEdges.forEach((e) => {
    if (!e.from.startsWith("Task:") || !e.to.startsWith("Task:")) return;
    (deps[e.from] = deps[e.from] || []).push(e.to);
  });
  const memo = {};
  const wave = (id, guard) => {
    if (memo[id] !== undefined) return memo[id];
    if (guard.has(id)) return 0;          // cycle guard: never hang
    guard.add(id);
    let w = 0;
    (deps[id] || []).forEach((d) => { w = Math.max(w, wave(d, guard) + 1); });
    memo[id] = w;
    return w;
  };
  const out = {};
  tasks.forEach((t) => { out[t.id] = wave(t.id, new Set()); });
  return out;
}

/* ------------------------------------------------------------- layout */
const X = { skill: -270, agent: -10, task: 310, step: 265, runGap: 90 };
const Y = { task: 88, laneGap: 80, agent: 104, skill: 42 };

function computeLayout(state, manualPos) {
  const agents = state.nodes.filter((n) => n.type === "Agent");
  const skills = state.nodes.filter((n) => n.type === "Skill");
  const tasks  = state.nodes.filter((n) => n.type === "Task");
  const runs   = state.nodes.filter((n) => n.type === "Run");
  const wave = computeWaves(tasks, state.edges.filter((e) => e.type === "DEPENDS_ON"));

  // node ids are prefixed ("Run:x"), task.run_id is not ("x") — normalise so
  // every campaign finds its own lane.
  const runKey = (id) => (id ? (id.startsWith("Run:") ? id : "Run:" + id) : "__orphan__");
  const laneOf = (t) => runKey(t.run_id);

  const ts = {};
  runs.forEach((r) => { ts[r.id] = Number(r.ts) || 0; });

  // one lane per campaign, newest first (top)
  const laneIds = [...new Set(tasks.map(laneOf).concat(runs.map((r) => r.id)))]
    .sort((a, b) => (ts[b] || 0) - (ts[a] || 0));

  const pos = {};
  const laneBoxes = [];
  let cursor = 0;
  let maxWave = -1;

  laneIds.forEach((lane) => {
    const laneTasks = tasks.filter((t) => laneOf(t) === lane);
    const byWave = {};
    laneTasks.forEach((t) => {
      const w = wave[t.id] || 0;
      (byWave[w] = byWave[w] || []).push(t);
    });
    const waves = Object.keys(byWave).map(Number).sort((a, b) => a - b);
    let stack = 1;
    waves.forEach((w) => {
      maxWave = Math.max(maxWave, w);
      stack = Math.max(stack, byWave[w].length);
      byWave[w].forEach((t, i) => { pos[t.id] = { x: X.task + w * X.step, y: cursor + i * Y.task }; });
    });
    const laneBottom = cursor + (stack - 1) * Y.task;
    laneBoxes.push({ lane, top: cursor, bottom: laneBottom });
    cursor = laneBottom + Y.laneGap;
  });

  // every campaign ends in the SAME "CAMPAIGN" column (aligned with its header)
  const runX = X.task + (maxWave + 1) * X.step + X.runGap;
  laneBoxes.forEach((box) => {
    const runNode = runs.find((r) => r.id === box.lane);
    if (runNode) pos[runNode.id] = { x: runX, y: box.top + (box.bottom - box.top) / 2 };
  });

  // agents: vertically centred on the whole pipeline, coordinator on top
  const ys = Object.values(pos).map((p) => p.y);
  const midY = ys.length ? (Math.min(...ys) + Math.max(...ys)) / 2 : 0;
  const list = agents.filter((a) => a.role === "coordinator")
    .concat(agents.filter((a) => a.role !== "coordinator"));
  const h = (list.length - 1) * Y.agent;
  list.forEach((a, i) => { pos[a.id] = { x: X.agent, y: midY - h / 2 + i * Y.agent }; });

  // skills: right next to their owner agent
  const skillsOf = {};
  state.edges.filter((e) => e.type === "HAS_SKILL").forEach((e) => {
    (skillsOf[e.from] = skillsOf[e.from] || []).push(e.to);
  });
  const used = new Set();
  list.forEach((a) => {
    const ss = [...new Set(skillsOf[a.id] || [])].filter((s) => !used.has(s));
    ss.forEach((s) => used.add(s));
    const sh = (ss.length - 1) * Y.skill;
    ss.forEach((s, i) => { pos[s] = { x: X.skill, y: pos[a.id].y - sh / 2 + i * Y.skill }; });
  });
  skills.filter((s) => !pos[s.id]).forEach((s, i) => {
    pos[s.id] = { x: X.skill, y: midY + h / 2 + 140 + i * Y.skill };
  });

  // keep user-dragged nodes where the user put them
  Object.keys(manualPos).forEach((id) => { if (pos[id]) pos[id] = manualPos[id]; });

  // column headers (wave labels etc.) — client-side only, not real graph nodes
  const headers = [];
  const minY = Math.min(...Object.values(pos).map((p) => p.y));
  const hdrY = minY - 76;
  headers.push({ id: "hdr:skills", x: X.skill, y: hdrY, label: "SKILLS" });
  headers.push({ id: "hdr:agents", x: X.agent, y: hdrY, label: "AGENTS" });
  for (let w = 0; w <= Math.max(maxWave, 0); w++) {
    headers.push({
      id: "hdr:w" + w, x: X.task + w * X.step, y: hdrY,
      label: "WAVE " + (w + 1) + (w === 0 ? "\nready now" : ""),
    });
  }
  headers.push({
    id: "hdr:runs",
    x: X.task + (maxWave + 1) * X.step + X.runGap, y: hdrY,
    label: "CAMPAIGN",
  });

  return { pos, headers };
}

/* --------------------------------------------------------- node/edge items */
function nodeItems(state, pos, headers) {
  const items = [];
  const skillsOf = {};
  const holders = {};
  state.edges.filter((e) => e.type === "HAS_SKILL").forEach((e) => {
    (skillsOf[e.from] = skillsOf[e.from] || []).push(e.to);
    (holders[e.to] = holders[e.to] || []).push(e.from.replace("Agent:", ""));
  });

  headers.forEach((h) => {
    items.push({
      id: h.id, x: h.x, y: h.y, shape: "text", label: h.label,
      fixed: true, physics: false, selectable: false, choosable: false,
      font: { size: 12, color: "#8593ab", face: "Segoe UI", bold: true },
      widthConstraint: { maximum: 160 },
    });
  });

  state.nodes.forEach((n) => {
    const p = pos[n.id] || { x: 0, y: 0 };

    if (n.type === "Agent") {
      const busy = n.status === "busy";
      items.push({
        id: n.id, x: p.x, y: p.y, shape: "box",
        // busy agents stay PURPLE (orange = running *task* only) but glow
        label: (busy ? "⚙ " : "") + n.label + "\n" + (n.role || ""),
        title: tip([
          `<b>${esc(n.label)}</b> — ${esc(n.role)}`,
          `status: ${esc(n.status)}${busy ? " (working now)" : ""}`,
          `skills: ${esc((skillsOf[n.id] || []).join(", ") || "—")}`,
        ]),
        color: {
          background: C.agent.bg,
          border: busy ? "#ffffff" : C.agent.border,
          highlight: { background: C.agent.bg, border: "#fff" },
        },
        borderWidth: busy ? 3 : 1,
        shadow: busy
          ? { enabled: true, color: "rgba(160,107,255,0.95)", size: 18, x: 0, y: 0 }
          : { enabled: false },
        font: { color: C.agent.fg, size: 15, bold: true },
        margin: { top: 8, bottom: 8, left: 12, right: 12 },
        widthConstraint: { minimum: 120, maximum: 175 },
      });
      return;
    }

    if (n.type === "Skill") {
      items.push({
        id: n.id, x: p.x, y: p.y, shape: "hexagon", size: 15,
        label: n.label,
        title: tip([
          `skill: <b>${esc(n.label)}</b>`,
          "agents that hold it: " + esc((holders[n.id] || []).join(", ") || "—"),
        ]),
        color: { background: C.skill.bg, border: C.skill.border },
        font: { color: C.skill.fg, size: 13, bold: true },
      });
      return;
    }

    if (n.type === "Run") {
      const st = n.status || "done";
      items.push({
        id: n.id, x: p.x, y: p.y, shape: "database",
        label: (ICON[st] || "") + " " + wrapLines(n.label, 22, 3).join("\n"),
        title: tip([
          `<b>campaign</b> ${esc(n.label)}`,
          `status: ${esc(st)}`,
          `id: ${esc(String(n.id).replace("Run:", ""))}`,
        ]),
        color: { background: C.run.bg, border: C.run.border },
        font: { color: C.run.fg, size: 13, bold: true },
        widthConstraint: { maximum: 150 },
        // extra top margin keeps multi-line labels off the cylinder's "lid"
        margin: { top: 17, bottom: 11, left: 10, right: 10 },
      });
      return;
    }

    if (n.type === "Task") {
      const st = C.task[n.status] || C.task.pending;
      items.push({
        id: n.id, x: p.x, y: p.y, shape: "box",
        label: (ICON[n.status] || "") + " " + n.label,
        title: tip([
          `<b>${esc(n.label)}</b>`,
          `status: ${esc(n.status)}`,
          n.skill ? `skill: ${esc(n.skill)}` : null,
          n.assigned_to ? `assigned → ${esc(n.assigned_to)}` : null,
          n.completed_by ? `completed by ${esc(n.completed_by)}` : null,
          n.output ? `<i style="color:#9fb0c8">${esc(wrapTitle(n.output, 140))}</i>` : null,
        ]),
        color: { background: st.bg, border: st.border,
                 highlight: { background: st.bg, border: "#fff" } },
        font: { color: st.fg, size: 14, bold: n.status === "in_progress" },
        widthConstraint: { maximum: 175 },
        margin: { top: 7, bottom: 7, left: 10, right: 10 },
      });
    }
  });

  return items;
}

function edgeItems(state) {
  const taskById = {};
  state.nodes.filter((n) => n.type === "Task").forEach((t) => { taskById[t.id] = t; });
  const newest = state.nodes.filter((n) => n.type === "Run")
    .sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0))[0];
  const newestId = newest ? newest.id : "";

  /* ASSIGNED_TO and COMPLETED_BY describe ONE relationship — "this agent
     works on this task" — so we draw a single connector per pair: orange
     while the task runs, green once COMPLETED_BY has been written. Both edges
     still exist in FalkorDB; this only keeps the canvas readable. Connectors
     of older campaigns are drawn faint so the current run stays the focus. */
  const pairs = {};
  state.edges.forEach((e) => {
    if (e.type === "ASSIGNED_TO") {
      pairs[e.from + "|" + e.to] = { agent: e.from, task: e.to, done: false };
    } else if (e.type === "COMPLETED_BY") {
      const k = e.to + "|" + e.from;                 // agent|task
      if (!pairs[k]) pairs[k] = { agent: e.to, task: e.from, done: false };
      pairs[k].done = true;
    }
  });

  const items = [];
  Object.keys(pairs).forEach((k) => {
    const p = pairs[k];
    const t = taskById[p.task];
    const lane = t ? (t.run_id || "") : "";
    const isCurrent = !newestId || ("Run:" + lane) === newestId;
    const base = p.done ? C.edge.COMPLETED_BY : C.edge.ASSIGNED_TO;
    items.push({
      id: "assign:" + k,
      from: p.agent, to: p.task,
      title: esc(p.done ? "ASSIGNED_TO + COMPLETED_BY" : "ASSIGNED_TO"),
      arrows: { to: { enabled: true, scaleFactor: 0.5 } },
      color: { color: base.color.color, opacity: isCurrent ? 0.75 : 0.18 },
      width: base.width,
      smooth: { type: "horizontal" },   // tidy orthogonal-ish route, less fan-out
    });
  });

  state.edges.forEach((e) => {
    if (e.type === "ASSIGNED_TO" || e.type === "COMPLETED_BY") return;
    const s = C.edge[e.type] || { color: { color: "#888" }, smooth: false };
    items.push(Object.assign({
      id: e.from + "|" + e.to + "|" + e.type,
      from: e.from, to: e.to,
      title: esc(e.type),
      arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    }, s));
  });
  return items;
}

/* ------------------------------------------------------------------ network */
const nodes = new vis.DataSet([]);
const edges = new vis.DataSet([]);
const network = new vis.Network($("graph"), { nodes, edges }, {
  physics: false,
  nodes: {
    font: { color: "#eef2fa", size: 13, face: "Segoe UI, system-ui, sans-serif" },
    borderWidth: 1.5,
    shadow: { enabled: true, color: "rgba(0,0,0,0.4)", size: 7, x: 0, y: 3 },
  },
  edges: {
    selectionWidth: 2,
    hoverWidth: 1.5,
  },
  interaction: { hover: true, tooltipDelay: 130, dragView: true, zoomView: true },
});

let manualPos = {};

network.on("dragEnd", (e) => {
  (e.nodes || []).forEach((id) => {
    const d = nodes.get(id);
    if (d && d.x !== undefined) manualPos[id] = { x: d.x, y: d.y };
  });
});

/* ------------------------------------------------------------------- render */
let lastData = "";
let lastIds = "";

function dataSignature(state) {
  return JSON.stringify({
    n: state.nodes.map((n) => [n.id, n.status, n.label, n.output || "",
                               n.assigned_to || "", n.completed_by || ""]),
    e: state.edges.map((e) => [e.from, e.to, e.type]),
  });
}

function renderGraph(state) {
  const data = dataSignature(state);
  const ids = state.nodes.map((n) => n.id).sort().join(",");

  if (data !== lastData) {
    const { pos, headers } = computeLayout(state, manualPos);
    nodes.clear();
    nodes.add(nodeItems(state, pos, headers));
    edges.clear();
    edges.add(edgeItems(state));
    lastData = data;
  }
  if (ids !== lastIds) {
    lastIds = ids;
    const fit = () => { if (nodes.length) network.fit({ padding: 70 }); };
    setTimeout(fit, 60);
    setTimeout(fit, 420);   // re-fit once the canvas has settled (avoids clipping)
  }
}

function renderLog(events, activeRun) {
  const log = $("log");
  const shown = events.filter((e) => !activeRun || e.run_id === activeRun).slice(-60).reverse();
  log.innerHTML = shown.map((e) =>
    `<div class="entry ${esc(e.kind)}"><span class="who">${esc(e.agent)}</span> — ${esc(e.message)}</div>`
  ).join("");
  log.scrollTop = 0;
}

function setStatus(kind, text) {
  const b = $("statusBadge");
  b.className = "badge " + (kind === "busy" ? "badge-busy" : kind === "error" ? "badge-error" : "badge-idle");
  b.textContent = text;
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
  manualPos = {};
  lastData = ""; lastIds = "";
  await refresh();
}

$("runBtn").addEventListener("click", runCampaign);
$("resetBtn").addEventListener("click", resetAll);
$("fitBtn").addEventListener("click", () => { if (nodes.length) network.fit({ padding: 70 }); });

refresh();
setInterval(refresh, 1200);
