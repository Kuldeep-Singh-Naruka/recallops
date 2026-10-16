import sys

from db import get_graph, setup_indexes
from memory import create_incident, propose_fix, resolve_incident

# team -> on-call person
TEAMS = {
    "frontend": "@neha",
    "platform": "@arjun",
    "identity": "@riya",
    "payments": "@vikram",
    "commerce": "@sana",
    "catalog": "@dev",
}

# service -> owning team
SERVICES = {
    "web-frontend": "frontend",
    "api-gateway": "platform",
    "redis-cache": "platform",
    "kafka": "platform",
    "auth-service": "identity",
    "users-db": "identity",
    "payment-service": "payments",
    "payments-db": "payments",
    "order-service": "commerce",
    "inventory-service": "commerce",
    "orders-db": "commerce",
    "product-service": "catalog",
    "search-service": "catalog",
    "products-db": "catalog",
}

# (service, depends on)
DEPENDS_ON = [
    ("web-frontend", "api-gateway"),
    ("api-gateway", "auth-service"),
    ("api-gateway", "order-service"),
    ("api-gateway", "product-service"),
    ("auth-service", "redis-cache"),
    ("auth-service", "users-db"),
    ("order-service", "payment-service"),
    ("order-service", "inventory-service"),
    ("order-service", "orders-db"),
    ("order-service", "kafka"),
    ("payment-service", "redis-cache"),
    ("payment-service", "payments-db"),
    ("product-service", "search-service"),
    ("product-service", "products-db"),
    ("inventory-service", "orders-db"),
]

# Second architecture (a video streaming company), so tenants can look different
STREAMING_TEAMS = {
    "playback": "@kabir",
    "content": "@meera",
    "discovery": "@tanvi",
    "infra": "@rohan",
}

STREAMING_SERVICES = {
    "web-player": "playback",
    "playback-api": "playback",
    "stream-router": "playback",
    "user-profile": "playback",
    "transcoder": "content",
    "content-catalog": "content",
    "recommendation-engine": "discovery",
    "cdn-edge": "infra",
    "object-storage": "infra",
    "postgres-main": "infra",
    "memcached": "infra",
}

STREAMING_DEPENDS_ON = [
    ("web-player", "playback-api"),
    ("web-player", "cdn-edge"),
    ("web-player", "recommendation-engine"),
    ("playback-api", "stream-router"),
    ("playback-api", "user-profile"),
    ("playback-api", "content-catalog"),
    ("stream-router", "cdn-edge"),
    ("cdn-edge", "object-storage"),
    ("transcoder", "object-storage"),
    ("content-catalog", "postgres-main"),
    ("user-profile", "postgres-main"),
    ("recommendation-engine", "content-catalog"),
    ("recommendation-engine", "memcached"),
]

TEMPLATES = {
    "ecommerce": (TEAMS, SERVICES, DEPENDS_ON),
    "streaming": (STREAMING_TEAMS, STREAMING_SERVICES, STREAMING_DEPENDS_ON),
}

# (service, symptom, fix, worked)
PAST_INCIDENTS = [
    ("redis-cache", "auth latency spike, connection pool exhausted", "Increase Redis connection pool size", True),
    ("redis-cache", "login timeouts during sale traffic", "Restart redis-cache pods", False),
    ("redis-cache", "login timeouts during sale traffic", "Increase Redis connection pool size", True),
    ("payments-db", "slow queries, payment timeouts", "Add index on payments.created_at", True),
    ("kafka", "consumer lag, order events delayed", "Scale order consumers to 6 replicas", True),
    ("search-service", "500 errors right after deploy", "Roll back search-service to previous version", True),
]


def seed_history(tenant: str) -> None:
    graph = get_graph(tenant)
    if graph.query("MATCH (i:Incident) RETURN count(i)").result_set[0][0] > 0:
        return
    for service, symptom, fix, worked in PAST_INCIDENTS:
        incident_id = create_incident(tenant, service, symptom)
        propose_fix(tenant, incident_id, fix)
        resolve_incident(tenant, incident_id, fix, worked)
    graph.query(
        # history is closed: the failed one was handed to humans back then
        "MATCH (i:Incident) SET i.created_at = i.created_at - $week, "
        "i.status = CASE WHEN i.status = 'retrying' THEN 'escalated' ELSE i.status END",
        {"week": 7 * 24 * 3600 * 1000},
    )


def seed(tenant: str, with_history: bool = True, template: str = "ecommerce") -> None:
    teams, services, depends_on = TEMPLATES[template]
    setup_indexes(tenant)
    graph = get_graph(tenant)

    graph.query(
        """
        UNWIND $rows AS row
        MERGE (t:Team {name: row.team})
        SET t.oncall = row.oncall
        MERGE (s:Service {name: row.service})
        MERGE (t)-[:OWNS]->(s)
        """,
        {
            "rows": [
                {"service": s, "team": t, "oncall": teams[t]}
                for s, t in services.items()
            ]
        },
    )

    graph.query(
        """
        UNWIND $rows AS row
        MATCH (a:Service {name: row.src}), (b:Service {name: row.dst})
        MERGE (a)-[:DEPENDS_ON]->(b)
        """,
        {"rows": [{"src": a, "dst": b} for a, b in depends_on]},
    )

    # past incidents only exist for the ecommerce services
    if with_history and template == "ecommerce":
        seed_history(tenant)

    nodes = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    edges = graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    print(f"Seeded tenant '{tenant}': {nodes} nodes, {edges} relationships")


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "acme")
