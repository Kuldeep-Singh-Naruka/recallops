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
    return model.with_structured_output(schema, method="json_schema")


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


def investigator(state: IncidentState) -> IncidentState:
    tenant, service, incident_id = state["tenant"], state["service"], state["incident_id"]
    ctx = memory.service_context(tenant, service)
    past = memory.recall_similar(tenant, service)
    past_text = "\n".join(
        f"{n}. {p['fix']} | worked {p['success']}x, failed {p['fail']}x | on {p['services']} | symptoms: {p['symptoms']}"
        for n, p in enumerate(past, start=1)
    ) or "none"

    out = llm(Investigation).invoke(
        f"""You are the investigator agent. Find the likely root cause.
Service: {service}
It depends on: {ctx['depends_on']}

What other agents found so far:
{_steps_text(tenant, incident_id)}

Fixes from past incidents on this service or its dependencies:
{past_text}

If a past fix that worked matches the symptoms, set past_fix_number to its number.
Never pick a fix that only failed. Otherwise set past_fix_number to 0 and write new_fix.
Keep likely_cause and new_fix to one short sentence each."""
    )

    if 1 <= out.past_fix_number <= len(past):
        fix = past[out.past_fix_number - 1]["fix"]
        note = " (from memory)"
    else:
        fix = out.new_fix
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
    memory.add_step(
        tenant, incident_id, "resolver",
        f"Paged {owner.get('oncall', 'on-call')} ({owner.get('team', 'unknown team')}). "
        f"Proposed fix: {state['suggested_fix'].rstrip('.')}. Waiting for confirmation.",
    )
    return {}


builder = StateGraph(IncidentState)
builder.add_node("triage", triage)
builder.add_node("investigator", investigator)
builder.add_node("resolver", resolver)

builder.add_edge(START, "triage")
builder.add_edge("triage", "investigator")
builder.add_edge("investigator", "resolver")
builder.add_edge("resolver", END)

incident_graph = builder.compile()


def run_incident(tenant: str, service: str, symptom: str) -> IncidentState:
    return incident_graph.invoke({"tenant": tenant, "service": service, "symptom": symptom})


if __name__ == "__main__":
    tenant = sys.argv[1] if len(sys.argv) > 1 else "acme"
    result = run_incident(tenant, "auth-service", "login latency above 5s, many timeouts")
    print(result["incident_id"])
    print(_steps_text(tenant, result["incident_id"]))
