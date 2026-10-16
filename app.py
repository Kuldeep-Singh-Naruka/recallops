import json

import pandas as pd
import streamlit as st

import agents
import memory
from db import list_tenants
from seed import seed

st.set_page_config(page_title="RecallOps", layout="wide")

# ---------- sidebar: tenant + session ----------

st.sidebar.title("RecallOps")
st.sidebar.caption("Incident agents that remember")

tenants = list_tenants() or ["acme"]
tenant = st.sidebar.selectbox("Tenant", tenants)

with st.sidebar.expander("New tenant"):
    new_name = st.text_input("Name", placeholder="globex")
    template = st.selectbox("Architecture", ["streaming", "ecommerce"])
    if st.button("Create (empty memory)") and new_name:
        seed(new_name.strip().lower(), with_history=False, template=template)
        st.rerun()

session_id = st.sidebar.text_input("Chat session", value="oncall-shift-1")
st.sidebar.caption("Each tenant has its own graph in FalkorDB. Memory survives restarts.")

incident_tab, memory_tab, chat_tab, graph_tab = st.tabs(["Incident", "Memory", "Ask", "Graph"])

# ---------- incident: run the 3 agents ----------

with incident_tab:
    services = memory.list_services(tenant)
    if not services:
        st.info("This tenant has no services yet. Run `python seed.py` first.")
        st.stop()

    col1, col2 = st.columns([1, 2])
    service = col1.selectbox("Alert on service", services)
    symptom = col2.text_input("Symptom", value="login latency above 5s, many timeouts")

    if st.button("Run agents", type="primary"):
        incident_id = None
        with st.status("Agents working...", expanded=True) as status:
            stream = agents.incident_graph.stream(
                {"tenant": tenant, "service": service, "symptom": symptom},
                stream_mode="updates",
            )
            shown = 0
            for update in stream:
                node, output = next(iter(update.items()))
                if output and output.get("incident_id"):
                    incident_id = output["incident_id"]
                steps = memory.get_steps(tenant, incident_id)
                for step in steps[shown:]:
                    st.markdown(f"**{step['agent']}** — {step['text']}")
                shown = len(steps)
            status.update(label=f"Done: {incident_id}", state="complete")

    st.subheader("Waiting for confirmation")
    waiting = [i for i in memory.recent_incidents(tenant, 20) if i["status"] == "awaiting_confirmation"]
    if not waiting:
        st.caption("Nothing waiting.")
    for inc in waiting:
        with st.container(border=True):
            st.markdown(f"**{inc['id']}** · {inc['service']} · {inc['symptom']}")
            st.markdown(f"Proposed fix: **{inc['proposed_fix']}**")
            with st.expander("Agent steps"):
                for step in memory.get_steps(tenant, inc["id"]):
                    st.markdown(f"**{step['agent']}** — {step['text']}")
            ok, bad, _ = st.columns([1, 1, 4])
            if ok.button("Fix worked", key="ok" + inc["id"]):
                memory.resolve_incident(tenant, inc["id"], inc["proposed_fix"], worked=True)
                st.rerun()
            if bad.button("Didn't work", key="bad" + inc["id"]):
                memory.resolve_incident(tenant, inc["id"], inc["proposed_fix"], worked=False)
                st.rerun()

# ---------- memory: what the agents have learned ----------

with memory_tab:
    st.subheader("Procedural memory: fixes ranked by track record")
    st.dataframe(pd.DataFrame(memory.top_fixes(tenant)), width="stretch")

    st.subheader("Episodic memory: recent incidents")
    rows = []
    for i in memory.recent_incidents(tenant, 20):
        tried = [f"{a['fix']} ({'worked' if a['worked'] else 'failed'})" for a in i["attempts"] if a["fix"]]
        rows.append({"id": i["id"], "service": i["service"], "symptom": i["symptom"],
                     "status": i["status"], "tried": ", ".join(tried)})
    st.dataframe(pd.DataFrame(rows), width="stretch")

# ---------- chat: ask the memory ----------

with chat_tab:
    st.caption(f"Session **{session_id}** — history is loaded from FalkorDB, so it survives a restart.")
    for msg in memory.get_history(tenant, session_id, limit=50):
        st.chat_message(msg["role"]).write(msg["text"])

    question = st.chat_input("e.g. What usually fixes redis problems?")
    if question:
        st.chat_message("user").write(question)
        with st.spinner("Thinking..."):
            reply = agents.answer_question(tenant, session_id, question)
        st.chat_message("assistant").write(reply)

# ---------- graph view ----------

COLORS = {"Service": "#e07be0", "Team": "#c9a227", "Incident": "#4f7cff", "Fix": "#2fb67c",
          "Step": "#8c8c8c", "Agent": "#ff8a3d", "Session": "#d9534f", "Message": "#bbbbbb"}

with graph_tab:
    view = st.radio("View", list(memory.GRAPH_VIEWS), horizontal=True)
    incident_id = ""
    if view == "One incident":
        ids = [i["id"] for i in memory.recent_incidents(tenant, 20)]
        incident_id = st.selectbox("Incident", ids) if ids else ""

    nodes, edges = memory.subgraph(tenant, view, incident_id)
    for n in nodes:
        n["color"] = COLORS.get(n["group"], "#999999")

    data = json.dumps({"nodes": nodes, "edges": edges}).replace("</", "<\\/")
    st.iframe(
        f"""
        <div id="g" style="height:600px;background:#111;border-radius:8px"></div>
        <script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
        <script>
          new vis.Network(document.getElementById("g"),
            {data},
            {{nodes: {{shape: "dot", size: 14, font: {{color: "#eee", size: 12}}}},
              edges: {{arrows: "to", color: "#666", font: {{color: "#aaa", size: 9, strokeWidth: 0}}}},
              physics: {{stabilization: true}}}});
        </script>
        """,
        height=620,
    )
