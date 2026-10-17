"""Multi-agent coordination on a shared graph.

  * :class:`Coordinator` reads the graph (team, skills, past runs = company
    brain), breaks a goal into subtasks, and creates them in the graph with
    ``DEPENDS_ON`` edges.
  * :class:`Worker` executes a task (via LLM, or deterministically offline)
    and updates the graph (``ASSIGNED_TO`` / ``COMPLETED_BY`` + status).
  * ``run_campaign`` is the scheduler: it runs every "ready" wave of tasks in
    parallel (bounded by the number of workers) and respects dependencies.
"""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

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


