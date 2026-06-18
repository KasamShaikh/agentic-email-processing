"""
Merchant-onboarding form-verification pipeline.

For a pre-onboarding email with two attachments — a WEB-UI form (portal screenshot or
JSON export) and a HANDWRITTEN form (scanned image) — this pipeline:
  1. downloads both files from `incoming-attachments` (managed identity — shared-key
     auth is disabled by policy on this account),
  2. turns each into text: Azure AI Document Intelligence (`prebuilt-layout`, markdown)
     for images/PDFs, or direct decode for `.json`,
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

from azure.identity import DefaultAzureCredential

import form_compare as fcmp

DOCINTEL_ENDPOINT = "https://agentic-email-docintel-ks.cognitiveservices.azure.com/"
STORAGE_ACCOUNT_URL = "https://agenticemailks.blob.core.windows.net"
INPUT_CONTAINER = "incoming-attachments"
OUTPUT_CONTAINER = "contract-notes-output"
OUTPUT_PREFIX = "onboarding/"

_credential = DefaultAzureCredential()
_blob_service = None
_doc_client = None

# Filename hints used to tell the two forms apart.
_WEB_HINTS = ("web", "ui", "portal", "screen", "json", "online")
_HAND_HINTS = ("hand", "scan", "written", "manual", "paper")


def _blob():
    global _blob_service
    if _blob_service is None:
        from azure.storage.blob import BlobServiceClient

        _blob_service = BlobServiceClient(STORAGE_ACCOUNT_URL, credential=_credential)
    return _blob_service


def _docintel():
    global _doc_client
    if _doc_client is None:
        from azure.ai.documentintelligence import DocumentIntelligenceClient

        _doc_client = DocumentIntelligenceClient(DOCINTEL_ENDPOINT, credential=_credential)
    return _doc_client


def _blob_name(path: str) -> str:
    p = (path or "").lstrip("/")
    prefix = f"{INPUT_CONTAINER}/"
    return p[len(prefix):] if p.startswith(prefix) else p


def download_attachment(path: str) -> bytes:
    name = _blob_name(path)
    client = _blob().get_blob_client(container=INPUT_CONTAINER, blob=name)
    return client.download_blob().readall()


def analyze_document(data: bytes) -> str:
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

    poller = _docintel().begin_analyze_document(
        "prebuilt-layout",
        AnalyzeDocumentRequest(bytes_source=data),
        output_content_format="markdown",
    )
    return poller.result().content or ""


def upload_output(name: str, text: str) -> str:
    blob = f"{OUTPUT_PREFIX}{name}"
    client = _blob().get_blob_client(container=OUTPUT_CONTAINER, blob=blob)
    client.upload_blob(text.encode("utf-8"), overwrite=True)
    return f"{STORAGE_ACCOUNT_URL}/{OUTPUT_CONTAINER}/{blob}"


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
        low = _blob_name(p).lower()
        if web is None and any(h in low for h in _WEB_HINTS):
            web = p
        elif hand is None and any(h in low for h in _HAND_HINTS):
            hand = p

    leftovers = [p for p in paths if p not in (web, hand)]
    if web is None and leftovers:
        web = leftovers.pop(0)
        warnings.append(f"Could not identify the web-UI form by name; using '{_blob_name(web)}'.")
    if hand is None and leftovers:
        hand = leftovers.pop(0)
        warnings.append(f"Could not identify the handwritten form by name; using '{_blob_name(hand)}'.")
    return web, hand, warnings


def _extract_text(path: str) -> str:
    """OCR an image/PDF, or decode a JSON attachment directly."""
    data = download_attachment(path)
    if _blob_name(path).lower().endswith(".json"):
        try:
            return json.dumps(json.loads(data.decode("utf-8")), indent=2)
        except Exception:  # noqa: BLE001
            return data.decode("utf-8", "ignore")
    return analyze_document(data)


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
        "web_ui": _blob_name(web_path) if web_path else None,
        "handwritten": _blob_name(hand_path) if hand_path else None,
        "ts": time.time(),
    }

    try:
        web_text = _extract_text(web_path)
        yield {"type": "extracted", "name": _blob_name(web_path), "role": "web_ui",
               "chars": len(web_text), "ts": time.time()}
        hand_text = _extract_text(hand_path)
        yield {"type": "extracted", "name": _blob_name(hand_path), "role": "handwritten",
               "chars": len(hand_text), "ts": time.time()}
    except Exception as exc:  # noqa: BLE001
        yield {"type": "onboarding_done", "files": [],
               "warnings": warnings + [f"Extraction failed: {type(exc).__name__}: {str(exc)[:200]}"],
               "ts": time.time()}
        return

    combined = (
        "=== FORM A — WEB UI ===\n" + web_text.strip() +
        "\n\n=== FORM B — HANDWRITTEN ===\n" + hand_text.strip() + "\n"
    )
    res = run_agent(combined)
    yield {"type": "aligned", "status": res.get("status"), "ts": time.time()}
    if res.get("status") != "completed":
        yield {"type": "onboarding_done", "files": [],
               "warnings": warnings + ["form-compare-ks did not complete."], "ts": time.time()}
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
        "web_ui_file": _blob_name(web_path) if web_path else "",
        "handwritten_file": _blob_name(hand_path) if hand_path else "",
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
            url = upload_output(fname, text)
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
