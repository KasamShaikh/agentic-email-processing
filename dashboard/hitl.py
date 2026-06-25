"""
Human-in-the-loop (HITL) review queue + approval gates.

A real, blocking gate the orchestration pauses on: when a run reaches a gate it
`open_review(...)`s an item, emits an `awaiting_review` trace event, then
`wait_for_decision(...)` BLOCKS the run until a human Approves / Rejects / Edits it
from the dashboard (or an auto-decision fires after a timeout, so unattended/headless
emails never hang forever).

Three gate levels, chosen by what the function carries (reversibility x risk):
  L1  Final sign-off    — quick confirm right before an irreversible write
                          (contract-note -> PIS upload file).
  L2  Maker review      — durable approve/reject that can wait hours/days
                          (merchant onboarding verification verdict).
  L3  Exception check   — a human is pulled in only for odd/uncertain cases
                          (manual / unclassified email -> decide what to do).

Backing store is in-memory (live queue + blocking Events) plus an append-only JSONL
audit log on disk — enough to demo the full flow locally with complete tracing; no
extra infra required.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
AUDIT_LOG = DATA_DIR / "hitl_reviews.jsonl"

# How a function's risk maps to a gate level.
LEVEL_BY_INTENT = {
    "contract_note": 1,   # irreversible regulatory upload -> final sign-off
    "pre_onboarding": 2,  # verification verdict -> durable maker review
    "manual": 3,          # unclassified -> exception, human decides
}
LEVEL_LABEL = {
    1: "Final sign-off (L1)",
    2: "Maker review (L2)",
    3: "Exception check (L3)",
}
LEVEL_BLURB = {
    1: "Quick confirm before the one irreversible write (PIS upload file).",
    2: "Durable maker review — can wait hours/days, then resumes.",
    3: "Exception path — a human decides what to do with this email.",
}

# Default wait before an auto-decision fires (keeps unattended runs moving). Long for
# interactive UI runs so there is ample time to click; short for headless sources.
TIMEOUT_UI = int(os.getenv("HITL_TIMEOUT_UI", "900"))
TIMEOUT_HEADLESS = int(os.getenv("HITL_TIMEOUT_HEADLESS", "45"))
# What happens on timeout: "approve" (default — demo never hard-stops) or "reject".
TIMEOUT_ACTION = os.getenv("HITL_TIMEOUT_ACTION", "approve").lower()

_lock = threading.Lock()
_reviews: dict[str, dict] = {}
_events: dict[str, threading.Event] = {}


def level_for_intent(intent: str) -> int:
    return LEVEL_BY_INTENT.get(intent, 3)


def _persist(record: dict) -> None:
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001
        pass


def open_review(
    run_id: str,
    intent: str,
    title: str,
    summary: str,
    details: dict | None = None,
    level: int | None = None,
) -> dict:
    """Create an awaiting-review item and return it (does not block)."""
    lvl = level or level_for_intent(intent)
    rid = uuid.uuid4().hex[:12]
    record = {
        "id": rid,
        "runId": run_id,
        "intent": intent,
        "level": lvl,
        "levelLabel": LEVEL_LABEL.get(lvl, f"L{lvl}"),
        "levelBlurb": LEVEL_BLURB.get(lvl, ""),
        "title": title,
        "summary": summary,
        "details": details or {},
        "state": "awaiting",
        "decision": None,
        "createdAt": time.time(),
        "decidedAt": None,
    }
    with _lock:
        _reviews[rid] = record
        _events[rid] = threading.Event()
    _persist({"event": "opened", **record})
    return record


def decide(review_id: str, action: str, by: str = "reviewer", note: str = "",
           edited_intent: str | None = None) -> dict | None:
    """Record a human decision and unblock the waiting run. `action` in
    {approve, reject, edit}. `edited_intent` re-routes the run when action == edit."""
    action = (action or "").lower()
    if action not in ("approve", "reject", "edit"):
        return None
    with _lock:
        record = _reviews.get(review_id)
        if not record or record["state"] != "awaiting":
            return record
        record["state"] = "rejected" if action == "reject" else "approved"
        record["decision"] = {
            "action": action,
            "by": by,
            "note": note,
            "editedIntent": edited_intent,
            "at": time.time(),
        }
        record["decidedAt"] = record["decision"]["at"]
        ev = _events.get(review_id)
    _persist({"event": "decided", **record})
    if ev:
        ev.set()
    return record


def wait_for_decision(review_id: str, timeout: float, on_timeout: str = TIMEOUT_ACTION) -> dict:
    """BLOCK until the item is decided or `timeout` elapses; on timeout auto-decide
    with `on_timeout` (approve/reject) so the run never hangs. Returns the record."""
    ev = _events.get(review_id)
    decided = ev.wait(timeout) if ev else True
    if not decided:
        auto = "reject" if on_timeout == "reject" else "approve"
        rec = decide(review_id, auto, by="auto (timeout)",
                     note=f"No human decision within {int(timeout)}s; auto-{auto}.")
        return rec or _reviews.get(review_id, {})
    return _reviews.get(review_id, {})


def get(review_id: str) -> dict | None:
    with _lock:
        rec = _reviews.get(review_id)
        return dict(rec) if rec else None


def list_reviews(state: str | None = None) -> list[dict]:
    with _lock:
        items = [dict(r) for r in _reviews.values()]
    if state:
        items = [r for r in items if r["state"] == state]
    items.sort(key=lambda r: r["createdAt"], reverse=True)
    return items


def history(limit: int = 200) -> list[dict]:
    """Recent decided items from the in-memory store (newest first)."""
    decided = [r for r in list_reviews() if r["state"] != "awaiting"]
    return decided[:limit]
