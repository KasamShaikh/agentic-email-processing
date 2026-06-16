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
import shutil
import subprocess
import time
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


@app.get("/api/stream")
def api_stream(
    sample: str = Query("contract"),
    text: str | None = Query(None),
):
    """Server-Sent-Events stream of a single orchestrator run, live."""

    def gen():
        # 1) Resolve the email payload ------------------------------------- #
        if text:
            payload = {"subject": "(custom)", "from": "you", "body": text}
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
            orchestrator_id = next(
                (aid for aid, n in id_to_name.items() if n == ORCHESTRATOR_NAME), None
            )
            if not orchestrator_id:
                yield _sse({"type": "error", "message": "orchestrator-ks not found"})
                return

            yield _sse(
                {
                    "type": "orchestrator_start",
                    "agentId": orchestrator_id,
                    "name": ORCHESTRATOR_NAME,
                    "ts": time.time(),
                }
            )

            thread = ag.threads.create()
            ag.messages.create(
                thread_id=thread.id, role="user", content=json.dumps(payload)
            )
            run = ag.runs.create(thread_id=thread.id, agent_id=orchestrator_id)
            yield _sse(
                {
                    "type": "run_created",
                    "threadId": thread.id,
                    "runId": run.id,
                    "status": str(run.status),
                    "ts": time.time(),
                }
            )

            # 2) Poll until terminal, streaming new steps ------------------ #
            seen_steps: set[str] = set()
            agents_called: list[str] = []
            terminal = {"completed", "failed", "cancelled", "expired"}
            for _ in range(120):  # ~2 min max
                run = ag.runs.get(thread_id=thread.id, run_id=run.id)
                status = str(run.status).split(".")[-1].lower()
                yield _sse({"type": "status", "status": status, "ts": time.time()})

                for step in ag.run_steps.list(thread_id=thread.id, run_id=run.id):
                    if step.id in seen_steps:
                        continue
                    seen_steps.add(step.id)
                    d = step.as_dict()
                    detected = _detect_agents(d, id_to_name)
                    for name in detected:
                        if name not in agents_called:
                            agents_called.append(name)
                            yield _sse(
                                {"type": "agent_called", "name": name, "ts": time.time()}
                            )
                    yield _sse(
                        {
                            "type": "step",
                            "stepType": d.get("type"),
                            "status": d.get("status"),
                            "agents": detected,
                            "ts": time.time(),
                        }
                    )

                if status in terminal:
                    break
                time.sleep(1)

            # 3) Final answer --------------------------------------------- #
            final_text = ""
            try:
                for msg in ag.messages.list(thread_id=thread.id):
                    md = msg.as_dict()
                    if md.get("role") == "assistant":
                        parts = [
                            c.get("text", {}).get("value", "")
                            for c in md.get("content", [])
                            if c.get("type") == "text"
                        ]
                        final_text = "\n".join(p for p in parts if p).strip()
                        if final_text:
                            break
            except Exception:  # noqa: BLE001
                pass

            error = None
            if getattr(run, "last_error", None):
                le = run.last_error
                error = le.as_dict() if hasattr(le, "as_dict") else str(le)

            yield _sse(
                {
                    "type": "done",
                    "status": status,
                    "agentsCalled": agents_called,
                    "finalMessage": final_text,
                    "error": error,
                    "threadId": thread.id,
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
