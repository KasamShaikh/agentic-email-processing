# Agentic Email Processing — Implementation Guide

Step-by-step implementation. See [PLAN.md](./PLAN.md) for the architecture and
overall plan. This guide is built **phase by phase**; only the active phase is
detailed below.

**Environment**

| Setting | Value |
|---------|-------|
| Data region | Central India (`centralindia`) |
| Model region | Sweden Central (`swedencentral`) — Central India is PTU-only |
| Subscription | _(set your own at deploy time — not stored in repo)_ |
| Resource group | `agentic-email-processing` |
| Naming suffix | `-ks` (readable names, no GUIDs) |

---

## Phase 1 — Provision foundation (Bicep) ✅ Deployed

Phase 1 is deployed as **Infrastructure-as-Code** (Bicep) instead of manual portal
clicks, so it's repeatable. Templates live in [`infra/`](./infra):

| File | Purpose |
|------|---------|
| [`infra/main.bicep`](./infra/main.bicep) | Subscription-scope entry point — creates the resource group and deploys the resources module |
| [`infra/resources.bicep`](./infra/resources.bicep) | All Phase 1 resources (Storage + containers, Foundry account + project + model, Document Intelligence) |
| [`infra/main.bicepparam`](./infra/main.bicepparam) | Parameter values (no secrets / subscription IDs) |

### Deploy / redeploy

```powershell
# 1. Sign in and select the target subscription (ID is NOT stored in the repo)
az login
az account set --subscription <your-subscription-id>

# 2. Deploy (subscription scope creates the resource group too)
az deployment sub create `
  --name phase1-emailagentic `
  --location centralindia `
  --template-file infra/main.bicep `
  --parameters infra/main.bicepparam
```

The deployment is idempotent — re-running it reconciles to the template.

### Region note (important)

**Central India is PTU-only** for Azure OpenAI models (no pay-as-you-go
`Standard`/`GlobalStandard`). To avoid provisioned-capacity cost for the PoC:

- **Data resources** (Storage, Document Intelligence) → **Central India** (data stays in India).
- **Model** → **`gpt-5.4-mini`** deployed as **GlobalStandard** (pay-as-you-go, global routing)
  on a Foundry account in **Sweden Central**.

### What got created

| Resource | Name | Region | Notes |
|----------|------|--------|-------|
| Resource group | `agentic-email-processing` | Central India | |
| Foundry (AI Services) account | `foundry-ks` | Sweden Central | subdomain `agentic-email-foundry-ks` |
| Foundry project | `email-agentic-ks` | Sweden Central | |
| Model deployment | `gpt-mini-ks` | (global) | `gpt-5.4-mini` v2026-03-17, GlobalStandard, capacity 20 |
| Storage account | `agenticemailks` | Central India | Standard_LRS |
| Blob container (input) | `incoming-attachments` | | |
| Blob container (output) | `contract-notes-output` | | |
| Document Intelligence | `docintel-ks` | Central India | subdomain `agentic-email-docintel-ks` |

### Deployment outputs (for Phases 2–3)

| Item | Value |
|------|-------|
| Foundry endpoint | `https://agentic-email-foundry-ks.cognitiveservices.azure.com/` |
| Foundry project | `email-agentic-ks` |
| Model deployment name | `gpt-mini-ks` (model `gpt-5.4-mini`) |
| Storage account | `agenticemailks` |
| Input container | `incoming-attachments` |
| Output container | `contract-notes-output` |
| Document Intelligence endpoint | `https://agentic-email-docintel-ks.cognitiveservices.azure.com/` |

### Phase 1 verification ✅

- [x] Resource group `agentic-email-processing` created.
- [x] Foundry account `foundry-ks` + project `email-agentic-ks` created (Sweden Central).
- [x] Model deployment `gpt-mini-ks` (`gpt-5.4-mini`, GlobalStandard) — **Succeeded**.
- [x] Storage account `agenticemailks` with `incoming-attachments` + `contract-notes-output`.
- [x] Document Intelligence `docintel-ks` created.

---

## Phase 2 — Email ingestion (Logic Apps, Bicep) ✅ Deployed

A **Consumption Logic App** (`logic-email-ks`) handles the event-driven email
integration. It is deployed as Bicep into the existing resource group:

| File | Purpose |
|------|---------|
| [`infra/phase2.bicep`](./infra/phase2.bicep) | Logic App + Office 365 Outlook + Azure Blob API connections |
| [`infra/logic-workflow.json`](./infra/logic-workflow.json) | The workflow definition (trigger → loop → blob → call orchestrator) |
| [`infra/phase2.bicepparam`](./infra/phase2.bicepparam) | Parameter values (no secrets) |

### What the workflow does

1. **Trigger** — *When a new email arrives (V3)* (Office 365 Outlook), Inbox, attachments included.
2. **For each attachment** — if the file ends in `.pdf`, **Create blob** into the
   `incoming-attachments` container and collect its path.
3. **Compose payload** — `{ subject, from, bodyPreview, body, attachmentBlobs }`.
4. **Call orchestrator agent** — when `orchestratorAgentId` is set, POST the payload to
   the Foundry agents endpoint (`/threads/runs`) using the Logic App's **managed identity**.

### Deploy / redeploy

```powershell
az deployment group create `
  --resource-group agentic-email-processing `
  --template-file infra/phase2.bicep `
  --parameters infra/phase2.bicepparam
```

### ⚠️ One-time manual step (cannot be automated)

The **Office 365 Outlook** API connection requires an interactive OAuth consent. After
the first deploy, open the **Azure Portal → Resource group → `office365-ks` connection
→ Edit API connection → Authorize**, sign in with the mailbox account, and save. Until
this is done the email trigger will not fire. (The Azure Blob connection uses the
storage key fetched at deploy time and needs no manual step.)

### What got created

| Resource | Name | Notes |
|----------|------|-------|
| Logic App (Consumption) | `logic-email-ks` | System-assigned managed identity enabled |
| Office 365 Outlook connection | `office365-ks` | **Needs manual Authorize** (see above) |
| Azure Blob connection | `azureblob-ks` | Uses storage key (fetched via `listKeys` at deploy) |

The Logic App's managed identity is granted **Cognitive Services User** on the Foundry
account/project so it can call the orchestrator agent.

---

## Phase 3 — Foundry agents (Connected Agents) ✅ Deployed

Foundry agents are **data-plane** objects (they live inside the project, not in ARM),
so they **cannot** be expressed in Bicep. The IaC equivalent is a versioned script that
reads instruction files and (re)creates the agents idempotently.

| File | Purpose |
|------|---------|
| [`agents/deploy_agents.py`](./agents/deploy_agents.py) | Creates the 5 agents and wires the Connected Agents graph |
| [`agents/requirements.txt`](./agents/requirements.txt) | `azure-ai-agents`, `azure-identity` |
| [`agents/orchestrator.md`](./agents/orchestrator.md) | Orchestrator instructions (intent + routing) |
| [`agents/contract_note.md`](./agents/contract_note.md) | Contract Note Upload agent |
| [`agents/pre_onboarding.md`](./agents/pre_onboarding.md) | Merchant Pre-Onboarding agent |
| [`agents/form_verification.md`](./agents/form_verification.md) | Foundry Form Verification agent |
| [`agents/manual.md`](./agents/manual.md) | Manual Intervention agent |

### Agent graph (Connected Agents)

```mermaid
flowchart TD
    O[orchestrator-ks] -->|contract_note| C[contract-note-ks]
    O -->|pre_onboarding| P[pre-onboarding-ks]
    O -->|manual| M[manual-intervention-ks]
    P -->|form_verification| F[form-verification-ks]
```

The orchestrator classifies intent (`contract_note | pre_onboarding | manual`) and
delegates to the matching connected agent. `pre-onboarding-ks` further delegates field
validation to `form-verification-ks`. `contract-note-ks` and `form-verification-ks`
have the **Code Interpreter** tool for formatting/validation.

### Prerequisites

The identity running the script needs the **Cognitive Services User** role on the
Foundry account (data action `Microsoft.CognitiveServices/*`):

```powershell
$acct = az cognitiveservices account show -g agentic-email-processing -n foundry-ks --query id -o tsv
az role assignment create --assignee <your-object-id> --role "Cognitive Services User" --scope $acct
```

### Create / update the agents

```powershell
pip install -r agents/requirements.txt
python agents/deploy_agents.py
```

The script prints `ORCHESTRATOR_AGENT_ID=...`. Wire it into the Logic App so Phase 2 can
call the orchestrator:

```powershell
az deployment group create `
  --resource-group agentic-email-processing `
  --template-file infra/phase2.bicep `
  --parameters infra/phase2.bicepparam orchestratorAgentId=<asst_...>
```

---

_Phases 4–5 will be documented here as they are implemented._
