"""CLI demo — run a full campaign and print the coordination log + graph.

Usage:
    python demo.py "Launch a marketing campaign for product X"
    python demo.py                          # uses the default goal
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

import agents
import seed
from graph_store import build_store
from llm import LLMClient

load_dotenv()


def main() -> int:
    # make sure Unicode (e.g. LLM output) prints on the Windows console
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    goal = " ".join(sys.argv[1:]).strip() or agents.DEFAULT_GOAL
    store = build_store()
    llm = LLMClient()
    seed.seed(store)

    print(f"backend : {store.backend_name}")
    print(f"llm     : {'on (' + llm.provider + '/' + llm.model + ')' if llm.available else 'off (offline fallback)'}")
    print(f"goal    : {goal}\n")

    run_id = agents.run_campaign(store, llm, goal, delay=0.2)

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


if __name__ == "__main__":
    raise SystemExit(main())
