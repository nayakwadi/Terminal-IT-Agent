"""
Streamlit UI for the UC-01 Policy Conflict agent on Bedrock AgentCore.

Single-page app:
  1. Basic-auth wall — password is fetched once from Secrets Manager (or env).
  2. Ticket paste / upload form.
  3. Invokes Bedrock AgentCore Runtime via boto3 and renders the finding.

Environment variables:
  AGENT_RUNTIME_ARN          required — ARN from `agentcore launch` output
  AWS_REGION                 default us-east-1
  UI_PASSWORD_SECRET_NAME    optional — Secrets Manager secret holding the UI password
  UI_PASSWORD                optional — fallback plaintext password for local dev
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3
import botocore
import streamlit as st


REGION = os.getenv("AWS_REGION", "us-east-1")
AGENT_RUNTIME_ARN = os.getenv("AGENT_RUNTIME_ARN", "").strip()
UI_PASSWORD_SECRET_NAME = os.getenv("UI_PASSWORD_SECRET_NAME", "").strip()
UI_PASSWORD_ENV = os.getenv("UI_PASSWORD", "").strip()


# ---------- auth ----------

@st.cache_data(show_spinner=False)
def _expected_password() -> str:
    if UI_PASSWORD_SECRET_NAME:
        sm = boto3.client("secretsmanager", region_name=REGION)
        return sm.get_secret_value(SecretId=UI_PASSWORD_SECRET_NAME)["SecretString"].strip()
    if UI_PASSWORD_ENV:
        return UI_PASSWORD_ENV
    raise RuntimeError(
        "No UI password configured. Set UI_PASSWORD_SECRET_NAME or UI_PASSWORD."
    )


def _require_auth() -> None:
    if st.session_state.get("authenticated"):
        return

    st.title("🔐 ITOpsOrchestrator — Sign in")
    pwd = st.text_input("Password", type="password")
    if st.button("Sign in", type="primary"):
        try:
            if pwd and pwd == _expected_password():
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        except Exception as e:
            st.error(f"Could not verify password: {e}")
    st.stop()


# ---------- agent invocation ----------

def invoke_agent(ticket_text: str, ticket_id: str | None, post_to_teams: bool) -> dict[str, Any]:
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    payload = {"ticket_text": ticket_text, "post_to_teams": post_to_teams}
    if ticket_id:
        payload["ticket_id"] = ticket_id

    resp = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    body = resp["response"].read()
    return json.loads(body)


# ---------- rendering ----------

SEVERITY_BADGE = {
    "critical": ("🔴", "#b00020"),
    "high": ("🟠", "#d84315"),
    "medium": ("🟡", "#f9a825"),
    "low": ("🔵", "#1976d2"),
    "informational": ("🟢", "#2e7d32"),
}


def render_finding(result: dict[str, Any]) -> None:
    finding = result.get("finding") or {}
    severity = (finding.get("severity") or "informational").lower()
    icon, color = SEVERITY_BADGE.get(severity, ("⚪", "#616161"))
    conflict = bool(finding.get("conflict_detected"))
    confidence = float(finding.get("confidence") or 0.0)

    header = "CONFLICT DETECTED" if conflict else "NO CONFLICT DETECTED"
    st.markdown(
        f"### {icon} {header}  ·  "
        f"<span style='color:{color}'>{severity.upper()}</span>  ·  "
        f"Confidence {confidence:.0%}",
        unsafe_allow_html=True,
    )

    if result.get("ticket_id"):
        st.caption(f"Ticket: **{result['ticket_id']}**")

    st.markdown("#### Summary")
    st.write(finding.get("summary", "_(no summary)_"))

    st.markdown("#### User intent")
    st.write(finding.get("ticket_user_intent", "_(not detected)_"))

    if conflict:
        pa = finding.get("policy_a") or {}
        pb = finding.get("policy_b") or {}
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("#### 📄 Policy A — permits")
            st.caption(pa.get("source", ""))
            st.info(pa.get("excerpt", ""))
            st.write(pa.get("interpretation", ""))
        with col_b:
            st.markdown("#### 🛑 Policy B — blocks")
            st.caption(pb.get("source", ""))
            st.warning(pb.get("excerpt", ""))
            st.write(pb.get("interpretation", ""))

    st.markdown("#### ✅ Recommended action")
    st.success(finding.get("recommendation", "_(no recommendation)_"))

    if finding.get("_teams_post_error"):
        st.error(f"Teams post failed: {finding['_teams_post_error']}")

    with st.expander("🔍 Retrieval details"):
        st.write(f"**Query sent to KB:** `{result.get('retrieval_query', '')}`")
        for i, c in enumerate(result.get("chunks") or [], start=1):
            score = c.get("score")
            score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "n/a"
            st.markdown(f"**Chunk {i}** — score {score_s} — `{c.get('source', '')}`")
            st.text(c.get("text", "")[:1000])

    with st.expander("🧾 Raw JSON"):
        st.json(result)


# ---------- main ----------

def main() -> None:
    st.set_page_config(page_title="ITOpsOrchestrator UC-01", page_icon="🛡️", layout="wide")
    _require_auth()

    st.title("🛡️ ITOpsOrchestrator — Policy Conflict Detection")
    st.caption(f"Region: `{REGION}`  ·  Agent ARN: `{AGENT_RUNTIME_ARN or '(not set)'}`")

    if not AGENT_RUNTIME_ARN:
        st.error("AGENT_RUNTIME_ARN environment variable is not set on this service.")
        st.stop()

    tab_paste, tab_upload, tab_sample = st.tabs(["Paste ticket", "Upload .txt", "Sample ticket"])

    with tab_paste:
        ticket_text = st.text_area("Helpdesk ticket text", height=260, key="paste_text")

    with tab_upload:
        f = st.file_uploader("Upload a .txt ticket file", type=["txt"])
        if f is not None:
            ticket_text = f.read().decode("utf-8")
            st.session_state["paste_text"] = ticket_text
            st.text_area("Preview", value=ticket_text, height=260, disabled=True)

    with tab_sample:
        sample = st.selectbox(
            "Bundled sample tickets",
            options=[
                "",
                "ticket-INC0012345-dropbox-blocked.txt",
                "ticket-INC0012789-dropbox-sync-error.txt",
                "ticket-INC0013021-modify-zscaler-ssl-policy.txt",
                "ticket-INC0013055-share-env-credentials.txt",
            ],
        )
        if sample:
            path = os.path.join("/app/sample-tickets", sample)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    ticket_text = fh.read()
                    st.session_state["paste_text"] = ticket_text
                    st.text_area("Preview", value=ticket_text, height=260, disabled=True)
            else:
                st.warning(f"Sample not bundled in image: {path}")

    ticket_text = st.session_state.get("paste_text", "") or ""

    col_run, col_teams = st.columns([1, 2])
    with col_teams:
        post_to_teams = st.checkbox("Also post finding to MS Teams", value=False)
    with col_run:
        run = st.button("Analyze", type="primary", disabled=not ticket_text.strip())

    if run:
        with st.spinner("Calling AgentCore Runtime…"):
            try:
                result = invoke_agent(ticket_text, ticket_id=None, post_to_teams=post_to_teams)
            except botocore.exceptions.ClientError as e:
                st.error(f"AgentCore invocation failed: {e}")
                return
            except Exception as e:
                st.error(f"Unexpected error: {e}")
                return

        render_finding(result)


if __name__ == "__main__":
    main()
