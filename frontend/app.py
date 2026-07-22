"""Streamlit frontend (Section 4.6, Stage 7) -- exactly two views, per spec:

1. Health report summary, pulling from `GET /audit/report/{service}`.
2. Chat box hitting `POST /chat`.

Deliberately thin: no charts, no history, no extra dashboards. SigNoz
remains the primary dashboard (Section 4.4's provisioned panels); this app
exists only to surface the Guardian's own health-score/findings/narrative
output and let a human ask it free-form questions -- "do not add
additional views ... anything beyond these two belongs in SigNoz itself."

Run with:
    streamlit run frontend/app.py

Config via env (see `.env.example`):
    GUARDIAN_API_URL   -- FastAPI backend base URL, default http://localhost:8000
"""

from __future__ import annotations

import os

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("GUARDIAN_API_URL", "http://localhost:8000").rstrip("/")
DEFAULT_SERVICE = "_all_"  # matches guardian/scheduler.py's no-scope key; "all" also accepted by the API

st.set_page_config(page_title="Telemetry Health Guardian", page_icon="🩺", layout="wide")


def _get_report(service: str) -> tuple[dict | None, str | None]:
    try:
        resp = requests.get(f"{API_URL}/audit/report/{service}", timeout=30)
    except requests.RequestException as exc:
        return None, f"Could not reach the Guardian API at {API_URL}: {exc}"
    if resp.status_code == 404:
        return None, resp.json().get("detail", "No audit report yet for this service.")
    if not resp.ok:
        return None, f"Guardian API returned {resp.status_code}: {resp.text}"
    return resp.json(), None


def _trigger_audit(service: str | None) -> tuple[dict | None, str | None]:
    payload = {"service": None if service in (DEFAULT_SERVICE, "all") else service}
    try:
        resp = requests.post(f"{API_URL}/audit/run", json=payload, timeout=120)
    except requests.RequestException as exc:
        return None, f"Could not reach the Guardian API at {API_URL}: {exc}"
    if not resp.ok:
        return None, f"Guardian API returned {resp.status_code}: {resp.text}"
    return resp.json(), None


def _send_chat(service: str, question: str) -> tuple[dict | None, str | None]:
    payload = {"question": question, "service": None if service in (DEFAULT_SERVICE, "all") else service}
    try:
        resp = requests.post(f"{API_URL}/chat", json=payload, timeout=60)
    except requests.RequestException as exc:
        return None, f"Could not reach the Guardian API at {API_URL}: {exc}"
    if not resp.ok:
        return None, f"Guardian API returned {resp.status_code}: {resp.text}"
    return resp.json(), None


with st.sidebar:
    st.header("🩺 Telemetry Health Guardian")
    st.caption("An auditor for telemetry hygiene -- not what the agent did, but whether its telemetry can be trusted.")
    service = st.text_input(
        "Service",
        value=DEFAULT_SERVICE,
        help="A specific SigNoz service name, or '_all_' / 'all' for the combined audit across every service.",
    )
    st.caption(f"Guardian API: {API_URL}")
    if st.button("Run audit now", use_container_width=True):
        with st.spinner("Running audit against SigNoz..."):
            result, error = _trigger_audit(service)
        if error:
            st.error(error)
        else:
            st.success("Audit cycle complete.")
            st.session_state["latest_report"] = result

tab_report, tab_chat = st.tabs(["Health Report", "Chat"])

with tab_report:
    st.subheader(f"Health report -- {service}")

    report = st.session_state.get("latest_report")
    if report is None or report.get("service") != (None if service in (DEFAULT_SERVICE, "all") else service):
        report, error = _get_report(service)
        if error:
            st.info(error)
            report = None

    if report is not None:
        health = report["health_score"]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Health score", f"{health['score']:.1f} / 100")
        col2.metric("Missing-field rate (R1)", f"{health['terms'].get('missing_field_rate_pct', 0):.1f}%")
        col3.metric("Orphaned-span rate (R3)", f"{health['terms'].get('orphaned_span_rate_pct', 0):.1f}%")
        col4.metric("Truncation rate (R6)", f"{health['terms'].get('truncation_rate_pct', 0):.1f}%")

        if report.get("narrative"):
            st.markdown("### Guardian's narrative")
            st.write(report["narrative"])
        elif report.get("narrative_error"):
            st.warning(f"Narrative generation failed for this cycle: {report['narrative_error']}")

        st.markdown("### Findings by rule")
        findings = report["findings"]
        for rule_id in ("r1", "r2", "r3", "r6", "r7"):
            if rule_id not in findings:
                continue
            rule_data = findings[rule_id]
            count = len(rule_data.get("findings", []))
            label = f"{rule_id.upper()} -- {count} finding{'s' if count != 1 else ''}"
            with st.expander(label, expanded=count > 0):
                if count == 0:
                    st.write("Clean -- no issues detected in this audit window.")
                else:
                    st.json(rule_data["findings"])
    else:
        st.write("No report to show yet. Click **Run audit now** in the sidebar.")

with tab_chat:
    st.subheader(f"Ask the Guardian -- {service}")
    st.caption("Answers are grounded only in the most recent audit's findings for this service.")

    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []

    for entry in st.session_state["chat_history"]:
        with st.chat_message("user"):
            st.write(entry["question"])
        with st.chat_message("assistant"):
            st.write(entry["answer"])
            if entry.get("rules_fired_but_uncited"):
                st.caption(
                    "⚠️ Rules that fired but weren't named in this answer: "
                    + ", ".join(entry["rules_fired_but_uncited"])
                )

    question = st.chat_input("e.g. Why did the research pipeline's score drop?")
    if question:
        with st.chat_message("user"):
            st.write(question)
        with st.spinner("Thinking..."):
            answer_payload, error = _send_chat(service, question)
        with st.chat_message("assistant"):
            if error:
                st.error(error)
            else:
                st.write(answer_payload["answer"])
                if answer_payload.get("rules_fired_but_uncited"):
                    st.caption(
                        "⚠️ Rules that fired but weren't named in this answer: "
                        + ", ".join(answer_payload["rules_fired_but_uncited"])
                    )
                st.session_state["chat_history"].append(
                    {
                        "question": question,
                        "answer": answer_payload["answer"],
                        "rules_fired_but_uncited": answer_payload.get("rules_fired_but_uncited", []),
                    }
                )
