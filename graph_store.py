"""Graph storage layer for the Graph Hacks demo.

Two interchangeable backends implement the *same* domain interface:

  * ``FalkorStore`` -> real FalkorDB (Cypher). Used for the actual submission
    so the judges see genuine graph queries.
  * ``MemoryStore``  -> in-memory NetworkX graph. Lets you run and demo the
    whole pipeline locally without Docker.

``build_store()`` picks FalkorDB when ``FALKORDB_URL`` / ``FALKORDB_HOST`` is
configured and reachable, otherwise it falls back to the in-memory graph.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


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

    def set_run_report(self, run_id: str, report: str) -> None:
        """Attach the final campaign report (markdown) to the Run node."""
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

    def set_run_report(self, run_id, report):
        with self._lock:
            if run_id in self._g:
                self._g.nodes[run_id]["props"]["report"] = report

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

    def set_run_report(self, run_id, report):
        self._q("MATCH (r:Run {id: $id}) SET r.report=$report",
                {"id": run_id, "report": report})

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




