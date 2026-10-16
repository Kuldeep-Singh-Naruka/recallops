# My build notes

## Step 0 — Setup
- Project venv + docker compose for FalkorDB (data kept in a volume).

## Step 1 — Connect + seed
- `db.py` connects, one graph per tenant; `seed.py` adds the service map using `MERGE` to avoid duplicates.

## Step 2 — Memory layer
- `memory.py` reads/writes all memory types (semantic, episodic, procedural, chat); seed now adds past incidents so recall works on day one.

## Step 3 — Agents
- `agents.py`: LangGraph flow triage -> investigator -> resolver; agents hand off through steps saved in the graph. LLM picks a past fix by number, code saves the exact text so the fix success count grows.

## Step 4 — UI
- `app.py` (Streamlit): run agents live, confirm/reject the fix (updates memory), memory tables, chat that keeps history in the graph, graph view, tenant switch.

## Step 5 — Concurrency
- Two scouts (dependency + memory) run in parallel after triage and write to the same incident; investigator waits for both. `stress_test.py` shows 50 parallel writes stay correct because counters are updated inside FalkorDB, not read-modify-write in Python.
- Added a second architecture template (streaming) so a new tenant gets its own service map, not a copy of acme.
