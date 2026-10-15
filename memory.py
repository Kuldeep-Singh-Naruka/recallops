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
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def resolve_incident(tenant: str, incident_id: str, fix: str, worked: bool = True) -> None:
    get_graph(tenant).query(
        """
        MATCH (i:Incident {id: $id})-[:AFFECTED]->(s:Service)
        MERGE (f:Fix {id: $fix_id})
          ON CREATE SET f.text = $fix, f.success = 0, f.fail = 0
        MERGE (f)-[:FOR]->(s)
        CREATE (i)-[:RESOLVED_BY {worked: $worked}]->(f)
        SET i.status = CASE WHEN $worked THEN 'resolved' ELSE 'open' END,
            f.success = f.success + CASE WHEN $worked THEN 1 ELSE 0 END,
            f.fail = f.fail + CASE WHEN $worked THEN 0 ELSE 1 END
        """,
        {"id": incident_id, "fix_id": _fix_id(fix), "fix": fix, "worked": worked},
    )


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
