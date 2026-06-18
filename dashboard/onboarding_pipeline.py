"""
Merchant-onboarding form-verification pipeline.

For a pre-onboarding email with two attachments — a WEB-UI form (portal screenshot or
JSON export) and a HANDWRITTEN form (scanned image) — this pipeline:
  1. downloads both files (managed identity — shared-key auth is disabled by policy),
  2. turns each into text via the shared, type-aware `doc_extract.extract_text`
     (Document Intelligence for images/PDFs, direct decode for JSON/CSV/text),
  3. has `form-compare-ks` extract and ALIGN the fields across the two forms,
  4. scores the alignment deterministically (form_compare.py) into a match % + a
     match / near / mismatch verdict per field,
then writes a human-readable `.txt` report and a machine `.json` verdict to the output
container.

Runs as a generator yielding SSE event dicts so the dashboard can show every step live.
All Azure access uses `DefaultAzureCredential`.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Iterator

import form_compare as fcmp
from doc_extract import (
    blob_basename,
    download_blob,
    extract_text,
    upload_text,
)

OUTPUT_CONTAINER = "contract-notes-output"
OUTPUT_PREFIX = "onboarding/"

# Demo: onboarding always runs against the two fixed sample forms kept in this
# dedicated container (a web-UI form + a handwritten form). Drop any two files here
# and the flow uses them, whether triggered by an email or the UI button.
DEMO_CONTAINER = "onboarding"

# Cap the text sent to the agent per form. A scanned multi-page form (e.g. an Axis
# merchant application) can OCR to 60k+ chars of dense tables; the full combined
# prompt overruns the model deployment and the run fails with a generic server_error.
# Form fields live near the top, so ~12k chars/form is ample and keeps the call stable.
MAX_FORM_CHARS = 12000

# Filename hints used to tell the two forms apart.
_WEB_HINTS = ("web", "ui", "portal", "screen", "json", "online")
_HAND_HINTS = ("hand", "scan", "written", "manual", "paper")


def demo_attachments() -> list[str]:
    """List the fixed demo onboarding forms kept in the `onboarding` container
    (a web-UI form + a handwritten form).

    Returns container-qualified paths (`onboarding/<name>`), sorted by name. Empty
    list if the container holds no files, in which case callers fall back to the
    email's own attachments.
    """
    from doc_extract import _blob  # shared blob service client

    container = _blob().get_container_client(DEMO_CONTAINER)
    names = [b.name for b in container.list_blobs() if not b.name.endswith("/")]
    return [f"{DEMO_CONTAINER}/{n}" for n in sorted(names)]


def _parse_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return None


def _classify(paths: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Return (web_ui_path, handwritten_path, warnings) using filename hints,
    falling back to attachment order (first = web UI, second = handwritten)."""
    warnings: list[str] = []
    web, hand = None, None
    for p in paths:
        low = blob_basename(p).lower()
        if web is None and any(h in low for h in _WEB_HINTS):
            web = p
        elif hand is None and any(h in low for h in _HAND_HINTS):
            hand = p

    leftovers = [p for p in paths if p not in (web, hand)]
    if web is None and leftovers:
        web = leftovers.pop(0)
        warnings.append(f"Could not identify the web-UI form by name; using '{blob_basename(web)}'.")
    if hand is None and leftovers:
        hand = leftovers.pop(0)
        warnings.append(f"Could not identify the handwritten form by name; using '{blob_basename(hand)}'.")
    return web, hand, warnings


def process(
    attachment_paths: list[str],
    run_agent: Callable[[str], dict],
) -> Iterator[dict]:
    """Generator yielding SSE event dicts for the onboarding verification pipeline.

    `run_agent(content) -> {status, text, error}` runs the form-compare-ks agent once
    with both forms' text.
    """
    warnings: list[str] = []
    yield {"type": "onboarding_start", "attachments": attachment_paths, "ts": time.time()}

    if len(attachment_paths) < 2:
        yield {
            "type": "onboarding_done",
            "files": [],
            "warnings": ["Onboarding verification needs TWO forms (web-UI + handwritten); "
                         f"found {len(attachment_paths)} attachment(s)."],
            "ts": time.time(),
        }
        return

    web_path, hand_path, cls_warnings = _classify(attachment_paths)
    warnings.extend(cls_warnings)
    yield {
        "type": "forms_identified",
        "web_ui": blob_basename(web_path) if web_path else None,
        "handwritten": blob_basename(hand_path) if hand_path else None,
        "ts": time.time(),
    }

    try:
        web_text = extract_text(web_path)
        yield {"type": "extracted", "name": blob_basename(web_path), "role": "web_ui",
               "chars": len(web_text), "ts": time.time()}
        hand_text = extract_text(hand_path)
        yield {"type": "extracted", "name": blob_basename(hand_path), "role": "handwritten",
               "chars": len(hand_text), "ts": time.time()}
    except Exception as exc:  # noqa: BLE001
        yield {"type": "onboarding_done", "files": [],
               "warnings": warnings + [f"Extraction failed: {type(exc).__name__}: {str(exc)[:200]}"],
               "ts": time.time()}
        return

    if len(web_text) > MAX_FORM_CHARS:
        warnings.append(f"Web-UI form trimmed to {MAX_FORM_CHARS} chars (was {len(web_text)}).")
        web_text = web_text[:MAX_FORM_CHARS]
    if len(hand_text) > MAX_FORM_CHARS:
        warnings.append(f"Handwritten form trimmed to {MAX_FORM_CHARS} chars (was {len(hand_text)}).")
        hand_text = hand_text[:MAX_FORM_CHARS]

    combined = (
        "=== FORM A — WEB UI ===\n" + web_text.strip() +
        "\n\n=== FORM B — HANDWRITTEN ===\n" + hand_text.strip() + "\n"
    )
    res = run_agent(combined)
    yield {"type": "aligned", "status": res.get("status"), "ts": time.time()}
    if res.get("status") != "completed":
        detail = res.get("error")
        msg = "form-compare-ks did not complete."
        if detail:
            msg += f" {detail.get('message') if isinstance(detail, dict) else detail}"
        yield {"type": "onboarding_done", "files": [],
               "warnings": warnings + [msg], "ts": time.time()}
        return

    data_obj = _parse_json(res.get("text", ""))
    if not data_obj:
        yield {"type": "onboarding_done", "files": [],
               "warnings": warnings + ["Could not parse the alignment JSON from the agent."],
               "ts": time.time()}
        return

    result = fcmp.compare_aligned(
        data_obj.get("fields") or [],
        data_obj.get("only_in_web"),
        data_obj.get("only_in_handwritten"),
    )
    merchant = (data_obj.get("merchant_name") or "").strip()
    meta = {
        "web_ui_file": blob_basename(web_path) if web_path else "",
        "handwritten_file": blob_basename(hand_path) if hand_path else "",
    }
    verdict = fcmp.result_to_dict(result)
    yield {
        "type": "compared",
        "status": result.status,
        "matchPct": result.match_pct,
        "matched": len(result.matched),
        "near": len(result.near),
        "mismatched": len(result.mismatched),
        "total": result.total_common,
        "ts": time.time(),
    }

    safe = re.sub(r"[^A-Za-z0-9]+", "_", merchant or "merchant").strip("_")[:24] or "merchant"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    report_txt = fcmp.render_report(result, merchant=merchant, note_meta=meta)
    report_json = json.dumps({"merchant_name": merchant, **meta, **verdict}, indent=2)

    written = []
    for fname, text in (
        (f"ONBOARDING_{safe}_{stamp}.txt", report_txt),
        (f"ONBOARDING_{safe}_{stamp}.json", report_json),
    ):
        try:
            url = upload_text(OUTPUT_CONTAINER, f"{OUTPUT_PREFIX}{fname}", text)
            written.append({"file": fname, "url": url, "lines": text.count("\n")})
            yield {"type": "output_written", "file": fname, "url": url,
                   "preview": text[:600], "ts": time.time()}
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{fname}: upload failed — {str(exc)[:200]}")
            yield {"type": "output_error", "file": fname, "error": str(exc)[:200], "ts": time.time()}

    yield {
        "type": "onboarding_done",
        "files": written,
        "status": result.status,
        "matchPct": result.match_pct,
        "verdict": verdict,
        "warnings": warnings,
        "ts": time.time(),
    }
