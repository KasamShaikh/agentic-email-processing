"""
In-process Microsoft Agent Framework (MAF) agents.

This replaces the previous Foundry *remote* agents (`azure.ai.agents.AgentsClient`)
with in-process MAF `Agent` objects that talk straight to the Azure OpenAI
deployment on the Foundry account. Each agent's behaviour is its instruction prompt,
loaded from the shared `agents/*.md` files (single source of truth, identical to the
prompts the remote agents used).

The rest of the app calls two things only:

    run_agent(agent_name, content) -> {"status", "text", "error"}
    classify(payload_json_str)     -> {"intent", "reason"}

so the existing pipelines (`contract_pipeline`, `onboarding_pipeline`) keep working
unchanged — we just swap *what* the `run_agent` callable does.

Auth uses `DefaultAzureCredential` (your `az login` locally, managed identity in
Azure). No keys are read or written.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from agent_framework import Agent, tool
from agent_framework.azure import AzureOpenAIChatClient

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# Azure OpenAI endpoint of the Foundry (AI Services) account + the chat deployment.
AOAI_ENDPOINT = os.getenv(
    "AZURE_OPENAI_ENDPOINT",
    "https://agentic-email-foundry-ks.cognitiveservices.azure.com/",
)
AOAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-mini-ks")
AOAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")


def _resolve_agents_dir() -> Path:
    """Locate the `agents/` prompt folder.

    In the repo / local layout the prompts live one level up (`../agents`). When only
    the `dashboard/` folder is published as the app root (e.g. Azure App Service), a
    sibling copy is bundled at `./agents`. An explicit `AGENTS_DIR` env var always wins.
    """
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    env = os.getenv("AGENTS_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [here.parent / "agents", here / "agents"]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return here.parent / "agents"


AGENTS_DIR = _resolve_agents_dir()

# agent display-name -> prompt file under agents/. Names match the old remote agents
# so the dashboard, traces and saved data stay consistent.
AGENT_PROMPTS = {
    "orchestrator-ks": "orchestrator.md",
    "contract-note-ks": "contract_note.md",
    "form-compare-ks": "form_compare.md",
    "pre-onboarding-ks": "pre_onboarding.md",
    "form-verification-ks": "form_verification.md",
    "manual-intervention-ks": "manual.md",
}

ORCHESTRATOR_NAME = "orchestrator-ks"

# Agents whose prompts ship in agents/ but are NOT wired into the current MAF workflow
# graph (onboarding routes to form-compare-ks, not these). Hidden from the inventory +
# routing map and skipped on warmup; kept in AGENT_PROMPTS so a future multi-step
# onboarding flow can wire them back in.
HIDDEN_AGENTS = {"pre-onboarding-ks", "form-verification-ks"}

# The in-process orchestrator only classifies — code routes to the pipelines — so we
# tell it not to attempt any tool call (the prompt file mentions a tool the remote
# version used). This keeps Step-3 JSON output clean and deterministic.
_ORCH_OVERRIDE = (
    "\n\n---\nSYSTEM NOTE (in-process runtime): Do NOT call any tool. The workflow "
    "graph routes to the specialist pipeline itself based on your classification. "
    "Always reply with ONLY the single-line Step 3 JSON object and nothing else."
)

# The contract-note agent gets a REAL tool (lookup_isin). This note tells it to use
# the tool to resolve an ISIN when the note doesn't print one, so the agent genuinely
# tool-calls instead of leaving the field blank for downstream code.
_CONTRACT_TOOL_NOTE = (
    "\n\n---\nTOOL AVAILABLE: `lookup_isin(scrip_name)` resolves an Indian equity "
    "scrip / company name to its 12-char ISIN from the authoritative NSE/BSE security "
    "master. When a contract note does NOT print an ISIN for a trade, CALL this tool "
    "with the printed scrip name and put the returned ISIN into that trade's `isin` "
    "field. If the tool returns an empty string, leave `isin` as \"\"."
)


# --------------------------------------------------------------------------- #
# Real tool: ISIN resolution from the shipped NSE/BSE security master.
# --------------------------------------------------------------------------- #
_isin_table: dict[str, str] | None = None


def _isin_master() -> dict[str, str]:
    """Load + cache the scrip-name -> ISIN lookup from the authoritative masters."""
    global _isin_table
    if _isin_table is None:
        import contract_format as _cf

        _isin_table = _cf.load_security_master()
    return _isin_table


@tool
def lookup_isin(scrip_name: str) -> str:
    """Resolve an Indian equity scrip / company name to its 12-character ISIN using
    the authoritative NSE/BSE security master shipped with the app.

    Args:
        scrip_name: The security / company name as printed on the contract note.

    Returns:
        The 12-char ISIN (e.g. 'INE002A01018'), or an empty string if not found.
    """
    try:
        import contract_format as _cf

        return _cf.resolve_isin(scrip_name, _isin_master()) or ""
    except Exception:  # noqa: BLE001
        return ""


# agent display-name -> real tools it may call.
AGENT_TOOLS = {
    "contract-note-ks": [lookup_isin],
}


# --------------------------------------------------------------------------- #
# Persistent background event loop — a sync bridge so the app's synchronous
# generators (and the async /api/process endpoint, via a *different* loop) can run
# the agents' async `.run()` from any thread safely.
# --------------------------------------------------------------------------- #
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="maf-loop", daemon=True).start()
    return _loop


def _run_coro(coro, timeout: float = 180.0):
    fut = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    return fut.result(timeout)


# --------------------------------------------------------------------------- #
# Client + agent caches (built lazily, once)
# --------------------------------------------------------------------------- #
_credential: DefaultAzureCredential | None = None
_client: AzureOpenAIChatClient | None = None
_agents: dict[str, Agent] = {}
_prompts: dict[str, str] = {}


def _cred() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def _chat_client() -> AzureOpenAIChatClient:
    global _client
    if _client is None:
        _client = AzureOpenAIChatClient(
            endpoint=AOAI_ENDPOINT,
            deployment_name=AOAI_DEPLOYMENT,
            credential=_cred(),
            api_version=AOAI_API_VERSION,
        )
    return _client


def _prompt(agent_name: str) -> str:
    if agent_name not in _prompts:
        fname = AGENT_PROMPTS.get(agent_name)
        text = (AGENTS_DIR / fname).read_text(encoding="utf-8") if fname else ""
        if agent_name == ORCHESTRATOR_NAME:
            text += _ORCH_OVERRIDE
        elif agent_name == "contract-note-ks":
            text += _CONTRACT_TOOL_NOTE
        _prompts[agent_name] = text
    return _prompts[agent_name]


def _agent(agent_name: str) -> Agent:
    if agent_name not in _agents:
        if agent_name not in AGENT_PROMPTS:
            raise KeyError(f"unknown agent: {agent_name}")
        _agents[agent_name] = Agent(
            _chat_client(),
            instructions=_prompt(agent_name),
            name=agent_name,
            tools=AGENT_TOOLS.get(agent_name),
        )
    return _agents[agent_name]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def run_agent(agent_name: str, content: str) -> dict:
    """Run one in-process MAF agent to completion and return a uniform result dict
    `{status, text, error}` (drop-in for the old remote `_run_agent`)."""
    try:
        resp = _run_coro(_agent(agent_name).run(content))
        text = getattr(resp, "text", None)
        if text is None:
            text = str(resp)
        return {"status": "completed", "text": text or "", "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "text": "", "error": str(exc)[:400]}


async def arun_agent(agent_name: str, content: str) -> dict:
    """Async variant of `run_agent` — awaits the agent directly on the current loop.

    Use this from inside MAF workflow executors (which already run on the shared MAF
    event loop); calling the sync `run_agent` there would deadlock the loop.
    """
    try:
        resp = await _agent(agent_name).run(content)
        text = getattr(resp, "text", None)
        if text is None:
            text = str(resp)
        return {"status": "completed", "text": text or "", "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "text": "", "error": str(exc)[:400]}


_INTENTS = ("contract_note", "pre_onboarding", "manual")


def _parse_intent(text: str) -> tuple[str, str]:
    intent, reason = "", ""
    match = re.search(r"\{.*\}", text or "", re.S)
    if match:
        try:
            obj = json.loads(match.group(0))
            intent = str(obj.get("intent", "")).strip()
            reason = str(obj.get("reason", "")).strip()
        except Exception:  # noqa: BLE001
            pass
    if intent not in _INTENTS:
        low = (text or "").lower()
        if "contract" in low or "trade" in low:
            intent = "contract_note"
        elif "onboard" in low or "merchant" in low or "kyc" in low:
            intent = "pre_onboarding"
        else:
            intent = "manual"
    return intent, reason


def classify(payload_json_str: str) -> dict:
    """Run the orchestrator agent on the email payload and return {intent, reason}."""
    res = run_agent(ORCHESTRATOR_NAME, payload_json_str)
    if res["status"] != "completed":
        # On failure fall back to a keyword heuristic over the raw payload.
        intent, reason = _parse_intent(payload_json_str)
        return {"intent": intent, "reason": reason or f"classifier error: {res['error']}", "error": res["error"]}
    intent, reason = _parse_intent(res["text"])
    return {"intent": intent, "reason": reason, "error": None}


async def aclassify(payload_json_str: str) -> dict:
    """Async variant of `classify` for use inside workflow executors."""
    res = await arun_agent(ORCHESTRATOR_NAME, payload_json_str)
    if res["status"] != "completed":
        intent, reason = _parse_intent(payload_json_str)
        return {"intent": intent, "reason": reason or f"classifier error: {res['error']}", "error": res["error"]}
    intent, reason = _parse_intent(res["text"])
    return {"intent": intent, "reason": reason, "error": None}


def list_agents() -> list[dict]:
    """Static description of the in-process agents (for the /api/agents view)."""
    out = []
    for name, fname in AGENT_PROMPTS.items():
        if name in HIDDEN_AGENTS:
            continue
        out.append(
            {
                "id": name,
                "name": name,
                "model": AOAI_DEPLOYMENT,
                "tools": [t.name for t in AGENT_TOOLS.get(name, [])],
                "connected": [],
                "prompt": fname,
                "runtime": "in-process (MAF)",
            }
        )
    return out


def warmup() -> None:
    """Eagerly build the client + agents so the first request is fast (best effort)."""
    try:
        for name in AGENT_PROMPTS:
            if name in HIDDEN_AGENTS:
                continue
            _agent(name)
    except Exception:  # noqa: BLE001
        pass
