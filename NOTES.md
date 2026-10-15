# My build notes

## Step 0 — Setup
- Project venv + docker compose for FalkorDB (data kept in a volume).

## Step 1 — Connect + seed
- `db.py` connects, one graph per tenant; `seed.py` adds the service map using `MERGE` to avoid duplicates.

## Step 2 — Memory layer
- `memory.py` reads/writes all memory types (semantic, episodic, procedural, chat); seed now adds past incidents so recall works on day one.
