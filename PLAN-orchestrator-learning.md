# Plan — Orchestrator learns from human Re-routes

> Status: **PLANNED (not yet implemented)** · Saved 2026-06-26

## Goal
When a human uses the **Re-route** control at the HITL gate (action `edit` →
`editedIntent`), persist that correction and feed it back so the orchestrator classifies
similar future emails correctly — no repeat re-route.

Chosen design (confirmed with user): **both** mechanisms; match on **subject keywords +
body semantics**.

---

## How re-route works today (verified)
- **UI** `dashboard/static/index.html` `renderReviewGate()` → "Re-route to…" select +
  `↻ Re-route` button → `decideReview(reviewId,"edit",el,editedIntent)` →
  POST `/api/hitl/decision`.
- `dashboard/app.py` `api_hitl_decision` → `hitl.decide(..., edited_intent=...)`
  (sets `decision.editedIntent`).
- `dashboard/app.py` `_orchestrate`: after `wait_for_decision` it has in scope
  `payload` (parsed email dict), `req.get("intent")` (ORIGINAL/wrong intent),
  `dec.get("editedIntent")` (CORRECT), `dec.get("by")`, `dec.get("note")`.
- `dashboard/workflow.py` `ApprovalExecutor.on_decision`: on `action=="edit"` switches the
  run's `intent` to `editedIntent`.
- **Classifier** = `dashboard/maf_agents.py` `classify` / `aclassify` →
  `run_agent("orchestrator-ks", payload)` — pure prompt LLM call, **no memory**. Prompt is
  built once in `_prompt()` and cached in `_prompts` / `_agents`.
- **Gap:** nothing is remembered, so the same kind of email is misclassified again.

---

## Design (both mechanisms)
1. **SOFT (LLM few-shot, semantic generalization):** inject past corrections into the
   orchestrator's instructions so the LLM learns to classify similar emails the same way.
2. **HARD (deterministic override, guaranteed repeat):** keyword/token-overlap match of the
   incoming email's subject+body against stored corrections; on a strong match, force the
   corrected intent (skip the LLM) and set the reason to "learned routing…". The override
   only fixes the **default** classification — the HITL gate still opens; the human just
   approves (no re-route needed).

---

## Steps

### Phase A — Learning store (new module)
Create `dashboard/routing_memory.py` (pure stdlib: `json, os, re, time, pathlib`):
- `MEM_FILE = data/routing_memory.jsonl`.
- `_tokens(text)`: lowercase, strip punctuation + a small stopword set → set of tokens.
- `record_correction(payload, wrong_intent, correct_intent, by, note)`: append
  `{ts, from, subject, bodyPreview, body_excerpt(<=400), keywords(subject+body tokens),
  wrong_intent, correct_intent, by, note}`. Append-only; `mkdir data/` best-effort.
- `load_corrections(limit=20)`: read JSONL, newest-first, **latest-wins** dedup keyed on
  `(correct_intent + normalized subject signature)`, cap to `limit`.
- `build_guidance_block()`: render a compact instructions block
  `"## Learned routing corrections (from human re-routes) …"` listing
  `"subject:'…' / body:'…' → intent: X (note)"`. Return `""` if empty.
- `match_override(payload)`: token overlap of incoming subject+body vs each correction's
  keywords; return `(correct_intent, record)` if subject Jaccard ≥ threshold OR ≥ N shared
  distinctive tokens, else `None`. Threshold env-tunable (`ROUTING_MATCH_MIN`, default ~0.5
  / 3 tokens).
- `mtime()`: `MEM_FILE` mtime or `0` (for cache invalidation).

### Phase B — Capture on Re-route *(depends on A)*
`dashboard/app.py` `_orchestrate`: after computing `dec`, if `dec.get("action")=="edit"`
and `dec.get("editedIntent")` and `!= req.get("intent")`:
- `routing_memory.record_correction(payload, req.get("intent"), dec["editedIntent"], dec.get("by"), dec.get("note"))`.
- `maf_agents.refresh_orchestrator()` (evict cached orchestrator so the new example applies now).
- `yield` a `learned` SSE event `{type:"learned", from:wrong, to:editedIntent, ts}`.
- Add `import routing_memory` at the top of `app.py`.

### Phase C — Apply learning *(depends on A)*
`dashboard/maf_agents.py`:
- `import routing_memory`; add module var `_orch_mem_mtime`.
- `_prompt(ORCHESTRATOR_NAME)`: append `routing_memory.build_guidance_block()` after `_ORCH_OVERRIDE`.
- `refresh_orchestrator()`: pop `ORCHESTRATOR_NAME` from `_agents` and `_prompts`; set
  `_orch_mem_mtime = routing_memory.mtime()`.
- `_maybe_refresh_orch()`: if `routing_memory.mtime() != _orch_mem_mtime` →
  `refresh_orchestrator()` (self-heal across workers / restarts). Call at the start of
  `classify` / `aclassify`.
- **HARD override** in both `classify` and `aclassify`: parse payload→dict, call
  `routing_memory.match_override(payload)`; if hit, return
  `{intent, reason:"learned routing — matched a prior human re-route (note)", error:None}`
  **without** calling the LLM. Else proceed with the existing (now few-shot-augmented) LLM path.

### Phase D — UI feedback (optional polish)
`dashboard/static/index.html` `handleEvent`: add `case "learned"` →
`addEvent("final", "🧠 Orchestrator learned this re-route", "Future similar emails will route to <b>X</b> automatically")`.
The `intent` event already shows the override reason via `ev.reason`.

### Phase E — Tests (optional but recommended)
`dashboard/test_routing_memory.py` (pytest, no Azure): record→load round-trip;
`build_guidance_block` format; `match_override` matches a similar subject and rejects an
unrelated one (temp `MEM_FILE` via monkeypatch/env).

---

## Files
| File | Change |
|------|--------|
| `dashboard/routing_memory.py` | **NEW** — store + few-shot block + deterministic matcher |
| `dashboard/app.py` | `_orchestrate` capture on edit + emit `learned`; add import |
| `dashboard/maf_agents.py` | `_prompt` augmentation; new `refresh_orchestrator`/`_maybe_refresh_orch`; override in `classify`/`aclassify`; add import + `_orch_mem_mtime` |
| `dashboard/static/index.html` | `handleEvent` `case "learned"` (optional) |
| `dashboard/hitl.py`, `dashboard/workflow.py` | **reference only — no change** (editedIntent already flows through) |

---

## Verification
1. Local: `cd dashboard`; `.venv\Scripts\python -m uvicorn app:app --reload --port 8000`.
2. Run the "manual" sample (team-lunch) → at the gate, **Re-route** to `contract_note`.
   Confirm `data/routing_memory.jsonl` gets a record and the SSE shows `learned` +
   `review_decided` re-routed.
3. Re-run the same/similar email → `intent` event is now `contract_note` **without**
   re-route; reason mentions "learned routing"; the gate opens for the corrected intent
   (just approve).
4. Run an unrelated email (onboarding sample) → **not** falsely overridden.
5. `get_errors` clean on `app.py`, `maf_agents.py`, `routing_memory.py`, `index.html`.
6. (optional) `pytest dashboard/test_routing_memory.py`.
7. After approval: redeploy zip (`robocopy … /XD .venv`; bundle `agents/*.md` at
   `./agents`) + commit/push; confirm live behavior.

---

## Decisions
- Both mechanisms (soft few-shot + hard deterministic override).
- Match signal: subject keywords (deterministic token overlap on subject+body) + body
  semantics (LLM few-shot generalization).
- Store: `dashboard/data/routing_memory.jsonl` (consistent with `hitl_reviews` / `processed` logs).
- Capture point: `app.py` `_orchestrate` on `action=="edit"` (full payload + wrong + correct in scope).
- Override fixes the **default** classification only; the HITL gate still runs. Latest-wins on conflicts.

---

## Further considerations (decide at implementation time)
1. **Override threshold tuning** — too low → false overrides, too high → misses. Recommend a
   conservative default (subject Jaccard ≥ 0.5 OR ≥ 3 shared tokens), env-tunable
   `ROUTING_MATCH_MIN`.
2. **Persistence vs redeploy** — `data/` is bundled in the deploy zip, so a redeploy can
   reset learned corrections. Options: (A) keep in `data/` (PoC-simplest, recommended) /
   (B) move to `/home/data` outside wwwroot / (C) exclude `data/` from the zip + `.gitignore`.
3. **Scope** — include the small UI `learned` badge + pytest test (recommended, so the
   orchestrator visibly "knows"), or keep strictly to the backend "one thing".
