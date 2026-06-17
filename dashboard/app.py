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
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.ai.agents import AgentsClient
from fastapi import FastAPI, Query
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
        "subject": "Contract Note - Trade Confirmation 17-Jun-2026",
        "from": "broker@brokerage.example.com",
        "bodyPreview": "Please find attached your contract note for the trade executed today.",
        "body": "Dear Client, please find attached the contract note for your trade "
        "(ISIN INE002A01018, 100 shares). Regards, Broker Ops.",
        "attachmentBlobs": ["incoming-attachments/contract-note-cn123.pdf"],
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
        sub = subprocess.run(
            [az, "account", "show", "--query", "id", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        ).stdout.strip()
        if not sub:
            return JSONResponse({"runs": [], "error": "Not logged in (az login)."})
        url = (
            f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
            f"{RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/{LOGIC_APP_NAME}/"
            "runs?api-version=2016-06-01"
        )
        res = subprocess.run(
            [az, "rest", "--method", "get", "--url", url],
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )
        if res.returncode != 0:
            return JSONResponse({"runs": [], "error": res.stderr.strip()[:300]})
        data = json.loads(res.stdout or "{}")
        runs = []
        for r in data.get("value", [])[:10]:
            p = r.get("properties", {})
            runs.append(
                {
                    "name": r.get("name"),
                    "status": p.get("status"),
                    "startTime": p.get("startTime"),
                    "endTime": p.get("endTime"),
                }
            )
        return {"runs": runs}
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
            ag = client()
            id_to_name = _agents_index()
            name_to_id = {n: i for i, n in id_to_name.items()}
            orchestrator_id = name_to_id.get(ORCHESTRATOR_NAME)
            if not orchestrator_id:
                yield _sse({"type": "error", "message": "orchestrator-ks not found"})
                return

            payload_str = json.dumps(payload)
            agents_called: list[str] = []

            # 2) Orchestrator = intent classifier ------------------------- #
            yield _sse(
                {
                    "type": "orchestrator_start",
                    "agentId": orchestrator_id,
                    "name": ORCHESTRATOR_NAME,
                    "ts": time.time(),
                }
            )
            yield _sse({"type": "status", "status": "running", "ts": time.time()})
            orc = _run_agent(ag, orchestrator_id, payload_str)
            yield _sse(
                {
                    "type": "run_created",
                    "threadId": orc["threadId"],
                    "runId": orc["runId"],
                    "status": orc["status"],
                    "ts": time.time(),
                }
            )
            if orc["status"] != "completed":
                yield _sse(
                    {
                        "type": "done",
                        "status": orc["status"],
                        "agentsCalled": [],
                        "finalMessage": "",
                        "error": orc["error"],
                        "threadId": orc["threadId"],
                        "ts": time.time(),
                    }
                )
                return

            intent, reason = _parse_intent(orc["text"])
            yield _sse(
                {
                    "type": "intent",
                    "intent": intent,
                    "label": INTENT_LABEL.get(intent, intent),
                    "reason": reason,
                    "ts": time.time(),
                }
            )

            # 3) Route to the matching specialist (in code) --------------- #
            results: list[dict] = []
            target = INTENT_TO_AGENT[intent]
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

            # 3b) Pre-onboarding chains to form-verification -------------- #
            if intent == "pre_onboarding" and spec["status"] == "completed":
                fv = "form-verification-ks"
                yield _sse({"type": "agent_called", "name": fv, "ts": time.time()})
                agents_called.append(fv)
                fv_input = spec["text"] or payload_str
                fvr = _run_agent(ag, name_to_id[fv], fv_input)
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

            # 4) Final summary -------------------------------------------- #
            final = {
                "intent": intent,
                "delegated_to": agents_called,
                "results": results,
            }
            yield _sse(
                {
                    "type": "done",
                    "status": "completed",
                    "intent": intent,
                    "agentsCalled": agents_called,
                    "finalMessage": json.dumps(final, indent=2),
                    "error": None,
                    "threadId": orc["threadId"],
                    "ts": time.time(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "message": str(exc)[:400]})

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Static frontend
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
