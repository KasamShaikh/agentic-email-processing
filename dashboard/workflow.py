"""
Microsoft Agent Framework (MAF) **Workflow** for one email, end to end.

This is the genuinely-agentic core: instead of hard-coded Python `if intent == ...`
routing, the flow is a MAF **graph of executors** wired with conditional edges, with
a **native human-in-the-loop gate** (`ctx.request_info(...)` / `@response_handler`)
and **durable checkpointing** so a paused run can be resumed.

Graph::

    orchestrator-ks                 (classify the email -> intent)
          |
        hitl-gate                   (request_info: pause for a human decision)
          |  switch-case on intent
          +---------------------------+----------------------------+
          v                           v                            v
    contract-note-ks            form-compare-ks           manual-intervention-ks
    (PIS pipeline)              (onboarding pipeline)      (LLM reply)

The deterministic pipelines (`contract_pipeline`, `onboarding_pipeline`) are kept
intact and run *inside* the specialist executors. Every step each executor takes is
pushed onto the workflow event stream as a `data` event carrying the exact same SSE
dict the dashboard already understands, so the frontend contract is unchanged.

`app.py` drives the workflow through the small sync bridge at the bottom of this file
(`start_stream` / `resume_stream`), forwarding events as SSE and brokering the human
decision through `hitl.py`.
"""

import asyncio
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Never

from agent_framework import (
    Case,
    Default,
    Executor,
    FileCheckpointStorage,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowEvent,
    handler,
    response_handler,
)

import maf_agents

# intent -> human-readable label (kept local so this module has no app.py dependency)
INTENT_LABEL = {
    "contract_note": "Contract Note",
    "pre_onboarding": "Merchant Pre-Onboarding",
    "manual": "Manual Intervention",
}
# intent -> human-in-the-loop gate level (reversibility x risk)
LEVEL_BY_INTENT = {"contract_note": 1, "pre_onboarding": 2, "manual": 3}

DATA_DIR = Path(__file__).parent / "data"
CHECKPOINT_DIR = DATA_DIR / "checkpoints"

# Per-event wait when draining the async workflow from sync code (a single pipeline
# step — e.g. a Document Intelligence call — can take a while).
_STEP_TIMEOUT = float(os.getenv("WF_STEP_TIMEOUT", "600"))

_SENTINEL = object()


# --------------------------------------------------------------------------- #
# Helpers shared by the specialist executors
# --------------------------------------------------------------------------- #
def _payload(c: dict) -> dict:
    try:
        return json.loads(c.get("payload_str") or "{}")
    except Exception:  # noqa: BLE001
        return {}


async def _run_pipeline(executor_id: str, ctx: WorkflowContext, gen_factory) -> tuple[list, list]:
    """Run a deterministic pipeline generator (sync) in a worker thread, forwarding
    every yielded SSE dict onto the workflow event stream as a `data` event, and
    return its (files, warnings).

    The pipeline's `run_agent` callable hits the agents through the shared MAF loop;
    running the generator in a *thread* (not on the loop) keeps that from deadlocking.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def worker() -> None:
        try:
            for ev in gen_factory():
                loop.call_soon_threadsafe(queue.put_nowait, ev)
        except Exception as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(
                queue.put_nowait, {"type": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"}
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    threading.Thread(target=worker, name="pipeline", daemon=True).start()

    files: list = []
    warnings: list = []
    while True:
        ev = await queue.get()
        if ev is _SENTINEL:
            break
        await ctx.add_event(WorkflowEvent.emit(executor_id, ev))
        if isinstance(ev, dict) and ev.get("type") in ("contract_done", "onboarding_done"):
            files = ev.get("files", []) or []
            warnings = ev.get("warnings", []) or []
    return files, warnings


def _done(intent: str, agents: list[str], results: list[dict], files: list) -> dict:
    return {
        "type": "done",
        "status": "completed",
        "intent": intent,
        "agentsCalled": agents,
        "finalMessage": json.dumps(
            {"intent": intent, "delegated_to": agents, "results": results}, indent=2
        ),
        "error": None,
        "filesCount": len(files or []),
        "ts": time.time(),
    }


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #
class OrchestratorExecutor(Executor):
    """Classifies the email with the orchestrator agent, then forwards the intent."""

    def __init__(self) -> None:
        super().__init__(id="orchestrator-ks")

    @handler
    async def classify(self, msg: dict, ctx: WorkflowContext[dict]) -> None:
        payload_str = msg.get("payload_str") or "{}"
        source = msg.get("source") or "ui"
        cls = await maf_agents.aclassify(payload_str)
        intent = cls.get("intent") or "manual"
        reason = cls.get("reason") or ""
        await ctx.add_event(
            WorkflowEvent.emit(
                self.id,
                {
                    "type": "intent",
                    "intent": intent,
                    "label": INTENT_LABEL.get(intent, intent),
                    "reason": reason,
                    "ts": time.time(),
                },
            )
        )
        await ctx.send_message(
            {
                "kind": "classification",
                "intent": intent,
                "reason": reason,
                "payload_str": payload_str,
                "source": source,
            }
        )


class ApprovalExecutor(Executor):
    """The native human-in-the-loop gate. Emits a `request_info` event (pausing the
    workflow), then on the human's decision either ends the run (reject) or routes the
    (possibly re-classified) email on to a specialist (approve / edit)."""

    def __init__(self) -> None:
        super().__init__(id="hitl-gate")

    @handler
    async def gate(self, c: dict, ctx: WorkflowContext[dict, dict]) -> None:
        intent = c.get("intent") or "manual"
        payload = _payload(c)
        attachments = payload.get("attachmentBlobs") or []
        level = LEVEL_BY_INTENT.get(intent, 3)
        if intent == "contract_note":
            title = "Contract note — approve PIS file generation"
            details = {
                "action": "Generate the PIS upload file(s) and write them to the "
                "contract-notes-output blob container.",
                "attachments": [b.rsplit("/", 1)[-1] for b in attachments],
                "risk": "Irreversible regulatory output.",
            }
        elif intent == "pre_onboarding":
            title = "Merchant onboarding — approve verification"
            details = {
                "action": "Verify the two onboarding forms (web-UI vs handwritten) and "
                "write the verification report.",
                "risk": "Maker review of the onboarding verdict.",
            }
        else:
            title = "Manual / unclassified — human decision"
            details = {
                "action": "No automated action. Route this email to a human.",
                "subject": payload.get("subject"),
                "from": payload.get("from"),
            }
        request = {
            "kind": "approval_request",
            "intent": intent,
            "title": title,
            "summary": INTENT_LABEL.get(intent, intent),
            "details": details,
            "level": level,
            "payload_str": c.get("payload_str") or "{}",
            "source": c.get("source") or "ui",
            "reason": c.get("reason") or "",
        }
        await ctx.request_info(request, dict)

    @response_handler
    async def on_decision(self, request: dict, response: dict, ctx: WorkflowContext[dict, dict]) -> None:
        intent = request.get("intent") or "manual"
        payload_str = request.get("payload_str") or "{}"
        source = request.get("source") or "ui"
        action = (response.get("action") or "").lower()
        state = response.get("state") or ("rejected" if action == "reject" else "approved")

        if state == "rejected":
            await ctx.yield_output(
                {
                    "type": "done",
                    "status": "rejected",
                    "intent": intent,
                    "agentsCalled": [],
                    "finalMessage": json.dumps(
                        {
                            "intent": intent,
                            "decision": "rejected",
                            "by": response.get("by"),
                            "note": response.get("note"),
                        },
                        indent=2,
                    ),
                    "error": None,
                    "filesCount": 0,
                    "ts": time.time(),
                }
            )
            return

        # A human can re-route the email (edit) instead of approving the classified intent.
        if action == "edit" and response.get("editedIntent") in INTENT_LABEL:
            intent = response["editedIntent"]
            await ctx.add_event(
                WorkflowEvent.emit(
                    self.id,
                    {
                        "type": "intent",
                        "intent": intent,
                        "label": INTENT_LABEL.get(intent, intent),
                        "reason": "human-edited routing",
                        "ts": time.time(),
                    },
                )
            )

        await ctx.send_message(
            {"kind": "approved", "intent": intent, "payload_str": payload_str, "source": source}
        )


class ContractExecutor(Executor):
    """Runs the deterministic contract-note -> PIS pipeline."""

    def __init__(self) -> None:
        super().__init__(id="contract-note-ks")

    @handler
    async def run(self, c: dict, ctx: WorkflowContext[Never, dict]) -> None:
        attachments = _payload(c).get("attachmentBlobs") or []
        await ctx.add_event(
            WorkflowEvent.emit(self.id, {"type": "agent_called", "name": self.id, "ts": time.time()})
        )
        from contract_pipeline import process as _process

        files, warnings = await _run_pipeline(
            self.id, ctx, lambda: _process(attachments, lambda x: maf_agents.run_agent(self.id, x))
        )
        results = [{"agent": self.id, "files": files, "warnings": warnings}]
        await ctx.yield_output(_done("contract_note", [self.id], results, files))


class OnboardingExecutor(Executor):
    """Runs the deterministic merchant-onboarding form-verification pipeline."""

    def __init__(self) -> None:
        super().__init__(id="form-compare-ks")

    @handler
    async def run(self, c: dict, ctx: WorkflowContext[Never, dict]) -> None:
        attachments = _payload(c).get("attachmentBlobs") or []
        from onboarding_pipeline import demo_attachments, process as _process

        try:
            demo = await asyncio.to_thread(demo_attachments)
        except Exception:  # noqa: BLE001
            demo = []
        blobs = demo or attachments
        await ctx.add_event(
            WorkflowEvent.emit(
                self.id,
                {
                    "type": "onboarding_source",
                    "usingDemo": bool(demo),
                    "files": [b.rsplit("/", 1)[-1] for b in blobs],
                    "ts": time.time(),
                },
            )
        )
        await ctx.add_event(
            WorkflowEvent.emit(self.id, {"type": "agent_called", "name": self.id, "ts": time.time()})
        )
        files, warnings = await _run_pipeline(
            self.id, ctx, lambda: _process(blobs, lambda x: maf_agents.run_agent(self.id, x))
        )
        results = [{"agent": self.id, "files": files, "warnings": warnings}]
        await ctx.yield_output(_done("pre_onboarding", [self.id], results, files))


class ManualExecutor(Executor):
    """Routes unclassified / exception email to the manual-intervention agent."""

    def __init__(self) -> None:
        super().__init__(id="manual-intervention-ks")

    @handler
    async def run(self, c: dict, ctx: WorkflowContext[Never, dict]) -> None:
        payload_str = c.get("payload_str") or "{}"
        await ctx.add_event(
            WorkflowEvent.emit(self.id, {"type": "agent_called", "name": self.id, "ts": time.time()})
        )
        spec = await maf_agents.arun_agent(self.id, payload_str)
        await ctx.add_event(
            WorkflowEvent.emit(
                self.id,
                {
                    "type": "result",
                    "agent": self.id,
                    "status": spec["status"],
                    "text": spec["text"],
                    "error": spec["error"],
                    "ts": time.time(),
                },
            )
        )
        results = [{"agent": self.id, "result": spec["text"]}]
        await ctx.yield_output(_done("manual", [self.id], results, []))


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def _checkpoint_storage() -> FileCheckpointStorage | None:
    """Durable, file-backed checkpoint storage so a paused run survives a restart.
    Disable with WF_CHECKPOINT=0. Best-effort — falls back to no checkpointing."""
    if os.getenv("WF_CHECKPOINT", "1").lower() in ("0", "false", "no"):
        return None
    try:
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        return FileCheckpointStorage(str(CHECKPOINT_DIR))
    except Exception:  # noqa: BLE001
        return None


def build_workflow():
    """Build a fresh MAF Workflow instance (one per email run)."""
    orchestrator = OrchestratorExecutor()
    gate = ApprovalExecutor()
    contract = ContractExecutor()
    onboarding = OnboardingExecutor()
    manual = ManualExecutor()

    builder = WorkflowBuilder(
        start_executor=orchestrator,
        name="email-processing",
        checkpoint_storage=_checkpoint_storage(),
    )
    builder.add_edge(orchestrator, gate)
    builder.add_switch_case_edge_group(
        gate,
        [
            Case(
                condition=lambda d: isinstance(d, dict) and d.get("intent") == "contract_note",
                target=contract,
            ),
            Case(
                condition=lambda d: isinstance(d, dict) and d.get("intent") == "pre_onboarding",
                target=onboarding,
            ),
            Default(target=manual),
        ],
    )
    return builder.build()


# --------------------------------------------------------------------------- #
# Sync bridge — drive the async workflow from app.py's synchronous SSE generators,
# on the shared MAF event loop. Used by app.py to stream events and resume after the
# human-in-the-loop gate.
#
# The whole async iteration runs as ONE coroutine/task on the loop (so OpenTelemetry
# span contexts open and close in the same task), feeding a thread-safe queue that the
# caller's synchronous generator drains.
# --------------------------------------------------------------------------- #
def _drain(stream) -> Iterator[WorkflowEvent]:
    loop = maf_agents._ensure_loop()
    bridge: queue.Queue = queue.Queue()

    async def _driver() -> None:
        try:
            async for ev in stream:
                bridge.put(("event", ev))
        except Exception as exc:  # noqa: BLE001
            bridge.put(("error", exc))
        finally:
            bridge.put(("end", None))

    asyncio.run_coroutine_threadsafe(_driver(), loop)
    while True:
        kind, value = bridge.get(timeout=_STEP_TIMEOUT)
        if kind == "end":
            return
        if kind == "error":
            raise value
        yield value


def start_stream(wf, payload_str: str, source: str) -> Iterator[WorkflowEvent]:
    """Start a workflow run; yields WorkflowEvents until it completes or PAUSES at the
    human-in-the-loop gate (a `request_info` event, after which the stream ends)."""
    message = {"kind": "email", "payload_str": payload_str, "source": source}
    return _drain(wf.run(message, stream=True))


def resume_stream(wf, responses: dict[str, Any]) -> Iterator[WorkflowEvent]:
    """Resume a paused workflow with the human decision(s) (`{request_id: response}`)."""
    return _drain(wf.run(responses=responses, stream=True))
