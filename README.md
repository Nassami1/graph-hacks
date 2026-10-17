# 🕸️ Multi-Agent Coordination on a Shared Knowledge Graph

A **Graph Hacks** submission that puts a small team of LLM agents on top of a
real **FalkorDB** graph. A *Coordinator* reads the graph, breaks a goal into
subtasks with dependencies, and hands them to two *Workers* that execute in
parallel — while every decision and result is written back to the graph.

The demo covers all three tracks:

| Track | How it's covered |
|---|---|
| **Agents that act on connected data** | Agents read the graph (team, skills, dependencies) and mutate it (`ASSIGNED_TO`, `COMPLETED_BY`, task status). |
| **Agent memory / coordination** | The whole coordination history lives in the graph: `Run` → `Task` → `Event` nodes, plus `DEPENDS_ON` edges drive a dependency-aware parallel scheduler. |
| **Company brain** | Every campaign is a `Run` node that persists. The Coordinator reads *past campaigns* to inform new plans — accumulated knowledge, not a fresh start each time. |

---

## The idea (in one flow)

```
[User goal]
    ↓
[Coordinator] ──reads graph──▶ (agents + skills + past runs)
    ↓ breaks goal into subtasks, writes Task nodes + DEPENDS_ON edges
[Scheduler] runs every "ready" wave in parallel
    ↓
[Worker A]  ⇄  [FalkorDB graph]  ⇄  [Worker B]
    ↓                                    ↓
[Live graph + coordination log in the browser]
```

**Demo scenario:** *"Launch a marketing campaign for product X"* becomes
`Research → (Content ∥ Design) → Launch`, and you can watch the graph nodes
flip `pending → in_progress → done` in real time.

---

## Quick start

```bash
# 1. install deps (Python 3.10+)
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. (optional) copy config
copy .env.example .env

# 3a. run the CLI demo (no browser needed)
python demo.py "Launch a marketing campaign for product X"

# 3b. or run the web dashboard
python app.py
# open http://localhost:5000
```

### It works with **zero setup**
- **No LLM key?** → agents use a deterministic offline fallback (full demo still runs).
- **No Docker / FalkorDB?** → an in-memory NetworkX graph is used automatically.

### To use the real FalkorDB (for judging)

```bash
# local Docker
docker run -d -p 6379:6379 -p 3000:3000 falkordb/falkordb:latest

# then set in .env
FALKORDB_HOST=localhost
FALKORDB_PORT=6379
```
…or paste a cloud URL as `FALKORDB_URL=falkor://user:pass@host:port`.

> The two backends implement the **same** interface, so the app runs unchanged
> either way. All the real Cypher queries live in `graph_store.py::FalkorStore`.

---

## Graph schema

```cypher
(:Agent  {id, name, role, status})            // Coordinator, Atlas, Nova
(:Task   {id, title, status, priority, skill, run_id, assigned_to, output})
(:Skill  {name})                              // research, content, design, launch
(:Run    {id, goal, status, ts})              // one node per campaign (company brain)
(:Event  {id, agent, kind, message, ts})      // coordination log / memory

(:Agent)-[:HAS_SKILL]->(:Skill)
(:Agent)-[:ASSIGNED_TO]->(:Task)
(:Task)-[:DEPENDS_ON]->(:Task)                // drives parallel scheduling
(:Task)-[:COMPLETED_BY]->(:Agent)
(:Agent)-[:COORDINATES]->(:Agent)
(:Task)-[:PART_OF]->(:Run)
```

---

## Project layout

```
graph_store.py   # GraphStore interface + FalkorDB backend + in-memory backend
llm.py           # LLM client (OpenAI-compatible) + JSON parsing
agents.py        # Coordinator, Worker, and the parallel scheduler
seed.py          # seeds team + skills + one past campaign
app.py           # Flask API + serves the dashboard
demo.py          # CLI demo
static/          # vis-network dashboard (index.html, app.js, style.css)
```

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/state` | full graph snapshot (nodes, edges, events) + metadata |
| `POST` | `/api/run` | start a campaign in the background `{"goal": "..."}` |
| `POST` | `/api/reset` | clear graph and re-seed the demo |

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | auto | `openai` or `openrouter` (auto-detected if unset) |
| `OPENROUTER_API_KEY` | — | use OpenRouter as the LLM provider |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | OpenRouter model (free ones end in `:free`) |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter endpoint |
| `OPENAI_API_KEY` | — | use OpenAI as the LLM provider |
| `OPENAI_BASE_URL` | OpenAI | any OpenAI-compatible endpoint (Ollama, etc.) |
| `LLM_MODEL` | `gpt-4o-mini` | OpenAI model name |
| `FALKORDB_URL` / `FALKORDB_HOST` | — | connect to real FalkorDB |
| `FALKORDB_PORT` | `6379` | FalkorDB port |
| `FALKORDB_USERNAME` / `FALKORDB_PASSWORD` | — | cloud auth |
| `WORKER_DELAY` | `0.8` | seconds per worker task (watch it live) |
