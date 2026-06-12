"""
Phase 3 — create the Foundry agents (orchestrator + connected specialists).

Agents are DATA-PLANE objects in the Foundry project, so they cannot be expressed in
Bicep/ARM. This script is the IaC equivalent for Phase 3: it reads the versioned
instruction files in this folder and (re)creates the agents idempotently, wiring the
specialists to the orchestrator via the Connected Agents pattern.

Auth: uses DefaultAzureCredential (your `az login` identity). The identity needs the
"Azure AI Developer" role on the Foundry account.

Usage:
    pip install -r requirements.txt
    python deploy_agents.py
"""

import os
import pathlib

from azure.identity import DefaultAzureCredential
from azure.ai.agents import AgentsClient
from azure.ai.agents.models import ConnectedAgentTool, CodeInterpreterTool

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
    for existing in agents.list_agents():
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

    # --- Pre-onboarding delegates to form-verification ---------------------
    form_tool = ConnectedAgentTool(
        id=form.id,
        name="form_verification",
        description="Validate extracted merchant onboarding fields and return a verdict.",
    )
    pre = agents.create_agent(
        model=MODEL,
        name="pre-onboarding-ks",
        instructions=instructions("pre_onboarding"),
        tools=form_tool.definitions,
    )
    print(f"created pre-onboarding-ks: {pre.id}")

    # --- Orchestrator delegates to the three top-level routes --------------
    orchestrator_tools = (
        ConnectedAgentTool(
            id=contract.id,
            name="contract_note",
            description="Process contract note PDFs and produce a standardised text file.",
        ).definitions
        + ConnectedAgentTool(
            id=pre.id,
            name="pre_onboarding",
            description="Verify merchant pre-onboarding documents.",
        ).definitions
        + ConnectedAgentTool(
            id=manual.id,
            name="manual",
            description="Route the email to a human for manual handling.",
        ).definitions
    )
    orchestrator = agents.create_agent(
        model=MODEL,
        name="orchestrator-ks",
        instructions=instructions("orchestrator"),
        tools=orchestrator_tools,
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
