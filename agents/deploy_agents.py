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

from azure.identity import DefaultAzureCredential
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import CodeInterpreterTool

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
    "manual-intervention-ks",
}


def instructions(name: str) -> str:
    return (HERE / f"{name}.md").read_text(encoding="utf-8")


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

    # --- Orchestrator: pure intent classifier (routing done in code) -------
    orchestrator = agents.create_agent(
        model=MODEL,
        name="orchestrator-ks",
        instructions=instructions("orchestrator"),
    )
    print(f"created orchestrator-ks: {orchestrator.id}")

    print()
    print("=== Agents created ===")
    print(f"ORCHESTRATOR_AGENT_ID={orchestrator.id}")
    print(
        "Set this on the Logic App with:\n"
        "  az deployment group create -g agentic-email-processing "
        "--template-file infra/phase2.bicep --parameters infra/phase2.bicepparam "
        f"orchestratorAgentId={orchestrator.id}"
    )


if __name__ == "__main__":
    main()
