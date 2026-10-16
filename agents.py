import os
import sys
from typing import Literal, TypedDict

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

import memory

load_dotenv()
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def llm(schema):
    model = ChatGroq(model=MODEL, temperature=0, max_tokens=2048, reasoning_effort="low")
    strict = MODEL.startswith("openai/gpt-oss")
    structured = model.with_structured_output(schema, method="json_schema", strict=strict)
    # One retry covers the occasional malformed reply
    return structured.with_retry(stop_after_attempt=2)


class Triage(BaseModel):
    severity: Literal["P1", "P2", "P3"]
    summary: str


class Investigation(BaseModel):
    likely_cause: str
    past_fix_number: int
    new_fix: str


class IncidentState(TypedDict, total=False):
    tenant: str
    service: str
    symptom: str
    incident_id: str
    suggested_fix: str


def _steps_text(tenant: str, incident_id: str) -> str:
    return "\n".join(f"- [{s['agent']}] {s['text']}" for s in memory.get_steps(tenant, incident_id))


def triage(state: IncidentState) -> IncidentState:
    tenant, service = state["tenant"], state["service"]
    incident_id = memory.create_incident(tenant, service, state["symptom"])
    ctx = memory.service_context(tenant, service)

    out = llm(Triage).invoke(
        f"""You are the triage agent for a production incident.
Service: {service}
Symptom: {state['symptom']}
Owner: {ctx['owner']}
Depends on: {ctx['depends_on']}
Breaks if it is down: {ctx['impacts']}

Severity: P1 = customer-facing outage, P2 = degraded, P3 = minor.
Give the severity and a one-line summary."""
    )

    impacts = ", ".join(ctx["impacts"]) or "nothing upstream"
    memory.add_step(tenant, incident_id, "triage", f"{out.severity}: {out.summary} Impacts: {impacts}.")
    return {"incident_id": incident_id}


def dependency_scout(state: IncidentState) -> IncidentState:
    tenant, service, incident_id = state["tenant"], state["service"], state["incident_id"]
    open_upstream = [u for u in memory.upstream_open_incidents(tenant, service) if u["incident"] != incident_id]
    if open_upstream:
        found = "; ".join(f"{u['service']} has open {u['incident']} ({u['symptom']})" for u in open_upstream)
        text = f"Upstream problem found: {found}"
    else:
        deps = memory.service_context(tenant, service)["depends_on"]
        text = f"No open incidents upstream. Checked: {', '.join(deps) or 'no dependencies'}"
    memory.add_step(tenant, incident_id, "dependency_scout", text)
    return {}


def memory_scout(state: IncidentState) -> IncidentState:
    tenant, service, incident_id = state["tenant"], state["service"], state["incident_id"]
    past = memory.recall_similar(tenant, service)
    if past:
        best = ", ".join(f"{p['fix']} ({p['success']} worked / {p['fail']} failed)" for p in past[:3])
        text = f"Past fixes nearby: {best}"
    else:
        text = "No past fixes for this service or its dependencies"
    memory.add_step(tenant, incident_id, "memory_scout", text)
    return {}


MAX_ATTEMPTS = 3


def investigator(state: IncidentState) -> IncidentState:
    tenant, service, incident_id = state["tenant"], state["service"], state["incident_id"]
    ctx = memory.service_context(tenant, service)
    tried = memory.tried_fixes(tenant, incident_id)
    # a fix that already failed on THIS incident is never offered again
    past = [p for p in memory.recall_similar(tenant, service) if p["fix"] not in tried]
    past_text = "\n".join(
        f"{n}. {p['fix']} | worked {p['success']}x, failed {p['fail']}x | on {p['services']} | symptoms: {p['symptoms']}"
        for n, p in enumerate(past, start=1)
    ) or "none"

    out = llm(Investigation).invoke(
        f"""You are the investigator agent. Find the likely root cause.
Service: {service}
Symptom: {state['symptom']}
It depends on: {ctx['depends_on']}

What other agents found so far:
{_steps_text(tenant, incident_id)}

Already tried on this incident and FAILED: {tried or 'nothing yet'}

Fixes from past incidents on this service or its dependencies:
{past_text}

If a past fix that worked matches the symptoms, set past_fix_number to its number.
Never pick a fix that only failed. Otherwise set past_fix_number to 0 and write new_fix.
Keep likely_cause and new_fix to one short sentence each."""
    )

    picked = past[out.past_fix_number - 1] if 1 <= out.past_fix_number <= len(past) else None
    if picked and picked["success"] > 0:
        fix = picked["fix"]
        note = " (from memory)"
    else:
        # code enforces the rule too: a fix with zero wins is not trusted from memory
        fix = out.new_fix.strip() or "Escalate to the owning team for manual investigation"
        note = ""
    memory.add_step(
        tenant, incident_id, "investigator",
        f"Likely cause: {out.likely_cause.rstrip('.')}. Suggested fix{note}: {fix}",
    )
    return {"suggested_fix": fix}


def resolver(state: IncidentState) -> IncidentState:
    # No LLM here: the resolver takes the actions (simulated) and waits for a human.
    tenant, service, incident_id = state["tenant"], state["service"], state["incident_id"]
    owner = memory.service_context(tenant, service)["owner"] or {}
    tried = memory.tried_fixes(tenant, incident_id)
    if len(tried) >= MAX_ATTEMPTS:
        memory.add_step(
            tenant, incident_id, "resolver",
            f"{len(tried)} fixes failed. Escalated to {owner.get('oncall', 'on-call')} "
            f"({owner.get('team', 'unknown team')}) for manual handling.",
        )
        memory.set_status(tenant, incident_id, "escalated")
        return {}
    memory.add_step(
        tenant, incident_id, "resolver",
        f"Paged {owner.get('oncall', 'on-call')} ({owner.get('team', 'unknown team')}). "
        f"Proposed fix: {state['suggested_fix'].rstrip('.')}. Waiting for confirmation.",
    )
    memory.propose_fix(tenant, incident_id, state["suggested_fix"])
    return {}


builder = StateGraph(IncidentState)
builder.add_node("triage", triage)
builder.add_node("dependency_scout", dependency_scout)
builder.add_node("memory_scout", memory_scout)
builder.add_node("investigator", investigator)
builder.add_node("resolver", resolver)

builder.add_edge(START, "triage")
builder.add_edge("triage", "dependency_scout")
builder.add_edge("triage", "memory_scout")
builder.add_edge(["dependency_scout", "memory_scout"], "investigator")
builder.add_edge("investigator", "resolver")
builder.add_edge("resolver", END)

incident_graph = builder.compile()

# After "Didn't work": same incident, investigator looks again with the failed fix excluded
retry_builder = StateGraph(IncidentState)
retry_builder.add_node("investigator", investigator)
retry_builder.add_node("resolver", resolver)
retry_builder.add_edge(START, "investigator")
retry_builder.add_edge("investigator", "resolver")
retry_builder.add_edge("resolver", END)
retry_graph = retry_builder.compile()


def retry_incident(tenant: str, incident_id: str) -> IncidentState:
    inc = memory.get_incident(tenant, incident_id)
    return retry_graph.invoke({"tenant": tenant, "service": inc["service"],
                               "symptom": inc["symptom"], "incident_id": incident_id})


def answer_question(tenant: str, session_id: str, question: str) -> str:
    history = memory.get_history(tenant, session_id)
    incidents = "\n".join(
        f"- {i['id']} {i['service']}: {i['symptom']} | status {i['status']} | "
        f"tried: {[a for a in i['attempts'] if a['fix']]}"
        for i in memory.recent_incidents(tenant, 15)
    )
    fixes = "\n".join(
        f"- {f['fix']} on {f['services']}: worked {f['worked']}x, failed {f['failed']}x"
        for f in memory.top_fixes(tenant)
    )
    deps = "\n".join(f"- {s} depends on {', '.join(d)}" for s, d in memory.service_map(tenant).items())
    system = f"""You are RecallOps, an on-call assistant. Answer only from this team's memory below.
If the memory does not have the answer, say so. Keep answers short.
When recommending a fix: a service's problem is often caused by what it depends on, so check those too.
Prefer the fix with the best overall record (worked minus failed), say its numbers,
and never recommend a fix that has never worked. One failure does not cancel many successes.

Service dependencies:
{deps or 'none'}

Recent incidents:
{incidents or 'none'}

Known fixes (overall record):
{fixes or 'none'}"""

    messages = [("system", system)] + [(m["role"], m["text"]) for m in history] + [("user", question)]
    model = ChatGroq(model=MODEL, temperature=0, max_tokens=1024, reasoning_effort="low")
    reply = model.invoke(messages).content

    memory.save_message(tenant, session_id, "user", question)
    memory.save_message(tenant, session_id, "assistant", reply)
    return reply


def run_incident(tenant: str, service: str, symptom: str) -> IncidentState:
    return incident_graph.invoke({"tenant": tenant, "service": service, "symptom": symptom})


if __name__ == "__main__":
    tenant = sys.argv[1] if len(sys.argv) > 1 else "acme"
    result = run_incident(tenant, "auth-service", "login latency above 5s, many timeouts")
    print(result["incident_id"])
    print(_steps_text(tenant, result["incident_id"]))
