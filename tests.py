# Checks for memory, agents flow and concurrency. No LLM calls (a fake model stands in).
# Uses throwaway tenants t-acme / t-globex and deletes them after. Usage: python tests.py
import os
os.environ.setdefault("GROQ_API_KEY", "x")
from concurrent.futures import ThreadPoolExecutor

import sys

import db, memory, seed, agents
from db import get_graph

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))

def wipe(t):
    try: get_graph(t).delete()
    except Exception: pass

def counts(t):
    g = get_graph(t)
    return (g.query("MATCH (n) RETURN count(n)").result_set[0][0],
            g.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0])

# ---- fake LLM so the agent graph runs without Groq ----
class FakeStructured:
    def __init__(self, schema): self.schema = schema
    def invoke(self, prompt):
        if self.schema is agents.Triage:
            return agents.Triage(severity="P1", summary="Login timeouts.")
        return agents.Investigation(likely_cause="Redis pool exhausted.", past_fix_number=FAKE_PICK[0], new_fix="Do something new.")
FAKE_PICK = [1]
agents.llm = lambda schema: FakeStructured(schema)

for t in ["t-acme", "t-globex"]: wipe(t)

# 1 seed + idempotency
seed.seed("t-acme")
c1 = counts("t-acme")
seed.seed("t-acme")
check("seed is idempotent (re-run adds nothing)", counts("t-acme") == c1, f"{c1} vs {counts('t-acme')}")
check("acme has 14 services", len(memory.list_services("t-acme")) == 14)

# 2 isolation
seed.seed("t-globex", with_history=False, template="streaming")
check("globex has 11 streaming services", len(memory.list_services("t-globex")) == 11)
check("globex has no incident memory", memory.recent_incidents("t-globex") == [] and memory.top_fixes("t-globex") == [])
check("tenants listed", {"t-acme", "t-globex"} <= set(db.list_tenants()))

# 3 recall
rec = memory.recall_similar("t-acme", "auth-service")
check("recall for auth-service ranks pool fix first", rec and rec[0]["fix"] == "Increase Redis connection pool size", str([(r['fix'], r['success'], r['fail']) for r in rec]))
check("recall shows restart as 0 worked / 1 failed", any(r["fix"].startswith("Restart") and r["success"] == 0 and r["fail"] == 1 for r in rec))

# 4 full agent run (fake LLM picks #1)
st = agents.run_incident("t-acme", "auth-service", "login latency above 5s, many timeouts")
iid = st["incident_id"]
steps = memory.get_steps("t-acme", iid)
check("agent run writes 5 steps", len(steps) == 5, str([s["agent"] for s in steps]))
check("fix taken verbatim from memory", st["suggested_fix"] == "Increase Redis connection pool size")
inc = [i for i in memory.recent_incidents("t-acme") if i["id"] == iid][0]
check("status awaiting_confirmation", inc["status"] == "awaiting_confirmation", inc["status"])

before = [f for f in memory.top_fixes("t-acme") if f["fix"].startswith("Increase")][0]["worked"]
memory.resolve_incident("t-acme", iid, inc["proposed_fix"], True)
after = [f for f in memory.top_fixes("t-acme") if f["fix"].startswith("Increase")][0]["worked"]
check("Fix worked bumps counter by 1", after == before + 1, f"{before}->{after}")
nfix = get_graph("t-acme").query("MATCH (f:Fix) RETURN count(f)").result_set[0][0]
check("no duplicate Fix node created", nfix == 5, f"{nfix} Fix nodes")

# 5 double confirm (same incident clicked twice / two engineers)
memory.resolve_incident("t-acme", iid, inc["proposed_fix"], True)
again = [f for f in memory.top_fixes("t-acme") if f["fix"].startswith("Increase")][0]["worked"]
check("re-confirming a resolved incident is ignored", again == after, f"{after}->{again}")

# 6 two engineers click at the same moment on a fresh incident
st2 = agents.run_incident("t-acme", "auth-service", "login timeouts again")
b = [f for f in memory.top_fixes("t-acme") if f["fix"].startswith("Increase")][0]["worked"]
with ThreadPoolExecutor(10) as p:
    list(p.map(lambda _: memory.resolve_incident("t-acme", st2["incident_id"], st2["suggested_fix"], True), range(10)))
a = [f for f in memory.top_fixes("t-acme") if f["fix"].startswith("Increase")][0]["worked"]
check("10 concurrent confirms of ONE incident count once", a == b + 1, f"+{a-b}")

# 7 failed path -> retry loop -> escalation
FAKE_PICK[0] = 1
st3 = agents.run_incident("t-acme", "auth-service", "login timeouts, pool full")
first = st3["suggested_fix"]
memory.resolve_incident("t-acme", st3["incident_id"], first, False)
agents.retry_incident("t-acme", st3["incident_id"])
inc3 = memory.get_incident("t-acme", st3["incident_id"])
prop = [i for i in memory.recent_incidents("t-acme", 50) if i["id"] == st3["incident_id"]][0]["proposed_fix"]
check("Didn't work -> agents propose a DIFFERENT fix", inc3["status"] == "awaiting_confirmation" and prop != first, f"{first!r} -> {prop!r}")
for _ in range(3):
    cur = [i for i in memory.recent_incidents("t-acme", 50) if i["id"] == st3["incident_id"]][0]
    if cur["status"] != "awaiting_confirmation": break
    FAKE_PICK[0] = 0
    memory.resolve_incident("t-acme", st3["incident_id"], cur["proposed_fix"] + f" v{_}", False)
    agents.retry_incident("t-acme", st3["incident_id"])
check("3 failed fixes -> escalated, no endless loop", memory.get_incident("t-acme", st3["incident_id"])["status"] == "escalated", memory.get_incident("t-acme", st3["incident_id"])["status"])
FAKE_PICK[0] = 1

# 8 investigator picks a fix that only ever failed
rec = memory.recall_similar("t-acme", "auth-service")
bad_n = [n for n, r in enumerate(rec, 1) if r["success"] == 0][0]
FAKE_PICK[0] = bad_n
st4 = agents.run_incident("t-acme", "auth-service", "login timeouts")
check("code refuses a never-worked fix even if LLM picks it", not st4["suggested_fix"].startswith("Restart"), st4["suggested_fix"])
memory.resolve_incident("t-acme", st4["incident_id"], st4["suggested_fix"], True)
FAKE_PICK[0] = 1

# 9 cross-incident correlation (coordination)
up = memory.create_incident("t-acme", "redis-cache", "redis OOM")
st5 = agents.run_incident("t-acme", "auth-service", "login timeouts")
dep = [s for s in memory.get_steps("t-acme", st5["incident_id"]) if s["agent"] == "dependency_scout"][0]["text"]
check("dependency scout links open upstream redis incident", up in dep, dep[:120])
memory.resolve_incident("t-acme", st5["incident_id"], st5["suggested_fix"], True)

# 10 parallel scouts: agent nodes not duplicated
na = get_graph("t-acme").query("MATCH (a:Agent) RETURN a.name, count(*) ").result_set
check("one Agent node per agent name", all(r[1] == 1 for r in na), str(na))

# 11 'new tenant' with an existing name

# 12 weird tenant names
for name, want in [("t-globex", True), ("globex-2", True), ("Globex Corp", False), ("a/b", False), ("ünï", False), ("", False), ("x", False)]:
    check(f"tenant name {name!r} -> {want}", db.valid_tenant_name(name) == want)

# 13 injection-ish text is stored literally
evil = "'}) MATCH (n) DETACH DELETE n //"
i6 = memory.create_incident("t-acme", "auth-service", evil)
check("Cypher-looking symptom stored as plain data", any(i["symptom"] == evil for i in memory.recent_incidents("t-acme", 50)) and counts("t-acme")[0] > 10)

# 14 chat history order + isolation
for k in range(6):
    memory.save_message("t-acme", "s1", "user" if k % 2 == 0 else "assistant", f"m{k}")
h = [m["text"] for m in memory.get_history("t-acme", "s1")]
check("chat history in order even with same-ms writes", h == [f"m{k}" for k in range(6)], str(h))
check("chat session isolated per tenant", memory.get_history("t-globex", "s1") == [])

# 15 graph views
for v in memory.GRAPH_VIEWS:
    n, e = memory.subgraph("t-acme", v, iid)
    check(f"graph view '{v}' renders", len(n) > 0, f"{len(n)} nodes / {len(e)} edges")
n, e = memory.subgraph("t-globex", "Incident memory")
check("empty Incident memory view returns nothing, no crash", n == [])

# 16 fix-id normalisation
check("fix id ignores case/punctuation", memory._fix_id("Increase Redis connection pool size.") == memory._fix_id("increase redis  connection pool size"))
l1 = "Increase the connection pool size of the shared redis cache cluster to 200 for auth"
l2 = "Increase the connection pool size of the shared redis cache cluster to 200 for payments"
check("long different fixes do not collide", memory._fix_id(l1) != memory._fix_id(l2), memory._fix_id(l1))

print(f"\n{sum(r[1] for r in results)}/{len(results)} passed")
for t in ["t-acme", "t-globex"]: wipe(t)
sys.exit(0 if all(r[1] for r in results) else 1)
