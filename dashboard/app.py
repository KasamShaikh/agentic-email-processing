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
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import hitl
import maf_agents
import workflow

# --------------------------------------------------------------------------- #
# Configuration  (env-driven so the same code runs locally and on App Service;
# the defaults are the live values for local `az login` development)
# --------------------------------------------------------------------------- #
ENDPOINT = os.getenv(
    "FOUNDRY_PROJECT_ENDPOINT",
    "https://agentic-email-foundry-ks.services.ai.azure.com/api/projects/email-agentic-ks",
)
ORCHESTRATOR_NAME = os.getenv("ORCHESTRATOR_NAME", "orchestrator-ks")
RESOURCE_GROUP = os.getenv("RESOURCE_GROUP", "agentic-email-processing")
LOGIC_APP_NAME = os.getenv("LOGIC_APP_NAME", "logic-email-ks")

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


def _arm_get(url: str, timeout: int = 60) -> dict:
    """GET an Azure Resource Manager URL using the managed-identity / logged-in
    token — no `az` CLI required (App Service has no CLI on PATH)."""
    token = _credential.get_token("https://management.azure.com/.default").token
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


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
    sub = _sub()
    if not sub:
        return []
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
        f"{RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/{LOGIC_APP_NAME}/"
        "runs?api-version=2016-06-01"
    )
    try:
        data = _arm_get(url)
    except Exception:  # noqa: BLE001
        return []
    runs = []
    for r in data.get("value", [])[:top]:
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
        for chunk in _orchestrate(json.dumps(payload), source="logicapp"):
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


def _enable_tracing() -> None:
    """Best-effort: turn on MAF OpenTelemetry instrumentation (a span per agent run)
    when MAF_TRACING is set. The dashboard's own per-run event trace is always on."""
    if os.getenv("MAF_TRACING", "0").lower() not in ("1", "true", "yes"):
        return
    try:
        from agent_framework.observability import enable_instrumentation

        enable_instrumentation()
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
def _start_poller() -> None:
    _enable_tracing()
    maf_agents.warmup()
    if AUTO_PROCESS:
        threading.Thread(target=_poller_loop, name="autoprocess", daemon=True).start()


def _az() -> str | None:
    """Resolve the Azure CLI executable (az.cmd on Windows)."""
    return shutil.which("az") or shutil.which("az.cmd")


def _map_workflow_event(ev, state: dict) -> dict | None:
    """Map a MAF WorkflowEvent to the dashboard's SSE event dict (or None to skip).

    The orchestrator and specialist executors push the dashboard's own SSE dicts onto
    the workflow event stream (as `data` / `output` events), so the frontend contract
    is unchanged. Also accumulates a small run summary for the processed-emails log."""
    t = getattr(ev, "type", None)
    if t in ("data", "output"):
        d = getattr(ev, "data", None)
        if isinstance(d, dict) and d.get("type"):
            et = d["type"]
            if et == "intent" and d.get("intent"):
                state["intent"] = d["intent"]
            elif et == "agent_called" and d.get("name"):
                state["agents"].append(d["name"])
            elif et == "output_written":
                state["files"] += 1
            elif et == "done":
                if d.get("intent"):
                    state["intent"] = d["intent"]
                state["files"] = max(state["files"], int(d.get("filesCount") or 0))
            return d
        return None
    if t in ("error", "failed"):
        data = getattr(ev, "data", None)
        if isinstance(data, str):
            msg = data
        else:
            msg = str(getattr(ev, "details", None) or data or "workflow error")
        return {"type": "error", "message": msg[:400], "ts": time.time()}
    return None


def _orchestrate(payload_str: str, source: str = "ui"):
    """Yield SSE events for one email, end to end, by running the MAF **workflow**:

      orchestrator-ks (classify) -> hitl-gate (native `request_info` pause)
        -> switch-case routing -> specialist executor (deterministic pipeline) -> done.

    The human-in-the-loop gate PAUSES the workflow (a `request_info` event). This driver
    brokers the decision through `hitl.py` — blocking until a human decides from the
    dashboard, or an auto-decision fires after a timeout (short for headless emails, long
    for interactive UI runs) so nothing hangs forever — then RESUMES the workflow with the
    response. The exact SSE event contract the frontend expects is preserved. Reused by
    /api/stream (UI), /api/events/stream (real Logic App emails) and /api/process (headless).
    """
    import uuid as _uuid

    run_id = _uuid.uuid4().hex[:12]
    try:
        payload = json.loads(payload_str)
    except Exception:  # noqa: BLE001
        payload = {}
    payload_attachments = payload.get("attachmentBlobs") or []

    state = {"intent": None, "agents": [], "files": 0}

    yield _sse(
        {
            "type": "orchestrator_start",
            "agentId": run_id,
            "name": ORCHESTRATOR_NAME,
            "runtime": "MAF workflow (in-process)",
            "ts": time.time(),
        }
    )
    yield _sse({"type": "status", "status": "running", "ts": time.time()})

    wf = workflow.build_workflow()

    # 1) Run the workflow until it PAUSES at the human-in-the-loop gate (request_info).
    pending = None
    try:
        for ev in workflow.start_stream(wf, payload_str, source):
            if getattr(ev, "type", None) == "request_info":
                pending = (ev.request_id, ev.data)
                continue
            sse = _map_workflow_event(ev, state)
            if sse:
                yield _sse(sse)
    except Exception as exc:  # noqa: BLE001
        yield _sse({"type": "error", "message": str(exc)[:400]})

    if pending is None:
        # No approval gate was reached (unexpected) — record what we have and stop.
        _record(state["intent"], state["agents"], payload_attachments,
                list(range(state["files"])), source)
        return

    request_id, req = pending
    req = req if isinstance(req, dict) else {}

    # 2) Open the HITL review and BLOCK until a human decides.
    review = hitl.open_review(
        run_id,
        req.get("intent"),
        req.get("title"),
        req.get("summary"),
        req.get("details"),
        req.get("level"),
    )
    yield _sse(
        {
            "type": "awaiting_review",
            "reviewId": review["id"],
            "level": review["level"],
            "levelLabel": review["levelLabel"],
            "levelBlurb": review["levelBlurb"],
            "title": req.get("title"),
            "summary": req.get("summary"),
            "details": req.get("details"),
            "intent": req.get("intent"),
            "ts": time.time(),
        }
    )
    timeout = hitl.TIMEOUT_HEADLESS if source == "logicapp" else hitl.TIMEOUT_UI
    decided = hitl.wait_for_decision(review["id"], timeout)
    dec = decided.get("decision") or {}
    yield _sse(
        {
            "type": "review_decided",
            "reviewId": review["id"],
            "state": decided.get("state"),
            "action": dec.get("action"),
            "by": dec.get("by"),
            "note": dec.get("note"),
            "editedIntent": dec.get("editedIntent"),
            "ts": time.time(),
        }
    )

    # 3) RESUME the workflow with the human decision; stream the rest (pipeline + done).
    response = {
        "action": dec.get("action"),
        "by": dec.get("by"),
        "note": dec.get("note"),
        "editedIntent": dec.get("editedIntent"),
        "state": decided.get("state"),
    }
    try:
        for ev in workflow.resume_stream(wf, {request_id: response}):
            sse = _map_workflow_event(ev, state)
            if sse:
                yield _sse(sse)
    except Exception as exc:  # noqa: BLE001
        yield _sse({"type": "error", "message": str(exc)[:400]})

    _record(state["intent"], state["agents"], payload_attachments,
            list(range(state["files"])), source)






def _sub() -> str | None:
    env = os.getenv("AZURE_SUBSCRIPTION_ID")
    if env:
        return env
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
    sub = _sub()
    if not sub:
        return None
    base = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
        f"{RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/{LOGIC_APP_NAME}/runs/{run_name}"
    )
    try:
        run_props = _arm_get(f"{base}?api-version=2016-06-01")
    except Exception:  # noqa: BLE001
        return None
    trig = run_props.get("properties", {}).get("trigger", {})

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
        act_data = _arm_get(f"{base}/actions?api-version=2016-06-01")
        for a in act_data.get("value", []):
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
    return {"agents": maf_agents.list_agents()}


@app.get("/api/samples")
def api_samples():
    return {"samples": {k: v["subject"] for k, v in SAMPLES.items()}}


# --------------------------------------------------------------------------- #
# Human-in-the-loop review queue
# --------------------------------------------------------------------------- #
@app.get("/api/hitl/queue")
def api_hitl_queue():
    """Items currently awaiting a human decision (the live review queue)."""
    return {"awaiting": hitl.list_reviews("awaiting")}


@app.get("/api/hitl/history")
def api_hitl_history():
    """Recently decided review items (approved/rejected), newest first."""
    return {"history": hitl.history()}


@app.post("/api/hitl/decision")
async def api_hitl_decision(request: Request):
    """Record a human decision (approve | reject | edit) — unblocks the waiting run."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    rec = hitl.decide(
        (body.get("reviewId") or "").strip(),
        (body.get("action") or "").strip(),
        by=(body.get("by") or "reviewer").strip(),
        note=(body.get("note") or "").strip(),
        edited_intent=body.get("editedIntent"),
    )
    if not rec:
        return JSONResponse({"error": "unknown review id or already decided"}, status_code=400)
    return {"review": rec}


@app.get("/api/logicapp/runs")
def api_logicapp_runs():
    """Recent real Logic App runs (the actual email triggers)."""
    try:
        if not _sub():
            return JSONResponse({"runs": [], "error": "AZURE_SUBSCRIPTION_ID not set (or not logged in via az)."})
        return {"runs": _list_logicapp_runs(10)}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"runs": [], "error": str(exc)[:300]})


@app.get("/api/logicapp/runs/{run_name}/actions")
def api_logicapp_run_actions(run_name: str):
    """Action-level trace of one real Logic App run (the actual email flow)."""
    if not re.fullmatch(r"[A-Za-z0-9]+", run_name or ""):
        return JSONResponse({"error": "invalid run name"}, status_code=400)
    try:
        sub = _sub()
        if not sub:
            return JSONResponse({"error": "AZURE_SUBSCRIPTION_ID not set (or not logged in via az)."})
        base = (
            f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
            f"{RESOURCE_GROUP}/providers/Microsoft.Logic/workflows/{LOGIC_APP_NAME}/"
            f"runs/{run_name}"
        )
        try:
            run = _arm_get(f"{base}?api-version=2016-06-01").get("properties", {})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:300]})
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
        try:
            act_data = _arm_get(f"{base}/actions?api-version=2016-06-01")
        except Exception:  # noqa: BLE001
            act_data = {}
        for a in act_data.get("value", []):
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
            yield from _orchestrate(json.dumps(payload), source="ui")
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
        for chunk in _orchestrate(json.dumps(payload), source="logicapp"):
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
