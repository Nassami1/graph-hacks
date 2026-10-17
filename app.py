"""Flask app: serves the visualizer and a small JSON API.

Endpoints
---------
GET  /             -> the vis-network dashboard (static/index.html)
GET  /api/state    -> full graph snapshot + metadata
POST /api/run      -> start a campaign in the background  {goal: "..."}
POST /api/reset    -> clear the graph and re-seed the demo
"""
from __future__ import annotations

import os
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

import agents
import seed
from graph_store import build_store
from llm import LLMClient

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="/static")

store = build_store()
llm = LLMClient()
seed.seed(store)

_active: dict = {"run_id": None, "goal": None, "thread": None}
_lock = threading.Lock()


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/state")
def api_state():
    snap = store.snapshot()
    with _lock:
        active_run = _active["run_id"]
    agents = store.list_agents()
    return jsonify({
        "backend": store.backend_name,
        "llm": {"available": llm.available, "model": llm.model,
                "provider": llm.provider},
        "active_run": active_run,
        "agents": agents,
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
        run_id = f"run-{__import__('uuid').uuid4().hex[:6]}"
        _active["run_id"] = run_id
        _active["goal"] = goal

        delay = float(os.getenv("WORKER_DELAY", "0.8"))

        def _work():
            try:
                agents.run_campaign(store, llm, goal, run_id=run_id, delay=delay)
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
        seed.seed(store)
        _active["run_id"] = None
        _active["goal"] = None
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print(f"* backend : {store.backend_name}")
    print(f"* llm     : {'on (' + llm.provider + '/' + llm.model + ')' if llm.available else 'off (offline fallback)'}")
    print(f"* open    : http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
