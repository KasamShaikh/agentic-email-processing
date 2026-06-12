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

_Phases 2–5 will be documented here as they are implemented._
