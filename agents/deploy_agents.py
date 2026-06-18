"""
Phase 3 — create the Foundry agents (intent classifier + leaf specialists).

Agents are DATA-PLANE objects in the Foundry project, so they cannot be expressed in
Bicep/ARM. This script is the IaC equivalent for Phase 3: it reads the versioned
instruction files in this folder and (re)creates the agents idempotently.

Routing model: the orchestrator is a pure INTENT CLASSIFIER (returns JSON `{intent}`)
and the routing/delegation to specialists is performed in code (see dashboard/app.py
and agents/test_orchestrator.py). The Foundry "connected agents (classic)" tool is not
used — it is unsupported on this endpoint/model and fails server-side.

Auth: uses DefaultAzureCredential (your `az login` identity). The identity needs the
"Cognitive Services User" role on the Foundry account.

Usage:
    pip install -r requirements.txt
    python deploy_agents.py
"""

import os
import pathlib
from typing import List

from azure.identity import DefaultAzureCredential
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import CodeInterpreterTool, FunctionTool

ENDPOINT = os.environ.get(
    "PROJECT_ENDPOINT",
    "https://agentic-email-foundry-ks.services.ai.azure.com/api/projects/email-agentic-ks",
)
MODEL = os.environ.get("MODEL_DEPLOYMENT", "gpt-mini-ks")

HERE = pathlib.Path(__file__).parent

AGENT_NAMES = {
    "orchestrator-ks",
    "contract-note-ks",
    "pre-onboarding-ks",
    "form-verification-ks",
    "form-compare-ks",
    "manual-intervention-ks",
}


def instructions(name: str) -> str:
    return (HERE / f"{name}.md").read_text(encoding="utf-8")


def run_contract_pipeline(attachment_blobs: List[str]) -> str:
    """Extract, map and convert ALL contract-note attachments on this email into the
    pipe-delimited PIS upload file(s) — grouped by exchange and Buy/Sale — and upload
    them to the contract-notes-output container.

    The body of this function is never executed here: it only defines the tool schema
    advertised to the orchestrator. The dashboard worker (dashboard/app.py) executes the
    real pipeline (dashboard/contract_pipeline.py) when the agent requests this tool and
    submits the result back as the tool output.

    :param attachment_blobs: Blob paths of EVERY attachment on the email, e.g.
        ["incoming-attachments/note1.pdf", "incoming-attachments/note2.png"]. Always pass
        the COMPLETE list in one call so all notes are combined into the correct grouped
        files (never one file per attachment).
    :return: A short summary describing the files written and any warnings.
    """
    return ""


_contract_pipeline_tool = FunctionTool({run_contract_pipeline})


def main() -> None:
    agents = AgentsClient(
        endpoint=ENDPOINT,
        credential=DefaultAzureCredential(),
        credential_scopes=["https://ai.azure.com/.default"],
    )

    # Idempotency: remove any prior copies of our agents before recreating.
    # Materialise the list first — deleting while paging corrupts the iterator.
    for existing in list(agents.list_agents()):
        if existing.name in AGENT_NAMES:
            agents.delete_agent(existing.id)
            print(f"deleted existing agent: {existing.name} ({existing.id})")

    # --- Leaf agents -------------------------------------------------------
    form = agents.create_agent(
        model=MODEL,
        name="form-verification-ks",
        instructions=instructions("form_verification"),
        tools=CodeInterpreterTool().definitions,
    )
    print(f"created form-verification-ks: {form.id}")

    contract = agents.create_agent(
        model=MODEL,
        name="contract-note-ks",
        instructions=instructions("contract_note"),
        tools=CodeInterpreterTool().definitions,
    )
    print(f"created contract-note-ks: {contract.id}")

    manual = agents.create_agent(
        model=MODEL,
        name="manual-intervention-ks",
        instructions=instructions("manual"),
    )
    print(f"created manual-intervention-ks: {manual.id}")

    # --- Pre-onboarding: leaf extractor (routing done in code) -------------
    pre = agents.create_agent(
        model=MODEL,
        name="pre-onboarding-ks",
        instructions=instructions("pre_onboarding"),
    )
    print(f"created pre-onboarding-ks: {pre.id}")

    # --- Onboarding form comparison: extracts + aligns two forms (web-UI vs
    #     handwritten); deterministic scoring is done in code (form_compare.py). ---
    form_compare = agents.create_agent(
        model=MODEL,
        name="form-compare-ks",
        instructions=instructions("form_compare"),
    )
    print(f"created form-compare-ks: {form_compare.id}")

    # --- Orchestrator: intent classifier + contract-pipeline tool caller ---
    orchestrator = agents.create_agent(
        model=MODEL,
        name="orchestrator-ks",
        instructions=instructions("orchestrator"),
        tools=_contract_pipeline_tool.definitions,
    )
    print(f"created orchestrator-ks: {orchestrator.id}")

    print()
    print("=== Agents created ===")
    print(f"ORCHESTRATOR_AGENT_ID={orchestrator.id}")
    print(
        "Agents are resolved by name at runtime, so no id needs to be wired anywhere.\n"
        "The dashboard and Logic App reference 'orchestrator-ks' by name."
    )


if __name__ == "__main__":
    main()
