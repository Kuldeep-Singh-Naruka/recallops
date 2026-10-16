# Proves the shared memory stays correct when many agents/users write at the same time.
# Usage: python stress_test.py [workers]
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import memory
from db import db, get_graph
from seed import seed

TENANT = "stresstest"
FIX = "Increase Redis connection pool size"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 50


def fix_count() -> int:
    rows = get_graph(TENANT).query("MATCH (f:Fix {id: $id}) RETURN f.success", {"id": memory._fix_id(FIX)}).result_set
    return rows[0][0] if rows else 0


def naive_confirm() -> None:
    # The WRONG way: read in Python, add 1, write back. Two workers can read the same value.
    g = get_graph(TENANT)
    current = g.query("MATCH (f:Fix {id: $id}) RETURN f.success", {"id": memory._fix_id(FIX)}).result_set[0][0]
    time.sleep(0.01)  # any real app has some delay here (network, logging, ...)
    g.query("MATCH (f:Fix {id: $id}) SET f.success = $v", {"id": memory._fix_id(FIX), "v": current + 1})


def run(label, task, items) -> None:
    before = fix_count()
    start = time.time()
    with ThreadPoolExecutor(max_workers=N) as pool:
        list(pool.map(task, items))
    after = fix_count()
    status = "OK" if after - before == len(items) else "LOST UPDATES"
    print(f"{label:<28} expected +{len(items):<4} got +{after - before:<4} {status}  ({time.time() - start:.2f}s)")


if __name__ == "__main__":
    if TENANT in [g.removeprefix("recallops_") for g in db.list_graphs()]:
        get_graph(TENANT).delete()
    seed(TENANT, with_history=False)

    incidents = [memory.create_incident(TENANT, "redis-cache", f"load test {i}") for i in range(N)]
    memory.resolve_incident(TENANT, incidents[0], FIX)  # make sure the Fix node exists

    print(f"\n{N} workers writing to the same Fix node at the same time:\n")
    run("naive (read, +1, write)", lambda _: naive_confirm(), range(N))
    run("RecallOps resolve_incident", lambda i: memory.resolve_incident(TENANT, i, FIX), incidents[1:])

    shared = incidents[0]
    before = len(memory.get_steps(TENANT, shared))
    with ThreadPoolExecutor(max_workers=N) as pool:
        list(pool.map(lambda n: memory.add_step(TENANT, shared, f"agent-{n % 5}", f"finding {n}"), range(N)))
    added = len(memory.get_steps(TENANT, shared)) - before
    print(f"{'parallel add_step':<28} expected +{N:<4} got +{added:<4} {'OK' if added == N else 'MISSING'}")

    get_graph(TENANT).delete()  # clean up the test tenant
