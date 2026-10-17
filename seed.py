"""Seed the demo graph: team, skills, and one past campaign (company brain).

Idempotent — calling it again on an already-seeded store does nothing, so it
is safe to run at app startup and from the CLI.
"""
from __future__ import annotations

import time


def seed(store) -> None:
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
