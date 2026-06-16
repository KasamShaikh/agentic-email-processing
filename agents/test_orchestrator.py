"""
Test helper — see which agent the orchestrator routes to.

This does NOT change any deployed resource. It sends a sample email payload straight
to the `orchestrator-ks` agent, waits for the run to finish, then prints:

  1. the intent / final answer the orchestrator returned, and
  2. every connected-agent hop recorded in the run steps (i.e. which specialist
     agents actually fired).

Usage:
    pip install -r requirements.txt        # same deps as deploy_agents.py
    python test_orchestrator.py contract   # built-in contract-note sample
    python test_orchestrator.py onboarding # built-in onboarding sample
    python test_orchestrator.py manual     # built-in "nothing matches" sample
    python test_orchestrator.py "Subject: ... Body: ... your own raw text"

Auth: uses your `az login` identity (needs Cognitive Services User on the Foundry
account, which you already granted).
"""

import json
import os
import sys

from azure.identity import DefaultAzureCredential
from azure.ai.agents import AgentsClient

ENDPOINT = os.environ.get(
    "PROJECT_ENDPOINT",
    "https://agentic-email-foundry-ks.services.ai.azure.com/api/projects/email-agentic-ks",
)
ORCHESTRATOR_NAME = "orchestrator-ks"

# connected-agent tool name -> the agent it represents
TOOL_TO_AGENT = {
    "contract_note": "contract-note-ks",
    "pre_onboarding": "pre-onboarding-ks",
    "manual": "manual-intervention-ks",
    "form_verification": "form-verification-ks",
}

SAMPLES = {
    "contract": json.dumps(
        {
            "subject": "Contract Note - Trade Confirmation 17-Jun-2026",
            "from": "broker@brokerage.example.com",
            "bodyPreview": "Please find attached your contract note for the trade executed today.",
            "body": "Dear Client, please find attached the contract note for your trade "
            "(ISIN INE002A01018, 100 shares). Regards, Broker Ops.",
            "attachmentBlobs": ["incoming-attachments/contract-note-cn123.pdf"],
        }
    ),
    "onboarding": json.dumps(
        {
            "subject": "Merchant pre-onboarding documents for ACME Traders",
            "from": "ops@acme-traders.example.com",
            "bodyPreview": "Submitting KYC and registration documents for onboarding.",
            "body": "Hi, attaching our business registration and bank details to start "
            "the merchant onboarding for ACME Traders Pvt Ltd.",
            "attachmentBlobs": ["incoming-attachments/acme-kyc.pdf"],
        }
    ),
    "manual": json.dumps(
        {
            "subject": "Team lunch on Friday",
            "from": "hr@company.example.com",
            "bodyPreview": "Reminder: team lunch at 1 PM in the cafeteria.",
            "body": "Hi all, just a reminder that we have a team lunch this Friday at 1 PM. "
            "Please RSVP. Thanks!",
            "attachmentBlobs": [],
        }
    ),
}


def pick_payload() -> str:
    arg = sys.argv[1] if len(sys.argv) > 1 else "contract"
    if arg in SAMPLES:
        return SAMPLES[arg]
    # treat anything else as a raw custom body
    return arg


def message_text(msg_dict: dict) -> str:
    parts = []
    for item in msg_dict.get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", {}).get("value", ""))
    return "\n".join(parts).strip()


def main() -> None:
    payload = pick_payload()

    agents = AgentsClient(
        endpoint=ENDPOINT,
        credential=DefaultAzureCredential(),
        credential_scopes=["https://ai.azure.com/.default"],
    )

    # Resolve agent ids -> names so we can map run-step references back to agents.
    id_to_name = {}
    orchestrator_id = None
    for a in agents.list_agents():
        id_to_name[a.id] = a.name
        if a.name == ORCHESTRATOR_NAME:
            orchestrator_id = a.id
    if not orchestrator_id:
        sys.exit(f"Could not find agent '{ORCHESTRATOR_NAME}'. Run deploy_agents.py first.")

    print("=" * 70)
    print("Sending payload to orchestrator-ks:")
    print(payload)
    print("=" * 70)

    thread = agents.threads.create()
    agents.messages.create(thread_id=thread.id, role="user", content=payload)
    run = agents.runs.create_and_process(thread_id=thread.id, agent_id=orchestrator_id)

    print(f"\nthread id : {thread.id}")
    print(f"run id    : {run.id}")
    print(f"run status: {run.status}")
    if getattr(run, "last_error", None):
        print(f"last error: {run.last_error}")

    # --- which agents fired (from run steps) ---------------------------------
    called = []
    print("\n--- run steps (in order) ---")
    for step in agents.run_steps.list(thread_id=thread.id, run_id=run.id):
        d = step.as_dict()
        blob = json.dumps(d)
        hits = set()
        # match by connected-agent tool name
        for tool_name, agent_name in TOOL_TO_AGENT.items():
            if f'"{tool_name}"' in blob:
                hits.add(agent_name)
        # match by agent id reference
        for aid, aname in id_to_name.items():
            if aid in blob and aname != ORCHESTRATOR_NAME:
                hits.add(aname)
        step_type = d.get("type", "?")
        if hits:
            for h in hits:
                if h not in called:
                    called.append(h)
            print(f"  [{step_type}] -> {', '.join(sorted(hits))}")
        else:
            print(f"  [{step_type}]")

    print("\n>>> AGENTS THE ORCHESTRATOR CALLED:", called or "(none detected in steps)")

    # --- final answer --------------------------------------------------------
    print("\n--- orchestrator final message ---")
    for msg in agents.messages.list(thread_id=thread.id):
        d = msg.as_dict()
        if d.get("role") == "assistant":
            text = message_text(d)
            if text:
                print(text)
            break

    print(
        "\nTip: open this thread in the Foundry portal "
        f"(thread {thread.id}) to inspect every step visually."
    )


if __name__ == "__main__":
    main()
