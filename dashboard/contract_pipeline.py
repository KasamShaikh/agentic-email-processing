"""
Contract-note processing pipeline.

For a contract-note email, for each attachment:
  1. download the file from the `incoming-attachments` container (managed identity /
     Entra ID — shared-key auth is disabled by policy on this account),
  2. extract its text + tables with Azure AI Document Intelligence (`prebuilt-layout`,
     markdown output — works for PDF and image),
  3. have `contract-note-ks` normalise it into structured JSON,
  4. resolve ISINs from the security master,
then group all notes by exchange × Purchase/Sales, format the PIS `H`/`T` ASCII
files, and upload them to the `contract-notes-output` container.

Runs as a generator that yields plain SSE event dicts so the dashboard can show
every step live. All Azure access uses `DefaultAzureCredential`.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Iterator

from contract_format import (
    ContractNote,
    group_and_format,
    load_security_master,
    note_from_dict,
)
from doc_extract import (
    blob_basename,
    download_blob,
    extract_text,
    upload_text,
)

OUTPUT_CONTAINER = "contract-notes-output"

_master: dict[str, str] | None = None


def _security_master() -> dict[str, str]:
    global _master
    if _master is None:
        _master = load_security_master()
    return _master


def _parse_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return None


def process(
    attachment_paths: list[str],
    run_agent: Callable[[str], dict],
) -> Iterator[dict]:
    """Generator yielding SSE event dicts for the whole contract-note pipeline.

    `run_agent(content) -> {status, text, error}` runs the contract-note agent.
    """
    master = _security_master()
    notes: list[ContractNote] = []
    warnings: list[str] = []

    yield {"type": "contract_start", "attachments": attachment_paths, "ts": time.time()}

    if not attachment_paths:
        yield {
            "type": "contract_done",
            "files": [],
            "warnings": ["No attachments found on this email — nothing to process."],
            "ts": time.time(),
        }
        return

    for path in attachment_paths:
        name = blob_basename(path)
        try:
            data = download_blob(path)
            yield {"type": "attachment_fetched", "name": name, "bytes": len(data), "ts": time.time()}

            content = extract_text(path, data)
            yield {"type": "extracted", "name": name, "chars": len(content), "ts": time.time()}

            res = run_agent(content)
            yield {
                "type": "normalized",
                "name": name,
                "status": res.get("status"),
                "ts": time.time(),
            }
            if res.get("status") != "completed":
                warnings.append(f"{name}: extraction agent did not complete.")
                continue

            data_obj = _parse_json(res.get("text", ""))
            if not data_obj:
                warnings.append(f"{name}: could not parse structured JSON from the agent.")
                continue

            note, note_warnings = note_from_dict(data_obj, master)
            warnings.extend(f"{name}: {w}" for w in note_warnings)
            if note.trades:
                notes.append(note)
                yield {
                    "type": "note_parsed",
                    "name": name,
                    "contractNoteNo": note.contract_note_no,
                    "exchange": note.exchange,
                    "transactionType": note.transaction_type,
                    "trades": len(note.trades),
                    "ts": time.time(),
                }
            else:
                warnings.append(f"{name}: no trade rows found.")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
            yield {"type": "attachment_error", "name": name, "error": str(exc)[:200], "ts": time.time()}

    files, group_warnings = group_and_format(notes)
    warnings.extend(group_warnings)

    written = []
    for fname, text in files.items():
        try:
            url = upload_text(OUTPUT_CONTAINER, fname, text)
            written.append({"file": fname, "url": url, "lines": text.count("\n")})
            yield {
                "type": "output_written",
                "file": fname,
                "url": url,
                "preview": text[:600],
                "ts": time.time(),
            }
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{fname}: upload failed — {str(exc)[:200]}")
            yield {"type": "output_error", "file": fname, "error": str(exc)[:200], "ts": time.time()}

    yield {
        "type": "contract_done",
        "files": written,
        "notes": len(notes),
        "warnings": warnings,
        "ts": time.time(),
    }
