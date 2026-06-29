# Agentic Email Processing — Production-Grade Component Architecture

> Board-ready, layered component view of the solution. Each tier is a swappable
> band of components: **Data Sources → Ingestion → Orchestration (MAF) → Processing →
> Human-in-the-Loop → Data & State → Observability**, all riding on a shared
> **Identity / Security** and **Platform / IaC** foundation. The PoC today wires
> Outlook + Logic Apps + MAF; every other component is a clearly-marked extension slot.

Legend: ✅ live in PoC · 🟡 design-ready slot · 🔵 platform / cross-cutting.

---

## 1. Layered component architecture (the big picture)

```mermaid
flowchart TB
    %% ================= DATA SOURCES =================
    subgraph SRC["①  DATA SOURCES"]
        direction LR
        S1["✅ Outlook / M365<br/>mailbox"]
        S2["🟡 SharePoint /<br/>OneDrive"]
        S3["🟡 Blob drop /<br/>SFTP"]
        S4["🟡 API / webhook<br/>partners"]
        S5["🟡 Teams /<br/>queues"]
    end

    %% ================= INGESTION =================
    subgraph ING["②  INGESTION LAYER"]
        direction LR
        I1["✅ Logic Apps<br/>(Office365 trigger)"]
        I2["🟡 Work IQ /<br/>connectors"]
        I3["🟡 Event Grid /<br/>Service Bus"]
        I4["✅ Attachment →<br/>Blob landing"]
    end

    %% ================= ORCHESTRATION =================
    subgraph ORC["③  ORCHESTRATION  (Microsoft Agent Framework)"]
        direction LR
        O1["✅ orchestrator-ks<br/>intent classifier"]
        O2["✅ Workflow graph<br/>switch-case edges"]
        O3["✅ Checkpointing<br/>FileCheckpointStorage"]
        O4["✅ gpt-mini-ks<br/>(Foundry model)"]
    end

    %% ================= HITL =================
    subgraph HIL["④  HUMAN-IN-THE-LOOP"]
        direction LR
        H1["✅ request_info gate"]
        H2["✅ L1 / L2 / L3<br/>risk levels"]
        H3["✅ Approve · Reject<br/>· Re-route"]
    end

    %% ================= PROCESSING =================
    subgraph PRC["⑤  PROCESSING  (specialist executors)"]
        direction LR
        P1["✅ contract-note-ks<br/>+ lookup_isin"]
        P2["✅ form-compare-ks<br/>OCR + scorer"]
        P3["✅ manual-intervention<br/>-ks"]
        P4["✅ Doc Intelligence<br/>prebuilt-layout"]
    end

    %% ================= DATA / STATE =================
    subgraph DAT["⑥  DATA & STATE"]
        direction LR
        D1["✅ incoming-attachments"]
        D2["✅ contract-notes-output"]
        D3["✅ ISIN / security master"]
        D4["✅ processed · HITL · checkpoints"]
    end

    %% ================= OBSERVABILITY =================
    subgraph OBS["⑦  OBSERVABILITY"]
        direction LR
        B1["✅ SSE live trace"]
        B2["✅ Run + HITL audit"]
        B3["🟡 App Insights /<br/>OTel"]
        B4["🟡 Log Analytics /<br/>alerts"]
    end

    %% ================= FOUNDATION =================
    subgraph SEC["🔵  IDENTITY & SECURITY — Managed Identity · RBAC · Key Vault · TLS · private endpoints"]
    end
    subgraph PLT["🔵  PLATFORM & IaC — Bicep (infra) · SDK script (agents) · azd · CI/CD"]
    end

    SRC --> ING --> ORC --> HIL --> PRC --> DAT
    ORC -. emits .-> OBS
    HIL -. emits .-> OBS
    PRC -. emits .-> OBS
    SEC -.-> ING & ORC & PRC & DAT
    PLT -.-> SRC & ING & ORC & PRC & DAT & OBS
```

---

## 2. Layer-by-layer component catalogue

| # | Layer | Live components (PoC) | Extension slots | Job |
|---|-------|-----------------------|-----------------|-----|
| ① | **Data Sources** | Outlook / M365 mailbox | SharePoint, OneDrive, SFTP/Blob drop, partner webhooks, Teams, queues | Where work arrives |
| ② | **Ingestion** | Logic Apps (Office 365 V3 trigger) → attachments to Blob | Work IQ connectors, Event Grid, Service Bus, Graph webhooks | Capture event, persist payload, hand off |
| ③ | **Orchestration (MAF)** | `orchestrator-ks` classifier, Workflow graph, switch-case edges, checkpoints, `gpt-mini-ks` | Multi-agent fan-out, retries, sub-workflows | Decide intent, route, keep durable state |
| ④ | **Human-in-the-Loop** | `request_info` gate, L1/L2/L3 levels, approve/reject/re-route, review-window timeout | Teams adaptive cards, role-based approval | Risk-graded sign-off, no run hangs |
| ⑤ | **Processing** | `contract-note-ks` (+`lookup_isin`), `form-compare-ks` (OCR+scorer), `manual-intervention-ks`, Document Intelligence | Onboarding chain, browser automation, more specialists | Deterministic pipelines, side-effects |
| ⑥ | **Data & State** | `incoming-attachments`, `contract-notes-output`, security master, processed/HITL/checkpoints | Cosmos/SQL audit, vector index | Inputs, outputs, master data, durability |
| ⑦ | **Observability** | SSE live trace, run + HITL audit, dashboard | App Insights / OpenTelemetry, Log Analytics, alerts | See, audit, alert on every step |
| 🔵 | **Identity & Security** | Managed identity, RBAC, no keys on disk, TLS 1.2 | Key Vault, private endpoints, VNet | Least-privilege, auditable secrets |
| 🔵 | **Platform & IaC** | Bicep (Phase 1–2), SDK script (agents) | azd, CI/CD pipeline | Repeatable, versioned deploys |

---

## 3. Request flow (swimlane)

```mermaid
sequenceDiagram
    participant Mailbox as ① Outlook
    participant Logic as ② Logic Apps
    participant Blob as ⑥ Blob landing
    participant Orch as ③ Orchestrator (MAF)
    participant Human as ④ HITL gate
    participant Spec as ⑤ Specialist
    participant Obs as ⑦ Observability

    Mailbox->>Logic: new email + attachments
    Logic->>Blob: PDFs → incoming-attachments
    Logic->>Orch: payload (subject, body, blobs)
    Orch->>Orch: classify intent + checkpoint
    Orch->>Human: request_info (L1/L2/L3)
    Human-->>Orch: approve / reject / re-route
    Orch->>Spec: switch-case route
    Spec->>Blob: PIS files → contract-notes-output
    Orch-->>Obs: SSE trace + audit each step
```

---

## 4. Production hardening checklist

- **Identity:** managed identity everywhere; no keys/subscription IDs on disk.
- **Resilience:** checkpoint per superstep, retry-with-backoff on Doc Intelligence, HITL auto-decide on timeout.
- **Auditability:** processed/HITL/checkpoint journals + SSE trace; ready for App Insights + Log Analytics.
- **Network (next):** private endpoints + VNet, Key Vault for connection secrets.
- **Deploy:** Bicep for control-plane, versioned SDK script for data-plane agents — fully repeatable.

> PoC = ① Outlook ⮕ ② Logic Apps ⮕ ③ MAF ⮕ ④ HITL ⮕ ⑤ specialists ⮕ ⑥ storage, with ⑦ live trace. Yellow slots scale it to a multi-source, multi-tenant production grade.
