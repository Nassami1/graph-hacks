"""Build script: merge the whole project into a single self-contained file.

Usage:
    python build_onefile.py

Output:
    graph_hacks.py  -> everything (graph store, agents, LLM client, Flask app,
                       and the inlined frontend) in ONE file.

Run the result:
    python graph_hacks.py                        # serve dashboard on :5000
    python graph_hacks.py demo "Campaign goal"   # CLI demo
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).parent

HEADER = '''"""Graph Hacks — Multi-Agent Coordination on a Shared Knowledge Graph
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

'''


def strip_module(path: pathlib.Path) -> str:
    """Drop module docstring + top-level imports; keep everything else."""
    text = path.read_text(encoding="utf-8")
    # remove the leading module docstring
    text = re.sub(r'^"""[\\s\\S]*?"""\\s*\\n', "", text, count=1)
    kept: list[str] = []
    for ln in text.split("\n"):
        if ln.startswith("import ") or ln.startswith("from "):
            continue  # top-level import (indented lazy imports are kept)
        if ln.strip() == "load_dotenv()":
            continue
        kept.append(ln)
    return "\n".join(kept).strip("\n")


def build_index_html() -> str:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    html = html.replace(
        '<link rel="stylesheet" href="/static/style.css" />',
        "<style>\n" + css + "\n</style>",
    )
    html = html.replace(
        '<script src="/static/app.js"></script>',
        "<script>\n" + js + "\n</script>",
    )
    # safety: a triple-double-quote inside the HTML would break the embedding
    return html.replace('"""', '"\\"\\""')


APP_MAIN = '''

# --------------------------------------------------------------------------- #
# Flask app (frontend is inlined -> truly a single file)
# --------------------------------------------------------------------------- #
INDEX_HTML = """__INDEX_HTML__"""

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
    print("goal    :", goal, "\\n")

    run_id = run_campaign(store, llm, goal, delay=0.2)

    print("\\n=== Coordination log ===")
    for e in store.snapshot()["events"]:
        if e["run_id"] == run_id:
            print(f"[{e['kind']:>6}] {e['agent']:<12} {e['message']}")

    print("\\n=== Final tasks ===")
    for t in sorted(store.list_tasks(), key=lambda x: x["priority"]):
        if t.get("run_id") == run_id:
            who = t.get("completed_by") or t.get("assigned_to") or "-"
            print(f"  {t['id']:<14} {t['status']:<12} {t['title']:<30} by {who}")

    print("\\n=== Company brain (past runs) ===")
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
'''


def main() -> None:
    parts = [
        HEADER,
        strip_module(ROOT / "llm.py"),
        strip_module(ROOT / "graph_store.py"),
        strip_module(ROOT / "agents.py"),
        strip_module(ROOT / "seed.py").replace("def seed(store)", "def seed_graph(store)"),
        APP_MAIN.replace("__INDEX_HTML__", build_index_html()),
    ]
    out = ROOT / "graph_hacks.py"
    out.write_text("\n\n".join(p for p in parts if p) + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

