# app.py
# Agent CI files: list with per-file delete, upload + overwrite by filename (preserve original name),
# "New thread" button, larger Ask box, and show only current run responses.

import os
import time
import json
import tempfile
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

def read_setting(key: str, required: bool = True, default: str = "") -> str:
    val = os.getenv(key, default).strip().strip('"').strip("'")
    if required and not val:
        st.error(f"Missing setting: {key}. Add it to .env or environment variables.")
        st.stop()
    return val

PROJECT_ENDPOINT = read_setting("PROJECT_ENDPOINT").rstrip("/")   # https://.../api/projects/<project>
ORCHESTRATOR_AGENT_ID = read_setting("ORCHESTRATOR_AGENT_ID")

# Auth and SDK clients
from azure.identity import DefaultAzureCredential
credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import CodeInterpreterTool, MessageAttachment

agents = AgentsClient(endpoint=PROJECT_ENDPOINT, credential=credential)

# Session state
if "thread_id" not in st.session_state:
    st.session_state["thread_id"] = None
if "file_id" not in st.session_state:
    st.session_state["file_id"] = None
if "agent_list" not in st.session_state:
    st.session_state["agent_list"] = []
if "logs" not in st.session_state:
    st.session_state["logs"] = []

def log(msg: str):
    st.session_state["logs"].append(msg)

def get_bearer_token_for_foundry() -> str:
    token = credential.get_token("https://ai.azure.com/.default")
    return token.token

@st.cache_data(ttl=60)
def fetch_agents_list_rest():
    # List Agents (project scoped)
    url = f"{PROJECT_ENDPOINT}/assistants?api-version=v1"
    headers = {"Authorization": f"Bearer {get_bearer_token_for_foundry()}"}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    items = []
    for a in data:
        label = (a.get("name") or "").strip() or a.get("id")
        items.append((label, a.get("id")))
    if ORCHESTRATOR_AGENT_ID and ORCHESTRATOR_AGENT_ID not in [aid for _, aid in items]:
        items.insert(0, ("Orchestrator", ORCHESTRATOR_AGENT_ID))
    return items

def get_agent(agent_id: str) -> dict:
    url = f"{PROJECT_ENDPOINT}/assistants/{agent_id}?api-version=v1"
    headers = {"Authorization": f"Bearer {get_bearer_token_for_foundry()}"}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()

def files_get_rest(file_id: str) -> dict:
    # Resolve filename/size for display
    url = f"{PROJECT_ENDPOINT}/files/{file_id}?api-version=v1"
    headers = {"Authorization": f"Bearer {get_bearer_token_for_foundry()}"}
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()

def list_agent_ci_files(agent_id: str):
    agent = get_agent(agent_id)
    file_ids = agent.get("tool_resources", {}).get("code_interpreter", {}).get("file_ids", []) or []
    rows = []
    for fid in file_ids:
        try:
            meta = files_get_rest(fid)
            rows.append({"file_id": fid, "filename": meta.get("filename", ""), "bytes": meta.get("bytes")})
        except Exception:
            rows.append({"file_id": fid, "filename": "(unavailable)", "bytes": None})
    return rows

def set_agent_ci_file_ids(agent_id: str, file_ids: list):
    # Ensure CI tool remains present while updating tool_resources
    agent = get_agent(agent_id)
    tools = agent.get("tools", []) or []
    if not any((t.get("type") == "code_interpreter") for t in tools):
        tools.append({"type": "code_interpreter"})
    body = {"tools": tools, "tool_resources": {"code_interpreter": {"file_ids": file_ids}}}
    url = f"{PROJECT_ENDPOINT}/assistants/{agent_id}?api-version=v1"
    headers = {"Authorization": f"Bearer {get_bearer_token_for_foundry()}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)
    resp.raise_for_status()
    return resp.json()

st.title("AI Foundry Orchestrated Multi‑Agent with File upload")

with st.sidebar:
    st.header("Agent & File Controls")
    if st.button("Refresh agent list"):
        st.cache_data.clear()
    try:
        st.session_state["agent_list"] = fetch_agents_list_rest()
    except Exception as e:
        st.error(f"Failed to list agents: {e}")
        st.stop()

    if not st.session_state["agent_list"]:
        st.error("No agents found in this project.")
        st.stop()

    display_names = [n for n, _ in st.session_state["agent_list"]]
    chosen = st.selectbox("Target agent", display_names, index=0)
    chosen_agent_id = dict(st.session_state["agent_list"])[chosen]

    # New thread button
    if st.button("New thread"):
        thread = agents.threads.create()
        st.session_state["thread_id"] = thread.id
        st.session_state["logs"] = []
        st.success(f"Started a new thread: {thread.id}")

    # Existing CI files with inline Delete
    st.subheader("Files in Code Interpreter")
    try:
        ci_files = list_agent_ci_files(chosen_agent_id)
        if ci_files:
            for f in ci_files:
                c1, c2 = st.columns([0.75, 0.25])
                with c1:
                    st.write(f"- {f['filename']} (id={f['file_id']}, bytes={f['bytes']})")
                with c2:
                    if st.button("Delete", key=f"del_{f['file_id']}"):
                        try:
                            remaining = [x["file_id"] for x in ci_files if x["file_id"] != f["file_id"]]
                            set_agent_ci_file_ids(chosen_agent_id, remaining)  # remove from tool_resources
                            try:
                                agents.files.delete(file_id=f["file_id"])  # remove file object
                            except Exception:
                                pass
                            st.success(f"Deleted {f['filename']} from CI and project.")
                            st.cache_data.clear(); st.rerun()
                        except Exception as e:
                            st.error(f"Delete failed: {e}")
        else:
            st.info("No files attached to Code Interpreter for this agent.")
    except Exception as e:
        st.warning(f"Could not list CI files: {e}")

# Upload and overwrite-by-filename (preserve original filename; no temp suffix)
uploaded = st.file_uploader("Upload a file to attach/persist in Code Interpreter", type=["xlsx","xlsm","xls","csv","pdf","png","jpg","jpeg"])

if uploaded and st.button("Upload and persist (overwrite by filename)"):
    # Write bytes to a temp directory with the original name, then upload by file_path + filename
    data = uploaded.getvalue()
    with st.spinner("Uploading to Foundry Files…"):
        tmpdir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmpdir, uploaded.name)  # exact original name; no random suffix
        with open(tmp_path, "wb") as f:
            f.write(data)
        try:
            # Preserve stored name using filename kw per FilesOperations.upload
            new_file = agents.files.upload(file_path=tmp_path, purpose="assistants", filename=uploaded.name)  # API supports filename kw
            st.session_state["file_id"] = new_file.id
            log(f"Uploaded new file_id={new_file.id} for '{uploaded.name}'.")
        finally:
            try:
                os.remove(tmp_path)
                os.rmdir(tmpdir)
            except Exception:
                pass

    # Persist in CI, overwriting by filename if needed
    try:
        ci_files = list_agent_ci_files(chosen_agent_id)
        existing_ids = [e["file_id"] for e in ci_files]
        existing_by_name = {(e["filename"] or "").lower(): e for e in ci_files}
        new_name_lc = uploaded.name.lower()

        if new_name_lc in existing_by_name:
            old_id = existing_by_name[new_name_lc]["file_id"]
            new_ids = [st.session_state["file_id"] if fid == old_id else fid for fid in existing_ids]
            set_agent_ci_file_ids(chosen_agent_id, new_ids)
            try:
                agents.files.delete(file_id=old_id)
            except Exception:
                pass
            st.success(f"File '{uploaded.name}' has been overwritten in Code Interpreter.")
            log(f"Overwritten: '{uploaded.name}' old_id={old_id} -> new_id={st.session_state['file_id']}.")
        else:
            set_agent_ci_file_ids(chosen_agent_id, existing_ids + [st.session_state["file_id"]])
            st.success(f"File '{uploaded.name}' attached to Code Interpreter.")
            log(f"Persisted new file to CI: '{uploaded.name}' id={st.session_state['file_id']}.")

        st.cache_data.clear(); st.rerun()
    except Exception as e:
        st.error(f"Persist/overwrite failed: {e}")

# Larger Ask box
question = st.text_area("Ask the orchestrator", height=180, placeholder="Type a detailed question or instructions...")

# Run: render only current run’s assistant output (history remains in thread)
if st.button("Run"):
    # Create/reuse thread (history persists for context)
    if st.session_state["thread_id"] is None:
        thread = agents.threads.create()
        st.session_state["thread_id"] = thread.id
        log(f"Thread created: {thread.id}.")
    else:
        class _T: pass
        thread = _T(); thread.id = st.session_state["thread_id"]

    # Attach last uploaded file so CI can read it this run
    attachments = []
    if st.session_state.get("file_id"):
        ci_tool = CodeInterpreterTool()
        attachments = [MessageAttachment(file_id=st.session_state["file_id"], tools=ci_tool.definitions)]
        log("File attached to Code Interpreter for this run (message-level).")

    user_prompt = question or "Please analyze the uploaded file."
    agents.messages.create(thread_id=thread.id, role="user", content=user_prompt, attachments=attachments)

    with st.spinner("Running orchestrator…"):
        run = agents.runs.create(thread_id=thread.id, agent_id=ORCHESTRATOR_AGENT_ID)
        status = run.status
        while status in ("queued", "in_progress", "requires_action"):
            time.sleep(2)
            run = agents.runs.get(thread_id=thread.id, run_id=run.id)
            status = run.status
        st.info(f"Run status: {status}")

    # Show only current run’s assistant messages
    pager = agents.messages.list(thread_id=thread.id, run_id=run.id, order="asc", limit=100)
    chunks = []
    for m in pager:
        if m.role != "assistant":
            continue
        if getattr(m, "text_messages", None):
            for tmc in m.text_messages:
                if getattr(tmc, "text", None) and getattr(tmc.text, "value", None):
                    chunks.append(tmc.text.value)
    if chunks:
        st.markdown("**Assistant:**")
        st.write("\n\n".join(chunks))
    else:
        st.warning("No assistant response generated.")

# Show logs
if st.session_state["logs"]:
    with st.expander("Logs"):
        for l in st.session_state["logs"]:
            st.write(f"- {l}")  
