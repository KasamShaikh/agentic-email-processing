"""
Agentic Email Processing — Live Operations Dashboard (backend).

A lightweight FastAPI app that powers a single-screen, real-time view of the
whole flow:  email received -> orchestrator called -> which specialist agent
fired -> run-step traces -> final answer.  It also surfaces the real Logic App
run history (the actual email-triggered runs).

Run it:
    pip install -r requirements.txt
    python -m uvicorn app:app --reload --port 8000
    # then open http://localhost:8000

Auth uses your `az login` identity (Cognitive Services User on the Foundry
account + reader on the Logic App resource group).  Nothing secret is written
to disk — the subscription id is read live from `az account show`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.ai.agents import AgentsClient
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
ENDPOINT = "https://agentic-email-foundry-ks.services.ai.azure.com/api/projects/email-agentic-ks"
ORCHESTRATOR_NAME = "orchestrator-ks"
RESOURCE_GROUP = "agentic-email-processing"
LOGIC_APP_NAME = "logic-email-ks"

# connected-agent tool name -> the agent it represents
TOOL_TO_AGENT = {
    "contract_note": "contract-note-ks",
    "pre_onboarding": "pre-onboarding-ks",
    "manual": "manual-intervention-ks",
    "form_verification": "form-verification-ks",
}

# intent -> specialist agent (code-driven routing)
INTENT_TO_AGENT = {
    "contract_note": "contract-note-ks",
    "pre_onboarding": "pre-onboarding-ks",
    "manual": "manual-intervention-ks",
}
INTENT_LABEL = {
    "contract_note": "Contract Note",
    "pre_onboarding": "Merchant Pre-Onboarding",
    "manual": "Manual Intervention",
}

SAMPLES = {
    "contract": {
        "subject": "Contract Note - Trade Confirmation 27-May-2026",
        "from": "broker@brokerage.example.com",
        "bodyPreview": "Please find attached your contract note for the trade executed today.",
        "body": "Dear Client, please find attached the contract note for your trades. "
        "Regards, Broker Ops.",
        "attachmentBlobs": ["incoming-attachments/AU_C3320_27052026_1192284.png"],
    },
    "onboarding": {
        "subject": "Merchant pre-onboarding documents for ACME Traders",
        "from": "ops@acme-traders.example.com",
        "bodyPreview": "Submitting KYC and registration documents for onboarding.",
        "body": "Hi, attaching our business registration and bank details to start "
        "the merchant onboarding for ACME Traders Pvt Ltd.",
        "attachmentBlobs": ["incoming-attachments/acme-kyc.pdf"],
    },
    "manual": {
        "subject": "Team lunch on Friday",
        "from": "hr@company.example.com",
        "bodyPreview": "Reminder: team lunch at 1 PM in the cafeteria.",
        "body": "Hi all, just a reminder that we have a team lunch this Friday at 1 PM. "
        "Please RSVP. Thanks!",
        "attachmentBlobs": [],
    },
}

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Agentic Email Processing Dashboard")

_credential = DefaultAzureCredential()
_client: AgentsClient | None = None


def client() -> AgentsClient:
    global _client
    if _client is None:
        _client = AgentsClient(
            endpoint=ENDPOINT,
            credential=_credential,
            credential_scopes=["https://ai.azure.com/.default"],
        )
    return _client


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


DATA_DIR = Path(__file__).parent / "data"
PROCESSED_LOG = DATA_DIR / "processed.jsonl"


def _record(intent: str, agents: list, attachments: list, files: list, source: str) -> None:
    """Append one processed-email record for the Overview tab (best effort)."""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        rec = {
            "ts": time.time(),
            "date": time.strftime("%Y-%m-%d"),
            "intent": intent,
            "label": INTENT_LABEL.get(intent, intent),
            "agents": agents,
            "attachments": len(attachments or []),
            "files": len(files or []),
            "source": source,
        }
        with PROCESSED_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001
        pass


TRACE_DIR = DATA_DIR / "traces"


def _save_trace(run_name: str, events: list) -> None:
    """Persist the full end-to-end agent trace for a Logic App run so it can be
    viewed later straight from disk — no need to re-run the agents."""
    if not run_name or not re.fullmatch(r"[A-Za-z0-9]+", run_name):
        return
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        (TRACE_DIR / f"{run_name}.json").write_text(
            json.dumps({"runName": run_name, "ts": time.time(), "events": events}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _load_trace(run_name: str) -> dict | None:
    """Read a previously saved end-to-end agent trace for a run, if present."""
    if not run_name or not re.fullmatch(r"[A-Za-z0-9]+", run_name):
        return None
    try:
        p = TRACE_DIR / f"{run_name}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _list_logicapp_runs(top: int = 10) -> list[dict]:
    """Recent Logic App runs as [{name, status, startTime, endTime}], or []."""
    az = _az()
    sub = _sub()
    if not az or not sub:
        return []
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
        f"{RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/{LOGIC_APP_NAME}/"
        "runs?api-version=2016-06-01"
    )
    res = subprocess.run(
        [az, "rest", "--method", "get", "--url", url],
        capture_output=True, text=True, timeout=60, shell=False,
    )
    if res.returncode != 0:
        return []
    runs = []
    for r in json.loads(res.stdout or "{}").get("value", [])[:top]:
        p = r.get("properties", {})
        runs.append(
            {
                "name": r.get("name"),
                "status": p.get("status"),
                "startTime": p.get("startTime"),
                "endTime": p.get("endTime"),
            }
        )
    return runs


def _iter_process_run(run_name: str):
    """Reconstruct a real email from a Logic App run and process it end-to-end,
    yielding SSE chunks and saving the full trace to disk. Shared by the live
    stream endpoint and the auto-processing poller."""
    collected: list = []
    payload = _reconstruct_email_from_run(run_name)
    if not payload:
        yield _sse(
            {
                "type": "error",
                "message": "Could not read this email from the Logic App run outputs.",
            }
        )
        return

    att_display = [
        (a.get("name") if isinstance(a, dict) else a)
        for a in payload.get("attachments", [])
    ] or payload.get("attachmentBlobs", [])
    email_ev = {
        "type": "email",
        "subject": payload.get("subject"),
        "from": payload.get("from"),
        "body": payload.get("body") or payload.get("bodyPreview"),
        "attachments": att_display,
        "source": "logicapp",
        "runName": run_name,
        "ts": time.time(),
    }
    collected.append(email_ev)
    yield _sse(email_ev)

    try:
        for chunk in _orchestrate(client(), json.dumps(payload), source="logicapp"):
            try:
                collected.append(json.loads(chunk[6:].strip()))
            except Exception:  # noqa: BLE001
                pass
            yield chunk
    except Exception as exc:  # noqa: BLE001
        yield _sse({"type": "error", "message": str(exc)[:400]})
    finally:
        _save_trace(run_name, collected)


# --------------------------------------------------------------------------- #
# Auto-processing poller — makes real emails flow end-to-end with no UI click.
# A new Logic App run (an email arrived + attachments ingested) is picked up and
# processed through the agents automatically. Disable with AUTO_PROCESS=0.
# --------------------------------------------------------------------------- #
AUTO_PROCESS = os.getenv("AUTO_PROCESS", "1").lower() not in ("0", "false", "no")
POLL_INTERVAL = int(os.getenv("AUTO_PROCESS_INTERVAL", "20"))
_poller_started_at = time.time()


def _iso_to_epoch(s: str) -> float:
    s = (s or "").replace("Z", "+00:00")
    # Logic App timestamps carry 7-digit fractional seconds; fromisoformat wants <=6.
    m = re.match(r"(.*\.\d{6})\d*([+-]\d{2}:\d{2})?$", s)
    if m:
        s = m.group(1) + (m.group(2) or "")
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _autoprocess_run(run_name: str) -> None:
    """Process one run end-to-end (consume the shared generator)."""
    for _ in _iter_process_run(run_name):
        pass


def _poller_loop() -> None:
    """Background loop: auto-process new, successful Logic App runs once each."""
    while True:
        try:
            for run in _list_logicapp_runs(10):
                if run.get("status") != "Succeeded":
                    continue
                # Only emails that arrived after the dashboard started, so we don't
                # reprocess history; each run is processed at most once (trace guard).
                if _iso_to_epoch(run.get("startTime")) < _poller_started_at:
                    continue
                if _load_trace(run["name"]):
                    continue
                _autoprocess_run(run["name"])
        except Exception:  # noqa: BLE001
            pass
        time.sleep(POLL_INTERVAL)


@app.on_event("startup")
def _start_poller() -> None:
    if AUTO_PROCESS:
        threading.Thread(target=_poller_loop, name="autoprocess", daemon=True).start()


def _az() -> str | None:
    """Resolve the Azure CLI executable (az.cmd on Windows)."""
    return shutil.which("az") or shutil.which("az.cmd")


def _agents_index() -> dict[str, str]:
    """id -> name for all agents in the project."""
    return {a.id: a.name for a in client().list_agents()}


def _detect_agents(step_dict: dict, id_to_name: dict[str, str]) -> list[str]:
    """Given a run-step dict, return any specialist agents it references."""
    blob = json.dumps(step_dict)
    hits: set[str] = set()
    for tool_name, agent_name in TOOL_TO_AGENT.items():
        if f'"{tool_name}"' in blob:
            hits.add(agent_name)
    for aid, aname in id_to_name.items():
        if aid in blob and aname != ORCHESTRATOR_NAME:
            hits.add(aname)
    return sorted(hits)


def _agent_reply(ag, thread_id: str) -> str:
    """Latest assistant text on a thread."""
    for msg in ag.messages.list(thread_id=thread_id):
        md = msg.as_dict()
        if md.get("role") == "assistant":
            parts = [
                c.get("text", {}).get("value", "")
                for c in md.get("content", [])
                if c.get("type") == "text"
            ]
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text
    return ""


def _run_agent(ag, agent_id: str, content: str) -> dict:
    """Run one leaf agent to completion; return status/text/error."""
    thread = ag.threads.create()
    ag.messages.create(thread_id=thread.id, role="user", content=content)
    run = ag.runs.create_and_process(thread_id=thread.id, agent_id=agent_id)
    status = str(run.status).split(".")[-1].lower()
    err = None
    if getattr(run, "last_error", None):
        le = run.last_error
        err = le.as_dict() if hasattr(le, "as_dict") else str(le)
    return {
        "status": status,
        "text": _agent_reply(ag, thread.id),
        "threadId": thread.id,
        "runId": run.id,
        "error": err,
    }


def _parse_intent(text: str) -> tuple[str, str]:
    """Extract {intent, reason} from the classifier output (robust to extra prose)."""
    intent, reason = "", ""
    match = re.search(r"\{.*\}", text or "", re.S)
    if match:
        try:
            obj = json.loads(match.group(0))
            intent = str(obj.get("intent", "")).strip()
            reason = str(obj.get("reason", "")).strip()
        except Exception:  # noqa: BLE001
            pass
    if intent not in INTENT_TO_AGENT:
        low = (text or "").lower()
        if "contract" in low or "trade" in low:
            intent = "contract_note"
        elif "onboard" in low or "merchant" in low or "kyc" in low:
            intent = "pre_onboarding"
        else:
            intent = "manual"
    return intent, reason


def _status_str(run) -> str:
    """Normalise a run status enum/string to a lowercase token."""
    return str(run.status).split(".")[-1].lower()


def _run_contract(ag, contract_id: str, blobs: list):
    """Run the contract pipeline, yield its SSE events, return (files, warnings)."""
    from contract_pipeline import process as _contract_process

    files_written: list = []
    all_warnings: list = []
    try:
        for ev in _contract_process(blobs, lambda c: _run_agent(ag, contract_id, c)):
            yield _sse(ev)
            if ev.get("type") == "contract_done":
                files_written = ev.get("files", [])
                all_warnings = ev.get("warnings", [])
    except Exception as exc:  # noqa: BLE001
        yield _sse({"type": "error", "message": str(exc)[:400]})
    return files_written, all_warnings


def _orchestrate(ag, payload_str: str, source: str = "ui"):
    """Yield SSE events for one email: the orchestrator agent classifies the intent
    and, for contract notes, calls its `run_contract_pipeline` tool — which we execute
    here (extract -> map -> grouped PIS file -> blob) and feed back to the agent.
    Pre-onboarding / manual intents stay code-routed to their leaf agents.

    Reused by the UI simulator (/api/stream), real Logic App emails
    (/api/events/stream) and the headless processor (/api/process).
    """
    id_to_name = _agents_index()
    name_to_id = {n: i for i, n in id_to_name.items()}
    orchestrator_id = name_to_id.get(ORCHESTRATOR_NAME)
    contract_id = name_to_id.get("contract-note-ks")
    if not orchestrator_id:
        yield _sse({"type": "error", "message": "orchestrator-ks not found"})
        return

    try:
        payload = json.loads(payload_str)
    except Exception:  # noqa: BLE001
        payload = {}
    payload_attachments = payload.get("attachmentBlobs") or []

    agents_called: list[str] = []
    results: list[dict] = []
    contract_handled = False
    files_written: list = []
    all_warnings: list = []

    yield _sse(
        {
            "type": "orchestrator_start",
            "agentId": orchestrator_id,
            "name": ORCHESTRATOR_NAME,
            "ts": time.time(),
        }
    )
    yield _sse({"type": "status", "status": "running", "ts": time.time()})

    # --- Agentic run: the orchestrator decides. If it calls run_contract_pipeline,
    #     we execute the real pipeline here and submit the summary back. -----------
    thread = ag.threads.create()
    ag.messages.create(thread_id=thread.id, role="user", content=payload_str)
    run = ag.runs.create(thread_id=thread.id, agent_id=orchestrator_id)
    yield _sse(
        {
            "type": "run_created",
            "threadId": thread.id,
            "runId": run.id,
            "status": _status_str(run),
            "ts": time.time(),
        }
    )

    deadline = time.time() + 180
    while _status_str(run) in ("queued", "in_progress", "requires_action") and time.time() < deadline:
        if _status_str(run) == "requires_action":
            ra = getattr(run, "required_action", None)
            tool_calls = getattr(getattr(ra, "submit_tool_outputs", None), "tool_calls", []) or []
            outputs = []
            for tc in tool_calls:
                fname = tc.function.name
                if fname == "run_contract_pipeline":
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:  # noqa: BLE001
                        args = {}
                    blobs = (
                        args.get("attachment_blobs")
                        or args.get("attachmentBlobs")
                        or payload_attachments
                    )
                    yield _sse({"type": "agent_called", "name": "contract-note-ks", "ts": time.time()})
                    if "contract-note-ks" not in agents_called:
                        agents_called.append("contract-note-ks")
                    fw, ww = yield from _run_contract(ag, contract_id, blobs)
                    files_written, all_warnings = fw, ww
                    contract_handled = True
                    names = ", ".join(
                        f.get("file", "") if isinstance(f, dict) else str(f) for f in fw
                    )
                    summary = f"Wrote {len(fw)} file(s){': ' + names if names else ''}."
                    if ww:
                        summary += f" {len(ww)} warning(s)."
                    outputs.append({"tool_call_id": tc.id, "output": summary})
                else:
                    outputs.append({"tool_call_id": tc.id, "output": "unsupported tool"})
            run = ag.runs.submit_tool_outputs(
                thread_id=thread.id, run_id=run.id, tool_outputs=outputs
            )
        else:
            time.sleep(0.7)
            run = ag.runs.get(thread_id=thread.id, run_id=run.id)

    status = _status_str(run)
    err = None
    if getattr(run, "last_error", None):
        le = run.last_error
        err = le.as_dict() if hasattr(le, "as_dict") else str(le)

    if status != "completed":
        yield _sse(
            {
                "type": "done",
                "status": status,
                "agentsCalled": agents_called,
                "finalMessage": "",
                "error": err,
                "threadId": thread.id,
                "ts": time.time(),
            }
        )
        return

    intent, reason = _parse_intent(_agent_reply(ag, thread.id))
    yield _sse(
        {
            "type": "intent",
            "intent": intent,
            "label": INTENT_LABEL.get(intent, intent),
            "reason": reason,
            "ts": time.time(),
        }
    )

    target = INTENT_TO_AGENT.get(intent, "manual-intervention-ks")

    # Contract notes are handled by the agent's tool call above. If the model
    # classified contract_note but didn't call the tool, run the pipeline in code.
    if intent == "contract_note":
        if not contract_handled:
            yield _sse({"type": "agent_called", "name": "contract-note-ks", "ts": time.time()})
            if "contract-note-ks" not in agents_called:
                agents_called.append("contract-note-ks")
            fw, ww = yield from _run_contract(ag, contract_id, payload_attachments)
            files_written, all_warnings = fw, ww
        results.append(
            {"agent": "contract-note-ks", "files": files_written, "warnings": all_warnings}
        )
        final = {"intent": intent, "delegated_to": agents_called, "results": results}
        _record(intent, agents_called, payload_attachments, files_written, source)
        yield _sse(
            {
                "type": "done",
                "status": "completed",
                "intent": intent,
                "agentsCalled": agents_called,
                "finalMessage": json.dumps(final, indent=2),
                "error": None,
                "threadId": thread.id,
                "ts": time.time(),
            }
        )
        return

    # pre_onboarding / manual: code-routed leaf agents.
    yield _sse({"type": "agent_called", "name": target, "ts": time.time()})
    agents_called.append(target)
    spec = _run_agent(ag, name_to_id[target], payload_str)
    yield _sse(
        {
            "type": "result",
            "agent": target,
            "status": spec["status"],
            "text": spec["text"],
            "error": spec["error"],
            "ts": time.time(),
        }
    )
    results.append({"agent": target, "result": spec["text"]})

    if intent == "pre_onboarding" and spec["status"] == "completed":
        fv = "form-verification-ks"
        yield _sse({"type": "agent_called", "name": fv, "ts": time.time()})
        agents_called.append(fv)
        fvr = _run_agent(ag, name_to_id[fv], spec["text"] or payload_str)
        yield _sse(
            {
                "type": "result",
                "agent": fv,
                "status": fvr["status"],
                "text": fvr["text"],
                "error": fvr["error"],
                "ts": time.time(),
            }
        )
        results.append({"agent": fv, "result": fvr["text"]})

    final = {"intent": intent, "delegated_to": agents_called, "results": results}
    _record(intent, agents_called, payload_attachments, [], source)
    yield _sse(
        {
            "type": "done",
            "status": "completed",
            "intent": intent,
            "agentsCalled": agents_called,
            "finalMessage": json.dumps(final, indent=2),
            "error": None,
            "threadId": thread.id,
            "ts": time.time(),
        }
    )



def _sub() -> str | None:
    az = _az()
    if not az:
        return None
    out = subprocess.run(
        [az, "account", "show", "--query", "id", "-o", "tsv"],
        capture_output=True, text=True, timeout=30, shell=False,
    )
    return out.stdout.strip() or None


def _fetch_link_json(uri: str):
    with urllib.request.urlopen(uri, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _clean_text(s, limit: int = 4000) -> str:
    """Strip HTML/whitespace and cap length so big email threads don't blow up
    the classifier (large raw-HTML bodies trigger a server_error on the run)."""
    if not isinstance(s, str):
        s = str(s or "")
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = (
        s.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _reconstruct_email_from_run(run_name: str) -> dict | None:
    """Rebuild the email payload from a Logic App run's outputs — no workflow change
    needed. Accepts attachments of ANY format (read straight from the email)."""
    az = _az()
    sub = _sub()
    if not az or not sub:
        return None
    base = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
        f"{RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/{LOGIC_APP_NAME}/runs/{run_name}"
    )
    run_res = subprocess.run(
        [az, "rest", "--method", "get", "--url", f"{base}?api-version=2016-06-01"],
        capture_output=True, text=True, timeout=60, shell=False,
    )
    if run_res.returncode != 0:
        return None
    trig = json.loads(run_res.stdout or "{}").get("properties", {}).get("trigger", {})

    payload = {
        "subject": "(no subject)",
        "from": "",
        "bodyPreview": "",
        "body": "",
        "attachments": [],
        "attachmentBlobs": [],
    }

    # Primary source: the trigger outputs (the raw email) — ALL attachment formats.
    try:
        link = (trig.get("outputsLink") or {}).get("uri")
        if link:
            b = (_fetch_link_json(link) or {}).get("body", {}) or {}
            payload["subject"] = b.get("Subject") or b.get("subject") or payload["subject"]
            frm = b.get("From") or b.get("from") or {}
            if isinstance(frm, dict):
                frm = frm.get("emailAddress", {}).get("address") or frm.get("address") or ""
            payload["from"] = frm
            payload["bodyPreview"] = b.get("BodyPreview") or b.get("bodyPreview") or ""
            body = b.get("Body") or b.get("body") or payload["bodyPreview"]
            if isinstance(body, dict):
                body = body.get("content") or body.get("Content") or ""
            payload["body"] = body
            for att in (b.get("Attachments") or b.get("attachments") or []):
                if isinstance(att, dict):
                    name = att.get("Name") or att.get("name")
                    ctype = att.get("ContentType") or att.get("contentType")
                    if name:
                        payload["attachments"].append({"name": name, "contentType": ctype})
    except Exception:  # noqa: BLE001
        pass

    # Best-effort enhancement: the composed payload (exact body + saved blob paths).
    try:
        act_res = subprocess.run(
            [az, "rest", "--method", "get", "--url", f"{base}/actions?api-version=2016-06-01"],
            capture_output=True, text=True, timeout=60, shell=False,
        )
        if act_res.returncode == 0:
            for a in json.loads(act_res.stdout or "{}").get("value", []):
                if a.get("name") == "Compose_payload":
                    olink = (a.get("properties", {}).get("outputsLink") or {}).get("uri")
                    if olink:
                        raw = _fetch_link_json(olink)
                        cval = raw
                        if isinstance(raw, dict) and "subject" not in raw and isinstance(raw.get("body"), dict):
                            cval = raw["body"]
                        if isinstance(cval, dict) and "subject" in cval:
                            payload["body"] = cval.get("body") or payload["body"]
                            payload["attachmentBlobs"] = cval.get("attachmentBlobs") or payload["attachmentBlobs"]
                    break
    except Exception:  # noqa: BLE001
        pass

    # Keep the classifier input small & clean (full HTML threads cause server_error).
    payload["bodyPreview"] = _clean_text(payload["bodyPreview"], 600)
    payload["body"] = _clean_text(payload["body"] or payload["bodyPreview"], 4000)
    return payload


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/agents")
def api_agents():
    out = []
    for a in client().list_agents():
        d = a.as_dict()
        tools = [t.get("type") for t in d.get("tools", [])]
        connected = [
            t.get("connected_agent", {}).get("name")
            for t in d.get("tools", [])
            if t.get("type") == "connected_agent"
        ]
        out.append(
            {
                "id": a.id,
                "name": a.name,
                "model": d.get("model"),
                "tools": tools,
                "connected": [c for c in connected if c],
            }
        )
    return {"agents": out}


@app.get("/api/samples")
def api_samples():
    return {"samples": {k: v["subject"] for k, v in SAMPLES.items()}}


@app.get("/api/logicapp/runs")
def api_logicapp_runs():
    """Recent real Logic App runs (the actual email triggers)."""
    try:
        az = _az()
        if not az:
            return JSONResponse({"runs": [], "error": "Azure CLI (az) not found on PATH."})
        if not _sub():
            return JSONResponse({"runs": [], "error": "Not logged in (az login)."})
        return {"runs": _list_logicapp_runs(10)}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"runs": [], "error": str(exc)[:300]})


@app.get("/api/logicapp/runs/{run_name}/actions")
def api_logicapp_run_actions(run_name: str):
    """Action-level trace of one real Logic App run (the actual email flow)."""
    if not re.fullmatch(r"[A-Za-z0-9]+", run_name or ""):
        return JSONResponse({"error": "invalid run name"}, status_code=400)
    try:
        az = _az()
        if not az:
            return JSONResponse({"error": "Azure CLI (az) not found on PATH."})
        sub = subprocess.run(
            [az, "account", "show", "--query", "id", "-o", "tsv"],
            capture_output=True, text=True, timeout=30, shell=False,
        ).stdout.strip()
        if not sub:
            return JSONResponse({"error": "Not logged in (az login)."})
        base = (
            f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
            f"{RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/{LOGIC_APP_NAME}/"
            f"runs/{run_name}"
        )
        run_res = subprocess.run(
            [az, "rest", "--method", "get", "--url", f"{base}?api-version=2016-06-01"],
            capture_output=True, text=True, timeout=60, shell=False,
        )
        if run_res.returncode != 0:
            return JSONResponse({"error": run_res.stderr.strip()[:300]})
        run = json.loads(run_res.stdout or "{}").get("properties", {})
        trig = run.get("trigger", {})

        # best-effort: read the trigger outputs to surface the email subject/from
        email = {}
        try:
            link = (trig.get("outputsLink") or {}).get("uri")
            if link:
                with urllib.request.urlopen(link, timeout=10) as resp:  # noqa: S310
                    out = json.loads(resp.read().decode("utf-8"))
                b = out.get("body", out) or {}
                email = {
                    "subject": b.get("Subject") or b.get("subject"),
                    "from": (b.get("From") or b.get("from") or {}),
                    "bodyPreview": b.get("BodyPreview") or b.get("bodyPreview"),
                }
                if isinstance(email["from"], dict):
                    email["from"] = (
                        email["from"].get("emailAddress", {}).get("address")
                        or email["from"].get("address")
                    )
        except Exception:  # noqa: BLE001
            pass

        actions = []
        act_res = subprocess.run(
            [az, "rest", "--method", "get", "--url", f"{base}/actions?api-version=2016-06-01"],
            capture_output=True, text=True, timeout=60, shell=False,
        )
        if act_res.returncode == 0:
            for a in json.loads(act_res.stdout or "{}").get("value", []):
                p = a.get("properties", {})
                actions.append(
                    {
                        "name": a.get("name"),
                        "status": p.get("status"),
                        "start": p.get("startTime"),
                        "end": p.get("endTime"),
                        "code": p.get("code"),
                    }
                )
            actions.sort(key=lambda x: x.get("start") or "")

        return {
            "runName": run_name,
            "status": run.get("status"),
            "trigger": {"status": trig.get("status"), "start": trig.get("startTime")},
            "email": email,
            "actions": actions,
        }
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)[:300]})


@app.get("/api/logicapp/runs/{run_name}/trace")
def api_logicapp_run_trace(run_name: str):
    """Return the saved end-to-end agent trace for a run (from disk), so the UI can
    show the full flow — Logic App ingest + agent processing — without re-running."""
    if not re.fullmatch(r"[A-Za-z0-9]+", run_name or ""):
        return JSONResponse({"error": "invalid run name"}, status_code=400)
    t = _load_trace(run_name)
    if not t:
        return {"exists": False, "events": []}
    return {
        "exists": True,
        "runName": run_name,
        "savedAt": t.get("ts"),
        "events": t.get("events", []),
    }


@app.get("/api/stream")
def api_stream(
    sample: str = Query("contract"),
    text: str | None = Query(None),
    subject: str | None = Query(None),
):
    """Server-Sent-Events stream of a single orchestrator run, live."""

    def gen():
        # 1) Resolve the email payload ------------------------------------- #
        if text or subject:
            payload = {
                "subject": (subject or "").strip() or "(no subject)",
                "from": "you",
                "body": (text or "").strip(),
            }
        else:
            payload = SAMPLES.get(sample, SAMPLES["contract"])

        yield _sse(
            {
                "type": "email",
                "subject": payload.get("subject"),
                "from": payload.get("from"),
                "body": payload.get("body"),
                "attachments": payload.get("attachmentBlobs", []),
                "ts": time.time(),
            }
        )

        try:
            yield from _orchestrate(client(), json.dumps(payload), source="ui")
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "message": str(exc)[:400]})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/events/stream")
def api_events_stream(run: str = Query(...)):
    """Stream the FULL live agent routing trace for a REAL email — reconstructed
    from a Logic App run's outputs (no workflow change, any attachment format)."""
    if not re.fullmatch(r"[A-Za-z0-9]+", run or ""):
        return JSONResponse({"error": "invalid run name"}, status_code=400)
    return StreamingResponse(_iter_process_run(run), media_type="text/event-stream")


@app.get("/api/overview")
def api_overview(days: int = Query(14)):
    """Date-wise counts of processed emails by intent (for the Overview tab)."""
    rows: list[dict] = []
    try:
        if PROCESSED_LOG.exists():
            for line in PROCESSED_LOG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    intents = ["contract_note", "pre_onboarding", "manual"]
    by_date: dict[str, dict] = {}
    for r in rows:
        d = r.get("date") or ""
        bucket = by_date.setdefault(
            d,
            {"date": d, "total": 0, "files": 0, "contract_note": 0, "pre_onboarding": 0, "manual": 0},
        )
        it = r.get("intent")
        if it not in intents:
            it = "manual"
        bucket[it] += 1
        bucket["total"] += 1
        bucket["files"] += int(r.get("files") or 0)

    days_list = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)[:days]
    totals = {
        "total": sum(b["total"] for b in by_date.values()),
        "files": sum(b["files"] for b in by_date.values()),
        "contract_note": sum(b["contract_note"] for b in by_date.values()),
        "pre_onboarding": sum(b["pre_onboarding"] for b in by_date.values()),
        "manual": sum(b["manual"] for b in by_date.values()),
    }
    return {"days": days_list, "totals": totals, "labels": INTENT_LABEL}


@app.post("/api/process")
async def api_process(request: Request):
    """Headless entrypoint (Design A): accept an email's attachment list + metadata,
    run the agentic orchestration to completion, and return a JSON summary. This is
    what a hosted deployment's Logic App posts to for fully-automatic processing."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    payload = {
        "subject": body.get("subject") or "(no subject)",
        "from": body.get("from") or "",
        "bodyPreview": _clean_text(body.get("bodyPreview") or "", 600),
        "body": _clean_text(body.get("body") or body.get("bodyPreview") or "", 4000),
        "attachmentBlobs": body.get("attachmentBlobs") or body.get("attachment_blobs") or [],
    }
    intent = None
    files: list = []
    status = "completed"
    error = None
    try:
        for chunk in _orchestrate(client(), json.dumps(payload), source="logicapp"):
            try:
                ev = json.loads(chunk[6:].strip())
            except Exception:  # noqa: BLE001
                continue
            t = ev.get("type")
            if t == "intent":
                intent = ev.get("intent")
            elif t == "output_written":
                files.append(ev.get("file"))
            elif t == "done":
                status = ev.get("status")
                error = ev.get("error")
            elif t == "error":
                error = ev.get("message")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "failed", "error": str(exc)[:400]}, status_code=500)
    return {"intent": intent, "filesWritten": files, "status": status, "error": error}


# --------------------------------------------------------------------------- #
# Static frontend
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
