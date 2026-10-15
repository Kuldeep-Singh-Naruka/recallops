# Seeds the service map (synthetic data).
# Usage: python seed.py [tenant]
import sys

from db import get_graph, setup_indexes

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


def seed(tenant: str) -> None:
    setup_indexes(tenant)
    graph = get_graph(tenant)

    # MERGE so seed can be re-run without duplicates
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
                {"service": s, "team": t, "oncall": TEAMS[t]}
                for s, t in SERVICES.items()
            ]
        },
    )

    graph.query(
        """
        UNWIND $rows AS row
        MATCH (a:Service {name: row.src}), (b:Service {name: row.dst})
        MERGE (a)-[:DEPENDS_ON]->(b)
        """,
        {"rows": [{"src": a, "dst": b} for a, b in DEPENDS_ON]},
    )

    nodes = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    edges = graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    print(f"Seeded tenant '{tenant}': {nodes} nodes, {edges} relationships")


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "acme")
