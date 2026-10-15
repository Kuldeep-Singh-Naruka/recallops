# My build notes

Short notes after each step — what I built and why.

## Step 0 — Setup
- Created a virtual env (`.venv`) for the project so packages stay local, not global.
- Created a docker compose file to run FalkorDB (6379 = database, 3000 = Browser UI). Data is kept in a volume so memory survives restarts.