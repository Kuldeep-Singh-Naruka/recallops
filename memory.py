import hashlib
import re
import time
import uuid

from db import get_graph


def _now() -> int:
    return int(time.time() * 1000)


def _rows(result) -> list[dict]:
    names = [h[1] for h in result.header]
    return [dict(zip(names, row)) for row in result.result_set]


# ---------- semantic ----------

def service_context(tenant: str, service: str) -> dict:
    g = get_graph(tenant)
    owner = _rows(g.query(
        """
        MATCH (t:Team)-[:OWNS]->(s:Service {name: $svc})
        RETURN t.name AS team, t.oncall AS oncall
        """,
        {"svc": service},
    ))
    upstream = g.query(
        """
        MATCH (:Service {name: $svc})-[:DEPENDS_ON*1..3]->(d:Service)
        RETURN DISTINCT d.name
        """,
        {"svc": service},
    ).result_set
    downstream = g.query(
        """
        MATCH (u:Service)-[:DEPENDS_ON*1..3]->(:Service {name: $svc})
        RETURN DISTINCT u.name
        """,
        {"svc": service},
    ).result_set
    return {
        "service": service,
        "owner": owner[0] if owner else None,
        "depends_on": [r[0] for r in upstream],
        "impacts": [r[0] for r in downstream],
    }


# ---------- episodic ----------

def create_incident(tenant: str, service: str, symptom: str) -> str:
    incident_id = "INC-" + uuid.uuid4().hex[:6]
    get_graph(tenant).query(
        """
        MATCH (s:Service {name: $svc})
        CREATE (i:Incident {id: $id, symptom: $symptom, status: 'open',
                            created_at: $ts})-[:AFFECTED]->(s)
        """,
        {"svc": service, "id": incident_id, "symptom": symptom, "ts": _now()},
    )
    return incident_id


def add_step(tenant: str, incident_id: str, agent: str, text: str) -> None:
    get_graph(tenant).query(
        """
        MATCH (i:Incident {id: $id})
        MERGE (a:Agent {name: $agent})
        CREATE (st:Step {agent: $agent, text: $text, ts: $ts})
        CREATE (i)-[:HAS_STEP]->(st)
        CREATE (a)-[:WROTE]->(st)
        SET i.status = $agent
        """,
        {"id": incident_id, "agent": agent, "text": text, "ts": _now()},
    )


def get_steps(tenant: str, incident_id: str) -> list[dict]:
    return _rows(get_graph(tenant).query(
        """
        MATCH (:Incident {id: $id})-[:HAS_STEP]->(st:Step)
        RETURN st.agent AS agent, st.text AS text
        ORDER BY st.ts
        """,
        {"id": incident_id},
    ))


# ---------- procedural ----------

def _fix_id(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) <= 60:
        return slug
    # long fixes keep a hash so two different ones never share a prefix id
    return slug[:51] + "-" + hashlib.sha1(slug.encode()).hexdigest()[:8]


def resolve_incident(tenant: str, incident_id: str, fix: str, worked: bool = True) -> bool:
    # WHERE + SET in one query = exactly-once: a second click (or a second engineer) matches nothing
    result = get_graph(tenant).query(
        """
        MATCH (i:Incident {id: $id})-[:AFFECTED]->(s:Service)
        WHERE i.status = 'awaiting_confirmation'
        MERGE (f:Fix {id: $fix_id})
          ON CREATE SET f.text = $fix, f.success = 0, f.fail = 0
        MERGE (f)-[:FOR]->(s)
        CREATE (i)-[:RESOLVED_BY {worked: $worked}]->(f)
        SET i.status = CASE WHEN $worked THEN 'resolved' ELSE 'retrying' END,
            f.success = f.success + CASE WHEN $worked THEN 1 ELSE 0 END,
            f.fail = f.fail + CASE WHEN $worked THEN 0 ELSE 1 END
        RETURN count(i)
        """,
        {"id": incident_id, "fix_id": _fix_id(fix), "fix": fix, "worked": worked},
    )
    return result.result_set[0][0] > 0


def tried_fixes(tenant: str, incident_id: str) -> list[str]:
    rows = get_graph(tenant).query(
        "MATCH (:Incident {id: $id})-[:RESOLVED_BY {worked: false}]->(f:Fix) RETURN f.text",
        {"id": incident_id},
    ).result_set
    return [r[0] for r in rows]


def get_incident(tenant: str, incident_id: str) -> dict | None:
    rows = _rows(get_graph(tenant).query(
        """
        MATCH (i:Incident {id: $id})-[:AFFECTED]->(s:Service)
        RETURN i.id AS id, s.name AS service, i.symptom AS symptom, i.status AS status
        """,
        {"id": incident_id},
    ))
    return rows[0] if rows else None


def set_status(tenant: str, incident_id: str, status: str) -> None:
    get_graph(tenant).query("MATCH (i:Incident {id: $id}) SET i.status = $st", {"id": incident_id, "st": status})


def service_map(tenant: str) -> dict[str, list[str]]:
    rows = get_graph(tenant).query(
        "MATCH (a:Service)-[:DEPENDS_ON]->(b:Service) RETURN a.name, collect(b.name)"
    ).result_set
    return {a: deps for a, deps in rows}


def recall_similar(tenant: str, service: str) -> list[dict]:
    return _rows(get_graph(tenant).query(
        """
        MATCH (:Service {name: $svc})-[:DEPENDS_ON*0..3]->(d:Service)
              <-[:AFFECTED]-(i:Incident)-[:RESOLVED_BY]->(f:Fix)
        RETURN f.text AS fix, f.success AS success, f.fail AS fail,
               collect(DISTINCT d.name) AS services,
               collect(DISTINCT i.symptom) AS symptoms
        ORDER BY success - fail DESC
        LIMIT 5
        """,
        {"svc": service},
    ))


# ---------- chat ----------

def save_message(tenant: str, session_id: str, role: str, text: str) -> None:
    get_graph(tenant).query(
        """
        MERGE (s:Session {id: $sid})
        CREATE (m:Message {role: $role, text: $text, ts: $ts})
        CREATE (s)-[:HAS_MESSAGE]->(m)
        """,
        {"sid": session_id, "role": role, "text": text, "ts": _now()},
    )


def get_history(tenant: str, session_id: str, limit: int = 20) -> list[dict]:
    rows = _rows(get_graph(tenant).query(
        """
        MATCH (:Session {id: $sid})-[:HAS_MESSAGE]->(m:Message)
        RETURN m.role AS role, m.text AS text
        ORDER BY m.ts DESC, id(m) DESC
        LIMIT $limit
        """,
        {"sid": session_id, "limit": limit},
    ))
    return list(reversed(rows))


# ---------- for the UI ----------

def list_services(tenant: str) -> list[str]:
    rows = get_graph(tenant).query("MATCH (s:Service) RETURN s.name ORDER BY s.name").result_set
    return [r[0] for r in rows]


def propose_fix(tenant: str, incident_id: str, fix: str) -> None:
    get_graph(tenant).query(
        """
        MATCH (i:Incident {id: $id})
        SET i.proposed_fix = $fix, i.status = 'awaiting_confirmation'
        """,
        {"id": incident_id, "fix": fix},
    )


def recent_incidents(tenant: str, limit: int = 10) -> list[dict]:
    return _rows(get_graph(tenant).query(
        """
        MATCH (i:Incident)-[:AFFECTED]->(s:Service)
        OPTIONAL MATCH (i)-[r:RESOLVED_BY]->(f:Fix)
        RETURN i.id AS id, s.name AS service, i.symptom AS symptom, i.status AS status,
               i.proposed_fix AS proposed_fix, i.created_at AS created_at,
               collect({fix: f.text, worked: r.worked}) AS attempts
        ORDER BY created_at DESC
        LIMIT $limit
        """,
        {"limit": limit},
    ))


def top_fixes(tenant: str) -> list[dict]:
    return _rows(get_graph(tenant).query(
        """
        MATCH (f:Fix)-[:FOR]->(s:Service)
        RETURN f.text AS fix, collect(DISTINCT s.name) AS services,
               f.success AS worked, f.fail AS failed
        ORDER BY worked - failed DESC
        """
    ))


GRAPH_VIEWS = {
    "Service map": "MATCH p=()-[:OWNS|DEPENDS_ON]->() RETURN p",
    "Incident memory": "MATCH p=(:Incident)-[:AFFECTED|RESOLVED_BY]->() RETURN p",
    "One incident": "MATCH p=(:Incident {id: $id})-[*1..2]-() RETURN p",
}


def subgraph(tenant: str, view: str, incident_id: str = "") -> tuple[list[dict], list[dict]]:
    nodes, edges = {}, {}
    result = get_graph(tenant).query(GRAPH_VIEWS[view], {"id": incident_id})
    for (path,) in result.result_set:
        for n in path.nodes():
            p = n.properties
            label = n.labels[0]
            name = p.get("name") or p.get("text") or p.get("id") or label
            if label == "Step":
                name = p["agent"] + ": " + p["text"][:40]
            nodes[n.id] = {"id": n.id, "label": str(name)[:45], "group": label}
        for e in path.edges():
            edges[e.id] = {"from": e.src_node, "to": e.dest_node, "label": e.relation}
    return list(nodes.values()), list(edges.values())


def upstream_open_incidents(tenant: str, service: str, within_ms: int = 2 * 60 * 60 * 1000) -> list[dict]:
    return _rows(get_graph(tenant).query(
        """
        MATCH (:Service {name: $svc})-[:DEPENDS_ON*1..3]->(d:Service)<-[:AFFECTED]-(i:Incident)
        WHERE i.status <> 'resolved' AND i.created_at > $since
        RETURN DISTINCT d.name AS service, i.id AS incident, i.symptom AS symptom
        """,
        {"svc": service, "since": _now() - within_ms},
    ))
