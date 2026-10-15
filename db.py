import os

from dotenv import load_dotenv
from falkordb import FalkorDB

load_dotenv()

db = FalkorDB(
    host=os.getenv("FALKORDB_HOST", "localhost"),
    port=int(os.getenv("FALKORDB_PORT", "6379")),
    username=os.getenv("FALKORDB_USERNAME") or None,
    password=os.getenv("FALKORDB_PASSWORD") or None,
)

GRAPH_PREFIX = "recallops_"


def get_graph(tenant: str):
    return db.select_graph(GRAPH_PREFIX + tenant)


def list_tenants() -> list[str]:
    return [
        name.removeprefix(GRAPH_PREFIX)
        for name in db.list_graphs()
        if name.startswith(GRAPH_PREFIX)
    ]


INDEXES = [
    ("Service", "name"),
    ("Team", "name"),
    ("Incident", "id"),
    ("Fix", "id"),
    ("Session", "id"),
]


def setup_indexes(tenant: str) -> None:
    graph = get_graph(tenant)
    for label, prop in INDEXES:
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except Exception as e:
            # fine if the index already exists
            if "already indexed" not in str(e):
                raise
