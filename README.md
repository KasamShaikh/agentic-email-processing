# Agentic Email Processing — Implementation Guide

Step-by-step implementation. See [PLAN.md](./PLAN.md) for the architecture and
overall plan. This guide is built **phase by phase**; only the active phase is
detailed below.

**Environment**

| Setting | Value |
|---------|-------|
| Region | Central India (`centralindia`) |
| Subscription | _(set your own — see SETUP NOTE below)_ |
| Resource group | `agentic-email-processing` |
| Naming suffix | `-ks` (readable names, no GUIDs) |

---

## Phase 1 — Provision foundation (Azure Portal)

Goal: create the base Azure resources that later phases build on. All steps are in
the **Azure Portal** (click-ops) for the demo.

> Sign in to the Portal and make sure your target subscription is active before
> you start. **SETUP NOTE:** set your subscription with
> `az account set --subscription <your-subscription-id>` (do not commit the ID).

### Step 1 — Create the resource group

1. Portal → **Resource groups** → **+ Create**.
2. Subscription: _your target subscription_.
3. Resource group: **`agentic-email-processing`**.
4. Region: **Central India**.
5. **Review + create** → **Create**.

### Step 2 — Create the Azure AI Foundry resource + project

1. Portal → search **Azure AI Foundry** → open **Azure AI Foundry** portal
   (https://ai.azure.com), or create the resource from the portal.
2. Create a new **Azure AI Foundry resource**:
   - Name: **`foundry-ks`**
   - Subscription / Resource group: as above
   - Region: **Central India**
3. Inside the resource, create a **project**:
   - Project name: **`email-agentic-ks`**
4. Note the **Project endpoint** (you will need it in Phase 3).

### Step 3 — Deploy the chat model

1. In the Foundry project → **Models + endpoints** → **+ Deploy model** → **Deploy base model**.
2. In the catalog, **filter by region = Central India** and look for a GPT‑5‑series
   **mini** model.
   - ✅ If a **GPT‑5 mini** model is listed for Central India, deploy it.
   - ❌ If not available (likely), choose the fallback **`gpt-4.1-mini`**
     (or `gpt-4o-mini`).
3. Deployment name: **`gpt-mini-ks`** (keep this name regardless of model so
   downstream references don't change).
4. Deploy and wait until status = **Succeeded**.
5. Quick test: open the **Playground**, send "Hello", confirm a response.

> Record the **deployment name** (`gpt-mini-ks`) and the **model** actually used.

### Step 4 — Create the Storage account + containers

1. Portal → **Storage accounts** → **+ Create**.
   - Name: **`agenticemailks`** *(no hyphens allowed in storage account names)*
   - Resource group: `agentic-email-processing`
   - Region: **Central India**
   - Performance: Standard, Redundancy: **LRS** (cheapest for a PoC).
2. **Review + create** → **Create**.
3. After creation → **Data storage → Containers** → **+ Container**, create:
   - **`incoming-attachments`** (stores PDFs from emails)
   - **`contract-notes-output`** (stores generated standardised text files)

### Step 5 — (Recommended) Create Azure AI Document Intelligence

1. Portal → search **Document Intelligence** → **+ Create**.
   - Name: **`docintel-ks`**
   - Resource group: `agentic-email-processing`
   - Region: **Central India**
   - Pricing tier: **F0 (free)** if available, else **S0**.
2. **Review + create** → **Create**.
3. After creation → **Keys and Endpoint** → note the **endpoint** and a **key**.

### Step 6 — Capture outputs

Record these values for Phases 2–3:

| Item | Value |
|------|-------|
| Foundry project endpoint | `__________` |
| Model deployment name | `gpt-mini-ks` |
| Model actually deployed | `__________` |
| Storage account name | `agenticemailks` |
| Input container | `incoming-attachments` |
| Output container | `contract-notes-output` |
| Document Intelligence endpoint | `__________` |

### Phase 1 verification checklist

- [ ] Resource group `agentic-email-processing` exists in Central India.
- [ ] Foundry resource `foundry-ks` + project `email-agentic-ks` created.
- [ ] Model deployment `gpt-mini-ks` shows **Succeeded** and responds in the Playground.
- [ ] Storage account `agenticemailks` has both containers.
- [ ] (Optional) `docintel-ks` endpoint responds.

---

_Phases 2–5 will be documented here as they are implemented._
