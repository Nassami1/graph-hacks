"""Graph Hacks — Multi-Agent Coordination on a Shared Knowledge Graph
(single-file build)

Everything is bundled into this ONE file: graph storage (FalkorDB + in-memory
fallback), the LLM client (OpenAI + OpenRouter), the Coordinator/Worker agents,
the dependency-aware parallel scheduler, the Flask API, and the inlined
vis-network dashboard.

Run:
    python graph_hacks.py                          # dashboard at http://localhost:5000
    python graph_hacks.py demo "Campaign goal"     # CLI demo

Configuration comes from environment variables or a local ``.env`` file.
See ``.env.example``.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()



"""Thin LLM client wrapper supporting OpenAI **and** OpenRouter (both are
OpenAI-compatible HTTP APIs).

Provider selection
------------------
  * ``LLM_PROVIDER`` explicitly picks ``openai`` or ``openrouter``.
  * If unset, OpenRouter is used when ``OPENROUTER_API_KEY`` is present,
    otherwise OpenAI is used.

OpenAI
    OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL (default: gpt-4o-mini)

OpenRouter
    OPENROUTER_API_KEY / OPENROUTER_BASE_URL (default: https://openrouter.ai/api/v1)
    OPENROUTER_MODEL (default: openai/gpt-4o-mini)
    Optional: OPENROUTER_REFERER, OPENROUTER_APP_NAME (sent as HTTP-Referer/X-Title)

If no API key is configured, ``available`` is False and the agents use the
deterministic fallback logic instead, so the full demo still runs offline.
"""




OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class LLMClient:
    def __init__(self) -> None:
        explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
        or_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        oai_key = os.getenv("OPENAI_API_KEY", "").strip()

        if explicit in ("openrouter", "openai"):
            self.provider = explicit
        else:
            self.provider = "openrouter" if or_key else "openai"

        self._extra_headers: Optional[Dict[str, str]] = None

        if self.provider == "openrouter":
            self.api_key = or_key
            self.base_url = os.getenv(
                "OPENROUTER_BASE_URL", OPENROUTER_DEFAULT_BASE_URL).strip()
            self.model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()
            self._extra_headers = {
                "HTTP-Referer": os.getenv("OPENROUTER_REFERER",
                                          "http://localhost:5000").strip(),
                "X-Title": os.getenv("OPENROUTER_APP_NAME",
                                     "GraphHacks").strip(),
            }
        else:
            self.api_key = oai_key
            self.base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
            self.model = os.getenv("LLM_MODEL", "gpt-4o-mini").strip()

        self._client = None
        if self.api_key:
            from openai import OpenAI
            kwargs: Dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            if self._extra_headers:
                kwargs["default_headers"] = self._extra_headers
            self._client = OpenAI(**kwargs)

    @property
    def available(self) -> bool:
        return self._client is not None

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.4,
             max_tokens: int = 800) -> str:
        if not self._client:
            raise RuntimeError("LLM is not configured (set an API key).")
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()

    def chat_json(self, messages: List[Dict[str, str]],
                  temperature: float = 0.2) -> Any:
        text = self.chat(messages, temperature=temperature)
        return extract_json(text)


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM reply (handles code fences)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            if start == -1:
                continue
            depth = 0
            for i in range(start, len(text)):
                if text[i] == opener:
                    depth += 1
                elif text[i] == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
        raise ValueError(f"Could not parse JSON from LLM output: {text[:300]}")

"""Graph storage layer for the Graph Hacks demo.

Two interchangeable backends implement the *same* domain interface:

  * ``FalkorStore`` -> real FalkorDB (Cypher). Used for the actual submission
    so the judges see genuine graph queries.
  * ``MemoryStore``  -> in-memory NetworkX graph. Lets you run and demo the
    whole pipeline locally without Docker.

``build_store()`` picks FalkorDB when ``FALKORDB_URL`` / ``FALKORDB_HOST`` is
configured and reachable, otherwise it falls back to the in-memory graph.
"""



# --------------------------------------------------------------------------- #
# Base class (backend-agnostic logic lives here)
# --------------------------------------------------------------------------- #
class GraphStore:
    """Domain-level graph API used by the agents and the web app."""

    backend_name: str = "base"

    # ---- lifecycle -------------------------------------------------------- #
    def clear(self) -> None:
        raise NotImplementedError

    def is_seeded(self) -> bool:
        return len(self.list_agents()) > 0

    # ---- mutations -------------------------------------------------------- #
    def upsert_agent(self, agent_id: str, name: str, role: str,
                     status: str = "idle") -> None:
        raise NotImplementedError

    def add_skill(self, agent_id: str, skill: str) -> None:
        raise NotImplementedError

    def create_run(self, run_id: str, goal: str, status: str = "running",
                   ts: Optional[float] = None) -> None:
        raise NotImplementedError

    def set_run_status(self, run_id: str, status: str) -> None:
        raise NotImplementedError

    def create_task(self, task_id: str, title: str, skill: str,
                    priority: int = 1, run_id: str = "") -> None:
        raise NotImplementedError

    def add_dependency(self, task_id: str, depends_on_id: str) -> None:
        raise NotImplementedError

    def set_task_status(self, task_id: str, status: str) -> None:
        raise NotImplementedError

    def assign_task(self, task_id: str, agent_id: str) -> None:
        raise NotImplementedError

    def complete_task(self, task_id: str, agent_id: str, output: str) -> None:
        raise NotImplementedError

    def set_agent_status(self, agent_id: str, status: str) -> None:
        raise NotImplementedError

    def add_event(self, run_id: str, agent: str, message: str, kind: str) -> None:
        raise NotImplementedError

    # ---- reads (normalized, backend-agnostic) ----------------------------- #
    def list_agents(self) -> List[Dict[str, Any]]:
        """Each dict: {id, name, role, status, skills:[...]}."""
        raise NotImplementedError

    def list_tasks(self) -> List[Dict[str, Any]]:
        """Each dict: {id, title, status, priority, skill, run_id,
        assigned_to, completed_by, output}."""
        raise NotImplementedError

    def list_dependencies(self) -> List[Tuple[str, str]]:
        """List of (task_id, depends_on_task_id)."""
        raise NotImplementedError

    def list_runs(self) -> List[Dict[str, Any]]:
        """Each dict: {id, goal, status, ts}."""
        raise NotImplementedError

    def list_events(self) -> List[Dict[str, Any]]:
        """Each dict: {run_id, agent, kind, message, ts}."""
        raise NotImplementedError

    def list_skill_edges(self) -> List[Tuple[str, str]]:
        """List of (agent_id, skill_name)."""
        raise NotImplementedError

    # ---- shared derived logic -------------------------------------------- #
    def get_ready_tasks(self, run_id: str = "") -> List[Dict[str, Any]]:
        """Pending tasks whose dependencies are all ``done``."""
        all_tasks = self.list_tasks()
        done = {t["id"] for t in all_tasks if t["status"] == "done"}
        deps = self.list_dependencies()
        tasks = all_tasks if not run_id else [t for t in all_tasks if t.get("run_id") == run_id]
        ready: List[Dict[str, Any]] = []
        for t in tasks:
            if t["status"] != "pending":
                continue
            if all(dep in done for (tid, dep) in deps if tid == t["id"]):
                ready.append(t)
        return ready

    def match_agent(self, skill: str) -> Optional[str]:
        """Pick the best agent id for a task skill (idle skill-match first)."""
        agents = self.list_agents()
        candidates = [a for a in agents if skill in a.get("skills", [])]
        if not candidates:
            candidates = [a for a in agents if a.get("role") != "coordinator"] or agents
        idle = [a for a in candidates if a.get("status") == "idle"]
        pool = idle or candidates
        return pool[0]["id"] if pool else None

    def get_past_runs_summary(self, current_run_id: str = "",
                              limit: int = 3) -> List[Dict[str, Any]]:
        """Company brain: recent completed campaigns and what they produced."""
        runs = [r for r in self.list_runs() if r["id"] != current_run_id]
        runs.sort(key=lambda r: r.get("ts", 0), reverse=True)
        tasks = self.list_tasks()
        out: List[Dict[str, Any]] = []
        for r in runs[:limit]:
            rtasks = [t for t in tasks if t.get("run_id") == r["id"] and t["status"] == "done"]
            out.append({
                "run": r["id"],
                "goal": r["goal"],
                "completed_tasks": [t["title"] for t in rtasks],
            })
        return out


    def snapshot(self) -> Dict[str, Any]:
        """Normalized {nodes, edges, events} used by the visualizer."""
        agents = self.list_agents()
        tasks = self.list_tasks()
        runs = self.list_runs()
        skill_edges = self.list_skill_edges()
        deps = self.list_dependencies()
        events = sorted(self.list_events(), key=lambda e: e.get("ts", 0))

        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []

        for a in agents:
            nodes.append({"id": f"Agent:{a['id']}", "type": "Agent",
                          "label": a.get("name") or a["id"],
                          "status": a.get("status"), "role": a.get("role")})

        seen_skills: set = set()
        for agent_id, skill in skill_edges:
            sid = f"Skill:{skill}"
            if sid not in seen_skills:
                seen_skills.add(sid)
                nodes.append({"id": sid, "type": "Skill", "label": skill})
            edges.append({"from": f"Agent:{agent_id}", "to": sid, "type": "HAS_SKILL"})

        for t in tasks:
            nodes.append({"id": f"Task:{t['id']}", "type": "Task",
                          "label": t.get("title", t["id"]),
                          "status": t.get("status"), "priority": t.get("priority"),
                          "skill": t.get("skill", ""),
                          "assigned_to": t.get("assigned_to", ""),
                          "output": t.get("output", ""),
                          "run_id": t.get("run_id", "")})

        for r in runs:
            nodes.append({"id": f"Run:{r['id']}", "type": "Run",
                          "label": r.get("goal", r["id"]), "status": r.get("status")})

        for task_id, dep_id in deps:
            edges.append({"from": f"Task:{task_id}", "to": f"Task:{dep_id}",
                          "type": "DEPENDS_ON"})

        for t in tasks:
            if t.get("assigned_to"):
                edges.append({"from": f"Agent:{t['assigned_to']}",
                              "to": f"Task:{t['id']}", "type": "ASSIGNED_TO"})
            if t.get("completed_by"):
                edges.append({"from": f"Task:{t['id']}",
                              "to": f"Agent:{t['completed_by']}", "type": "COMPLETED_BY"})
            if t.get("run_id"):
                edges.append({"from": f"Task:{t['id']}",
                              "to": f"Run:{t['run_id']}", "type": "PART_OF"})

        coordinator = next((a for a in agents if a.get("role") == "coordinator"), None)
        if coordinator:
            for a in agents:
                if a["id"] != coordinator["id"] and a.get("role") != "coordinator":
                    edges.append({"from": f"Agent:{coordinator['id']}",
                                  "to": f"Agent:{a['id']}", "type": "COORDINATES"})

        return {"nodes": nodes, "edges": edges, "events": events}


# --------------------------------------------------------------------------- #
# In-memory backend (NetworkX)
# --------------------------------------------------------------------------- #
class MemoryStore(GraphStore):
    backend_name = "in-memory"

    def __init__(self) -> None:
        import networkx as nx
        self._g = nx.MultiDiGraph()
        self._events: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    # ---- helpers ---------------------------------------------------------- #
    def _set_node(self, node_id: str, label: str, props: Dict[str, Any]) -> None:
        self._g.add_node(node_id, labels={label}, props=dict(props))

    def _add_edge(self, u: str, v: str, relation: str) -> None:
        existing = self._g.get_edge_data(u, v) or {}
        if any(d.get("relation") == relation for d in existing.values()):
            return
        self._g.add_edge(u, v, relation=relation)

    # ---- lifecycle -------------------------------------------------------- #
    def clear(self) -> None:
        with self._lock:
            self._g.clear()
            self._events.clear()

    # ---- mutations -------------------------------------------------------- #
    def upsert_agent(self, agent_id, name, role, status="idle"):
        with self._lock:
            self._set_node(agent_id, "Agent",
                           {"id": agent_id, "name": name, "role": role, "status": status})

    def add_skill(self, agent_id, skill):
        with self._lock:
            sid = f"skill::{skill}"
            self._set_node(sid, "Skill", {"id": sid, "name": skill})
            self._add_edge(agent_id, sid, "HAS_SKILL")

    def create_run(self, run_id, goal, status="running", ts=None):
        with self._lock:
            self._set_node(run_id, "Run",
                           {"id": run_id, "goal": goal, "status": status,
                            "ts": ts if ts is not None else time.time()})

    def set_run_status(self, run_id, status):
        with self._lock:
            if run_id in self._g:
                self._g.nodes[run_id]["props"]["status"] = status

    def create_task(self, task_id, title, skill, priority=1, run_id=""):
        with self._lock:
            self._set_node(task_id, "Task", {
                "id": task_id, "title": title, "status": "pending",
                "priority": priority, "skill": skill, "run_id": run_id,
                "assigned_to": "", "completed_by": "", "output": "",
            })

    def add_dependency(self, task_id, depends_on_id):
        with self._lock:
            self._add_edge(task_id, depends_on_id, "DEPENDS_ON")

    def set_task_status(self, task_id, status):
        with self._lock:
            if task_id in self._g:
                self._g.nodes[task_id]["props"]["status"] = status

    def assign_task(self, task_id, agent_id):
        with self._lock:
            if task_id in self._g:
                self._g.nodes[task_id]["props"]["status"] = "in_progress"
                self._g.nodes[task_id]["props"]["assigned_to"] = agent_id
            self._add_edge(agent_id, task_id, "ASSIGNED_TO")

    def complete_task(self, task_id, agent_id, output):
        with self._lock:
            if task_id in self._g:
                self._g.nodes[task_id]["props"]["status"] = "done"
                self._g.nodes[task_id]["props"]["completed_by"] = agent_id
                self._g.nodes[task_id]["props"]["output"] = output
            self._add_edge(task_id, agent_id, "COMPLETED_BY")

    def set_agent_status(self, agent_id, status):
        with self._lock:
            if agent_id in self._g and "Agent" in self._g.nodes[agent_id]["labels"]:
                self._g.nodes[agent_id]["props"]["status"] = status

    def add_event(self, run_id, agent, message, kind):
        with self._lock:
            self._events.append({"run_id": run_id, "agent": agent,
                                 "kind": kind, "message": message, "ts": time.time()})

    # ---- reads ------------------------------------------------------------ #
    def list_agents(self):
        with self._lock:
            out = []
            for nid, data in self._g.nodes(data=True):
                if "Agent" in data["labels"]:
                    props = dict(data["props"])
                    skills = []
                    for s in self._g.successors(nid):
                        sdata = self._g.nodes[s]
                        if "Skill" in sdata["labels"]:
                            skills.append(sdata["props"]["name"])
                    props["skills"] = skills
                    out.append(props)
            return out

    def list_tasks(self):
        with self._lock:
            return [dict(d["props"]) for _, d in self._g.nodes(data=True)
                    if "Task" in d["labels"]]

    def list_dependencies(self):
        with self._lock:
            return [(u, v) for u, v, d in self._g.edges(data=True)
                    if d.get("relation") == "DEPENDS_ON"]

    def list_runs(self):
        with self._lock:
            return [dict(d["props"]) for _, d in self._g.nodes(data=True)
                    if "Run" in d["labels"]]

    def list_events(self):
        with self._lock:
            return [dict(e) for e in self._events]

    def list_skill_edges(self):
        with self._lock:
            out = []
            for u, v, d in self._g.edges(data=True):
                if d.get("relation") == "HAS_SKILL":
                    out.append((u, self._g.nodes[v]["props"]["name"]))
            return out


# --------------------------------------------------------------------------- #
# FalkorDB backend (real Cypher)
# --------------------------------------------------------------------------- #
class FalkorStore(GraphStore):
    backend_name = "falkordb"

    def __init__(self, host: str = "localhost", port: int = 6379,
                 username: Optional[str] = None, password: Optional[str] = None,
                 url: Optional[str] = None, graph_name: str = "coordination") -> None:
        from falkordb import FalkorDB
        if url:
            self._db = FalkorDB.from_url(url)
        else:
            self._db = FalkorDB(host=host, port=port, username=username, password=password)
        self.graph = self._db.select_graph(graph_name)
        self._lock = threading.RLock()

    def _q(self, q: str, params: Optional[Dict[str, Any]] = None):
        with self._lock:
            return self.graph.query(q, params=params)

    # ---- lifecycle -------------------------------------------------------- #
    def clear(self) -> None:
        with self._lock:
            self.graph.delete()

    # ---- mutations -------------------------------------------------------- #
    def upsert_agent(self, agent_id, name, role, status="idle"):
        self._q("MERGE (a:Agent {id: $id}) SET a.name=$name, a.role=$role, a.status=$status",
                {"id": agent_id, "name": name, "role": role, "status": status})

    def add_skill(self, agent_id, skill):
        self._q("MERGE (s:Skill {name: $skill})", {"skill": skill})
        self._q("MATCH (a:Agent {id: $id}), (s:Skill {name: $skill}) MERGE (a)-[:HAS_SKILL]->(s)",
                {"id": agent_id, "skill": skill})

    def create_run(self, run_id, goal, status="running", ts=None):
        self._q("CREATE (r:Run {id: $id, goal: $goal, status: $status, ts: $ts})",
                {"id": run_id, "goal": goal, "status": status,
                 "ts": ts if ts is not None else time.time()})

    def set_run_status(self, run_id, status):
        self._q("MATCH (r:Run {id: $id}) SET r.status=$status",
                {"id": run_id, "status": status})

    def create_task(self, task_id, title, skill, priority=1, run_id=""):
        self._q(
            "CREATE (t:Task {id: $id, title: $title, status: 'pending', "
            "priority: $priority, skill: $skill, run_id: $run_id, "
            "assigned_to: '', completed_by: '', output: ''})",
            {"id": task_id, "title": title, "priority": priority,
             "skill": skill, "run_id": run_id},
        )

    def add_dependency(self, task_id, depends_on_id):
        self._q("MATCH (t:Task {id: $id}), (d:Task {id: $dep}) MERGE (t)-[:DEPENDS_ON]->(d)",
                {"id": task_id, "dep": depends_on_id})

    def set_task_status(self, task_id, status):
        self._q("MATCH (t:Task {id: $id}) SET t.status=$status",
                {"id": task_id, "status": status})

    def assign_task(self, task_id, agent_id):
        self._q("MATCH (t:Task {id: $id}) SET t.status='in_progress', t.assigned_to=$agent",
                {"id": task_id, "agent": agent_id})
        self._q("MATCH (a:Agent {id: $agent}), (t:Task {id: $id}) MERGE (a)-[:ASSIGNED_TO]->(t)",
                {"agent": agent_id, "id": task_id})

    def complete_task(self, task_id, agent_id, output):
        self._q("MATCH (t:Task {id: $id}) SET t.status='done', t.completed_by=$agent, t.output=$output",
                {"id": task_id, "agent": agent_id, "output": output})
        self._q("MATCH (t:Task {id: $id}), (a:Agent {id: $agent}) MERGE (t)-[:COMPLETED_BY]->(a)",
                {"id": task_id, "agent": agent_id})

    def set_agent_status(self, agent_id, status):
        self._q("MATCH (a:Agent {id: $id}) SET a.status=$status",
                {"id": agent_id, "status": status})

    def add_event(self, run_id, agent, message, kind):
        self._q(
            "CREATE (e:Event {id: $id, run_id: $run_id, agent: $agent, "
            "kind: $kind, message: $message, ts: $ts})",
            {"id": f"evt-{time.time_ns()}", "run_id": run_id, "agent": agent,
             "kind": kind, "message": message, "ts": time.time()},
        )

    # ---- reads ------------------------------------------------------------ #
    def list_agents(self):
        res = self._q("MATCH (a:Agent) RETURN a")
        skills: Dict[str, List[str]] = {}
        res2 = self._q("MATCH (a:Agent)-[:HAS_SKILL]->(s:Skill) RETURN a.id, s.name")
        for row in res2.result_set:
            skills.setdefault(row[0], []).append(row[1])
        out = []
        for row in res.result_set:
            props = dict(row[0].properties)
            props["skills"] = skills.get(props.get("id"), [])
            out.append(props)
        return out

    def list_tasks(self):
        res = self._q("MATCH (t:Task) RETURN t")
        return [dict(row[0].properties) for row in res.result_set]

    def list_dependencies(self):
        res = self._q("MATCH (t:Task)-[:DEPENDS_ON]->(d:Task) RETURN t.id, d.id")
        return [(row[0], row[1]) for row in res.result_set]

    def list_runs(self):
        res = self._q("MATCH (r:Run) RETURN r")
        return [dict(row[0].properties) for row in res.result_set]

    def list_events(self):
        res = self._q("MATCH (e:Event) RETURN e")
        events = [dict(row[0].properties) for row in res.result_set]
        events.sort(key=lambda e: e.get("ts", 0))
        return events

    def list_skill_edges(self):
        res = self._q("MATCH (a:Agent)-[:HAS_SKILL]->(s:Skill) RETURN a.id, s.name")
        return [(row[0], row[1]) for row in res.result_set]


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def _probe(host: str, port: int, timeout: float = 0.6) -> bool:
    """Cheap TCP reachability check (used to auto-detect a local FalkorDB)."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_store() -> GraphStore:
    """Build the best available store (FalkorDB if configured & reachable).

    Selection order:
      1. ``FALKORDB_URL`` / ``FALKORDB_HOST`` explicitly set  -> use it
         (falls back to in-memory if it is unreachable).
      2. nothing set -> probe ``localhost:6379`` once; if a FalkorDB is
         already running there (e.g. the ``docker run`` from the README)
         it is picked up automatically, so judges never end up on the
         in-memory fallback just because ``.env`` was missing.
      3. otherwise -> in-memory NetworkX graph.
    """
    url = os.getenv("FALKORDB_URL", "").strip()
    host = os.getenv("FALKORDB_HOST", "").strip()
    port = int(os.getenv("FALKORDB_PORT", "6379"))

    if not url and not host:
        if _probe("localhost", port):
            host = "localhost"
            print("[graph_store] auto-detected FalkorDB on localhost:%d" % port,
                  file=sys.stderr)
        else:
            return MemoryStore()

    try:
        store = FalkorStore(
            url=url or None,
            host=host or "localhost",
            port=port,
            username=os.getenv("FALKORDB_USERNAME", "").strip() or None,
            password=os.getenv("FALKORDB_PASSWORD", "").strip() or None,
        )
        store.graph.query("RETURN 1")  # connectivity check
        return store
    except Exception as exc:  # noqa: BLE001
        print(f"[graph_store] FalkorDB unreachable ({exc}); "
              f"falling back to in-memory graph", file=sys.stderr)
    return MemoryStore()

"""Multi-agent coordination on a shared graph.

  * :class:`Coordinator` reads the graph (team, skills, past runs = company
    brain), breaks a goal into subtasks, and creates them in the graph with
    ``DEPENDS_ON`` edges.
  * :class:`Worker` executes a task (via LLM, or deterministically offline)
    and updates the graph (``ASSIGNED_TO`` / ``COMPLETED_BY`` + status).
  * ``run_campaign`` is the scheduler: it runs every "ready" wave of tasks in
    parallel (bounded by the number of workers) and respects dependencies.
"""


DEFAULT_GOAL = "Launch a marketing campaign for a new smart water bottle"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def deterministic_output(task: Dict[str, Any], goal: str) -> str:
    """Offline worker deliverable (used when no LLM key is configured)."""
    skill = task.get("skill") or ""
    title = task.get("title", "task")
    if skill == "research":
        return (f"Researched target audience, competitors and market trends for "
                f"«{goal}». Identified 3 key segments and a positioning angle.")
    if skill == "content":
        return f"Drafted campaign headline, value proposition and CTA copy for «{goal}»."
    if skill == "design":
        return f"Created visual direction: palette, typography and hero banner for «{goal}»."
    if skill == "launch":
        return f"Scheduled and published the campaign across channels for «{goal}»."
    if skill == "strategy":
        return f"Defined positioning, channel mix and budget split for «{goal}»."
    if skill == "analysis":
        return f"Defined KPIs and a measurement plan for «{goal}»."
    return f"Completed «{title}»."


def normalize_plan(data: Any, run_id: str, skills: set) -> Dict[str, Any]:
    """Validate/normalize an LLM plan into task dicts with stable unique ids."""
    raw = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("LLM returned no 'tasks' list")

    run_short = run_id.split("-")[-1]
    id_map: Dict[str, str] = {}
    for i, t in enumerate(raw):
        short = str(t.get("id") or f"t{i + 1}")
        id_map[short] = f"{run_short}-{short}"

    tasks: List[Dict[str, Any]] = []
    for t in raw:
        short = str(t.get("id") or f"t{len(tasks) + 1}")
        skill = t.get("skill")
        if skill not in skills:
            skill = ""  # unknown skill -> any available worker
        deps = [id_map[str(d)] for d in (t.get("depends_on") or []) if str(d) in id_map]
        tasks.append({
            "id": id_map[short],
            "title": str(t.get("title") or f"Task {len(tasks) + 1}"),
            "skill": skill or "",
            "priority": int(t.get("priority") or 1),
            "depends_on": deps,
        })
    return {"summary": str(data.get("summary") or "Plan created by the LLM."),
            "tasks": tasks}


# --------------------------------------------------------------------------- #
# Coordinator
# --------------------------------------------------------------------------- #
class Coordinator:
    agent_id = "Coordinator"

    def __init__(self, store, llm) -> None:
        self.store = store
        self.llm = llm

    def plan(self, goal: str, run_id: str) -> Dict[str, Any]:
        workers = [a for a in self.store.list_agents()
                   if a.get("role") != "coordinator"]
        skills = sorted({s for a in workers for s in a.get("skills", [])})
        past = self.store.get_past_runs_summary(current_run_id=run_id, limit=3)

        if self.llm.available:
            try:
                return self._plan_with_llm(goal, workers, skills, past, run_id)
            except Exception as exc:  # noqa: BLE001
                self.store.add_event(
                    run_id, self.agent_id,
                    f"LLM planning failed ({exc}); using the fallback plan.", "warn")
        return self._plan_fallback(goal, run_id)

    def _plan_with_llm(self, goal, workers, skills, past, run_id) -> Dict[str, Any]:
        worker_lines = "\n".join(
            f"- {a['name']} (role: {a['role']}, skills: {', '.join(a.get('skills', []))})"
            for a in workers)
        past_lines = "\n".join(
            f"- {r['goal']} -> completed: {', '.join(r['completed_tasks']) or '—'}"
            for r in past) or "(no past campaigns yet)"

        system = (
            "You are the Coordinator of a multi-agent team that shares a knowledge "
            "graph. Break the user's goal into 3-6 subtasks and return ONLY JSON.\n\n"
            f"Workers (use only these skills):\n{worker_lines}\n\n"
            f"Available skills: {', '.join(skills)}\n\n"
            f"Company memory (past campaigns):\n{past_lines}\n\n"
            "Rules:\n"
            "- Give each subtask: id (t1, t2, ...), a short title, one skill from the "
            "list above, a priority number, and a depends_on list of ids.\n"
            "- Use depends_on to express ordering; leave independent tasks with no "
            "dependency so they can run in PARALLEL.\n"
            '- Return ONLY valid JSON: {"summary": "...", "tasks": '
            '[{"id": "t1", "title": "...", "skill": "...", "priority": 1, '
            '"depends_on": []}]}'
        )
        data = self.llm.chat_json([
            {"role": "system", "content": system},
            {"role": "user", "content": f"Goal: {goal}"},
        ])
        return normalize_plan(data, run_id, set(skills))

    def _plan_fallback(self, goal: str, run_id: str) -> Dict[str, Any]:
        """Deterministic 4-step marketing pipeline (offline demo)."""
        run_short = run_id.split("-")[-1]

        def tid(n: int) -> str:
            return f"{run_short}-t{n}"

        tasks = [
            {"id": tid(1), "title": "Market & audience research", "skill": "research",
             "priority": 1, "depends_on": []},
            {"id": tid(2), "title": "Content & messaging", "skill": "content",
             "priority": 2, "depends_on": [tid(1)]},
            {"id": tid(3), "title": "Visual design & assets", "skill": "design",
             "priority": 2, "depends_on": [tid(1)]},
            {"id": tid(4), "title": "Launch & distribution", "skill": "launch",
             "priority": 3, "depends_on": [tid(2), tid(3)]},
        ]
        summary = (f"4-step pipeline for «{goal}»: research, then content + design "
                   f"in parallel, then launch.")
        return {"summary": summary, "tasks": tasks}


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
class Worker:
    def __init__(self, agent_id: str, name: str, store, llm, delay: float = 0.8) -> None:
        self.agent_id = agent_id
        self.name = name
        self.store = store
        self.llm = llm
        self.delay = delay

    def execute(self, task: Dict[str, Any], run_id: str, goal: str) -> str:
        self.store.add_event(run_id, self.name, f"Started «{task['title']}».", "task")
        if self.delay:
            time.sleep(self.delay)
        output = self._produce(task, goal)
        self.store.complete_task(task["id"], self.agent_id, output)
        self.store.add_event(run_id, self.name,
                             f"Finished «{task['title']}» — {output[:120]}", "done")
        return output

    def _produce(self, task: Dict[str, Any], goal: str) -> str:
        if self.llm.available:
            try:
                system = (
                    f"You are '{self.name}', a worker agent in a coordinated team.\n"
                    f"Your skill for this task: {task.get('skill') or 'general'}.\n"
                    "Produce a concise, concrete deliverable (2-4 sentences). "
                    "No preamble, just the result."
                )
                return self.llm.chat(
                    [
                        {"role": "system", "content": system},
                        {"role": "user",
                         "content": f"Goal: {goal}\nTask: {task['title']}"},
                    ],
                    max_tokens=250,
                )
            except Exception:  # noqa: BLE001
                pass
        return deterministic_output(task, goal)


# --------------------------------------------------------------------------- #
# Scheduler (the "coordination loop")
# --------------------------------------------------------------------------- #
def run_campaign(store, llm, goal: str, run_id: Optional[str] = None,
                 delay: float = 0.8) -> str:
    """Plan + execute a campaign, updating the graph live. Returns run_id."""
    if run_id is None:
        run_id = f"run-{uuid.uuid4().hex[:6]}"

    store.create_run(run_id, goal, status="running")
    store.add_event(run_id, "Coordinator", f"New campaign: «{goal}»", "info")

    coordinator = Coordinator(store, llm)
    plan = coordinator.plan(goal, run_id)
    store.add_event(run_id, "Coordinator", f"Plan — {plan['summary']}", "plan")

    for t in plan["tasks"]:
        store.create_task(t["id"], t["title"], t["skill"],
                          t.get("priority", 1), run_id)
    for t in plan["tasks"]:
        for dep in t.get("depends_on", []):
            store.add_dependency(t["id"], dep)

    workers = {
        a["id"]: Worker(a["id"], a.get("name", a["id"]), store, llm, delay)
        for a in store.list_agents() if a.get("role") != "coordinator"
    }

    # Execution: run every "ready" wave in parallel.
    while True:
        ready = store.get_ready_tasks(run_id)
        if not ready:
            break

        assignments = []
        for t in sorted(ready, key=lambda x: (x.get("priority", 1), x["id"])):
            agent_id = store.match_agent(t.get("skill")) or next(iter(workers))
            store.assign_task(t["id"], agent_id)
            store.set_agent_status(agent_id, "busy")
            store.add_event(run_id, "Coordinator",
                            f"Assigning «{t['title']}» → {agent_id}", "assign")
            assignments.append((t, agent_id))

        with ThreadPoolExecutor(max_workers=len(workers)) as pool:
            futures = {pool.submit(workers[aid].execute, t, run_id, goal): (t, aid)
                       for t, aid in assignments}
            for fut in as_completed(futures):
                _, aid = futures[fut]
                fut.result()
                store.set_agent_status(aid, "idle")

    store.set_run_status(run_id, "done")
    store.add_event(run_id, "Coordinator", f"Campaign «{goal}» completed ✅", "done")
    return run_id

"""Seed the demo graph: team, skills, and one past campaign (company brain).

Idempotent — calling it again on an already-seeded store does nothing, so it
is safe to run at app startup and from the CLI.
"""



def seed_graph(store) -> None:
    if store.is_seeded():
        return

    # Team ---------------------------------------------------------------- #
    store.upsert_agent("Coordinator", "Coordinator", "coordinator", "idle")
    store.upsert_agent("Atlas", "Atlas", "researcher", "idle")
    store.upsert_agent("Nova", "Nova", "creator", "idle")

    # Skills -------------------------------------------------------------- #
    store.add_skill("Coordinator", "planning")
    store.add_skill("Atlas", "research")
    store.add_skill("Atlas", "design")
    store.add_skill("Nova", "content")
    store.add_skill("Nova", "launch")

    # A past campaign -> demonstrates the "company brain" (memory) track --- #
    past_run = "run-past-1"
    store.create_run(past_run, "Summer sale campaign for a coffee brand",
                     status="done", ts=time.time() - 86400)
    past_tasks = [
        ("past-1-t1", "Competitor & price research", "research"),
        ("past-1-t2", "Promo email copy", "content"),
        ("past-1-t3", "Social media visuals", "design"),
    ]
    owner = {"research": "Atlas", "content": "Nova", "design": "Atlas"}
    for task_id, title, skill in past_tasks:
        store.create_task(task_id, title, skill, priority=1, run_id=past_run)
        store.complete_task(task_id, owner[skill],
                            f"(history) {title} — completed during the summer sale run.")



# --------------------------------------------------------------------------- #
# Flask app (frontend is inlined -> truly a single file)
# --------------------------------------------------------------------------- #
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Graph Hacks — Multi-Agent Coordination</title>
  <style>
:root {
  --bg: #0f1420;
  --panel: #171d2b;
  --border: #26303f;
  --text: #e6ebf4;
  --muted: #8b96a8;
  --accent: #4f8cff;
  --green: #2ecc71;
  --orange: #f39c12;
  --red: #e74c3c;
  --purple: #a06bff;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font-family: "Segoe UI", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  display: flex;
  flex-direction: column;
  height: 100vh;
}

header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  gap: 8px;
}

header h1 { font-size: 18px; margin: 0; font-weight: 600; }
header h1 span { color: var(--muted); font-weight: 400; font-size: 13px; }

.badges { display: flex; gap: 8px; }
.badge {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--muted);
  background: var(--panel);
}
.badge-idle { color: var(--green); border-color: var(--green); }
.badge-busy { color: var(--orange); border-color: var(--orange); }
.badge-error { color: var(--red); border-color: var(--red); }

.controls {
  display: flex;
  gap: 10px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
}

#goal {
  flex: 1;
  padding: 10px 14px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  font-size: 14px;
}
#goal:focus { outline: 1px solid var(--accent); }

.btn {
  padding: 10px 18px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  cursor: pointer;
  font-size: 14px;
}
.btn:hover { border-color: var(--accent); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }

main {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 380px;
  min-height: 0;
}

.panel { min-height: 0; }

.graph-panel {
  position: relative;
  border-right: 1px solid var(--border);
}
#graph { position: absolute; inset: 0; }

.legend {
  position: absolute;
  bottom: 12px;
  left: 12px;
  right: 12px;
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  align-items: center;
  background: rgba(17, 22, 34, 0.92);
  padding: 8px 14px;
  border-radius: 10px;
  border: 1px solid var(--border);
  font-size: 11.5px;
  color: var(--muted);
  pointer-events: none;
}
.lg-group { display: inline-flex; align-items: center; gap: 11px; flex-wrap: wrap; }
.lg-title {
  text-transform: uppercase;
  letter-spacing: 0.7px;
  font-size: 10px;
  color: #5b6577;
  border-right: 1px solid var(--border);
  padding-right: 10px;
}
.lg { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.lg-hint { font-size: 10.5px; color: #97a2b4; margin-left: auto; font-style: italic; }

/* node swatches (shapes match the graph) */
.sh { display: inline-block; width: 13px; height: 13px; flex: 0 0 auto; }
.sh-agent    { background: var(--purple); border: 1px solid #fff; border-radius: 2px; }
.sh-agent-busy { background: var(--purple); border: 2px solid #fff; border-radius: 2px;
                 box-shadow: 0 0 7px 1px rgba(160,107,255,.95); }
.sh-skill    { background: #2c8cc9; clip-path: polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%); }
.sh-pending  { background: #39415a; border: 1px solid #626d8a; border-radius: 2px; }
.sh-active   { background: #f0a12a; border: 1px solid #ffd489; border-radius: 2px; }
.sh-done     { background: #2fbf71; border: 1px solid #9df0c4; border-radius: 2px; }
.sh-run      { background: #c94f6d; border: 1px solid #fff; border-radius: 50% / 30%; }

/* edge swatches */
.ln { display: inline-block; width: 24px; height: 0; border-top-width: 2px; flex: 0 0 auto; }
.ln-depends  { border-top: 2px dashed #93a3ba; }
.ln-assigned { border-top: 2px solid #f0a12a; }
.ln-completed{ border-top: 2px solid #2fbf71; }
.ln-skill    { border-top: 2px solid rgba(62, 163, 221, .8); }
.ln-part     { border-top: 2px dotted #7b8699; }
.ln-coord    { border-top: 2px dashed #a06bff; }

.log-panel {
  display: flex;
  flex-direction: column;
  padding: 14px;
  overflow: hidden;
}
.log-panel h2 { font-size: 14px; margin: 0 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
#log { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }

.entry {
  font-size: 13px;
  padding: 7px 10px;
  border-radius: 6px;
  background: var(--panel);
  border-left: 3px solid var(--border);
  line-height: 1.4;
  word-break: break-word;
}
.entry .who { font-weight: 600; }
.entry.info { border-left-color: var(--accent); }
.entry.plan { border-left-color: var(--purple); }
.entry.assign { border-left-color: var(--orange); }
.entry.task { border-left-color: #2c8cc9; }
.entry.done { border-left-color: var(--green); }
.entry.warn { border-left-color: var(--orange); }
.entry.error { border-left-color: var(--red); }

footer {
  padding: 8px 20px;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}

</style>
  <script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
</head>
<body>
  <header>
    <h1>🕸️ Multi-Agent Coordination <span>on a shared knowledge graph</span></h1>
    <div class="badges">
      <span id="backendBadge" class="badge">backend: …</span>
      <span id="llmBadge" class="badge">llm: …</span>
      <span id="statusBadge" class="badge badge-idle">idle</span>
    </div>
  </header>

  <section class="controls">
    <input id="goal" type="text"
           placeholder="e.g. Launch a marketing campaign for product X" />
    <button id="runBtn" class="btn primary">▶ Run campaign</button>
    <button id="resetBtn" class="btn">↺ Reset</button>
    <button id="fitBtn" class="btn" title="Zoom to fit the whole graph">⤢ Fit</button>
  </section>

  <main>
    <div class="panel graph-panel">
      <div id="graph"></div>
      <div class="legend">
        <div class="lg-group">
          <span class="lg-title">nodes</span>
          <span class="lg"><i class="sh sh-agent"></i>Agent</span>
          <span class="lg"><i class="sh sh-agent-busy"></i>Agent working</span>
          <span class="lg"><i class="sh sh-skill"></i>Skill</span>
          <span class="lg"><i class="sh sh-pending"></i>Task pending</span>
          <span class="lg"><i class="sh sh-active"></i>Task running</span>
          <span class="lg"><i class="sh sh-done"></i>Task done</span>
          <span class="lg"><i class="sh sh-run"></i>Campaign (Run)</span>
        </div>
        <div class="lg-group">
          <span class="lg-title">edges</span>
          <span class="lg"><i class="ln ln-depends"></i>DEPENDS_ON</span>
          <span class="lg"><i class="ln ln-assigned"></i>ASSIGNED_TO → COMPLETED_BY (green = done)</span>
          <span class="lg"><i class="ln ln-skill"></i>HAS_SKILL</span>
          <span class="lg"><i class="ln ln-part"></i>PART_OF</span>
          <span class="lg"><i class="ln ln-coord"></i>COORDINATES</span>
        </div>
        <div class="lg-hint">hover a node for details · drag to re-arrange · older campaigns are faint · glowing agent = working</div>
      </div>
    </div>

    <div class="panel log-panel">
      <h2>Coordination log</h2>
      <div id="log"></div>
    </div>
  </main>

  <footer>FalkorDB · Cypher · multi-agent · Graph Hacks demo</footer>

  <script>
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

</script>
</body>
</html>
"""

app = Flask(__name__)

store = build_store()
llm = LLMClient()
seed_graph(store)

_active = {"run_id": None, "goal": None, "thread": None}
_lock = threading.Lock()


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/state")
def api_state():
    snap = store.snapshot()
    with _lock:
        active_run = _active["run_id"]
    return jsonify({
        "backend": store.backend_name,
        "llm": {"available": llm.available, "model": llm.model,
                "provider": llm.provider},
        "active_run": active_run,
        "agents": store.list_agents(),
        "runs": store.list_runs(),
        "nodes": snap["nodes"],
        "edges": snap["edges"],
        "events": snap["events"],
    })


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(silent=True) or {}
    goal = (data.get("goal") or "").strip()
    if not goal:
        return jsonify({"error": "goal is required"}), 400
    with _lock:
        if _active["thread"] and _active["thread"].is_alive():
            return jsonify({"error": "a campaign is already running"}), 409
        run_id = "run-" + uuid.uuid4().hex[:6]
        _active["run_id"] = run_id
        _active["goal"] = goal
        delay = float(os.getenv("WORKER_DELAY", "0.8"))

        def _work():
            try:
                run_campaign(store, llm, goal, run_id=run_id, delay=delay)
            except Exception as exc:  # noqa: BLE001
                store.add_event(run_id, "Coordinator", f"Error: {exc}", "error")
                store.set_run_status(run_id, "error")

        _active["thread"] = threading.Thread(target=_work, daemon=True)
        _active["thread"].start()
    return jsonify({"run_id": run_id, "goal": goal})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with _lock:
        if _active["thread"] and _active["thread"].is_alive():
            return jsonify({"error": "cannot reset while a campaign is running"}), 409
        store.clear()
        seed_graph(store)
        _active["run_id"] = None
        _active["goal"] = None
    return jsonify({"ok": True})


def _demo(goal: str) -> int:
    print("backend :", store.backend_name)
    print("llm     :", "on (" + llm.provider + "/" + llm.model + ")"
          if llm.available else "off (offline fallback)")
    print("goal    :", goal, "\n")

    run_id = run_campaign(store, llm, goal, delay=0.2)

    print("\n=== Coordination log ===")
    for e in store.snapshot()["events"]:
        if e["run_id"] == run_id:
            print(f"[{e['kind']:>6}] {e['agent']:<12} {e['message']}")

    print("\n=== Final tasks ===")
    for t in sorted(store.list_tasks(), key=lambda x: x["priority"]):
        if t.get("run_id") == run_id:
            who = t.get("completed_by") or t.get("assigned_to") or "-"
            print(f"  {t['id']:<14} {t['status']:<12} {t['title']:<30} by {who}")

    print("\n=== Company brain (past runs) ===")
    for r in store.get_past_runs_summary(current_run_id=run_id):
        print(f"  {r['goal']} -> {', '.join(r['completed_tasks'])}")
    return 0


def main() -> int:
    # make sure Unicode (e.g. LLM output) prints on the Windows console
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]
    if args and args[0] == "demo":
        return _demo(" ".join(args[1:]).strip() or DEFAULT_GOAL)

    port = int(os.getenv("PORT", "5000"))
    print("* backend :", store.backend_name)
    print("* llm     :", "on (" + llm.provider + "/" + llm.model + ")"
          if llm.available else "off (offline fallback)")
    print("* open    : http://localhost:" + str(port))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

