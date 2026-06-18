"""
Shared document-extraction helpers used by every agent pipeline — one code path for
turning a blob into text.

Type-aware routing:
  - digital/text formats (json, csv, tsv, txt, xml, html, md) are decoded directly —
    no OCR, since they already contain clean text,
  - everything else (pdf, png, jpg, jpeg, tif, tiff, bmp, ...) goes through Azure AI
    Document Intelligence `prebuilt-layout` (markdown output — tables + handwriting).

Paths are container-qualified (`<container>/<blob>`), so any agent can read from its
own container. A bare name (no slash) resolves to the default input container.

All Azure access uses `DefaultAzureCredential` (managed identity / Entra ID — shared-key
auth is disabled by policy on this account).
"""

from __future__ import annotations

import json

from azure.identity import DefaultAzureCredential

DOCINTEL_ENDPOINT = "https://agentic-email-docintel-ks.cognitiveservices.azure.com/"
STORAGE_ACCOUNT_URL = "https://agenticemailks.blob.core.windows.net"
DEFAULT_CONTAINER = "incoming-attachments"

# File types that already hold digital text — skip Document Intelligence for these.
_TEXT_EXTS = (".json", ".csv", ".tsv", ".txt", ".xml", ".html", ".htm", ".md")

_credential = DefaultAzureCredential()
_blob_service = None
_doc_client = None


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


def split_path(path: str) -> tuple[str, str]:
    """`onboarding/Web-portal.pdf` -> ('onboarding', 'Web-portal.pdf').

    A bare name with no slash resolves to the default input container.
    """
    p = (path or "").lstrip("/")
    container, sep, blob = p.partition("/")
    if not sep:
        return DEFAULT_CONTAINER, p
    return container, blob


def blob_basename(path: str) -> str:
    """Last path segment, for display + filename hints:
    `onboarding/Web-portal.pdf` -> `Web-portal.pdf`."""
    return (path or "").rstrip("/").rsplit("/", 1)[-1]


def download_blob(path: str) -> bytes:
    """Download a container-qualified blob as bytes."""
    container, blob = split_path(path)
    client = _blob().get_blob_client(container=container, blob=blob)
    return client.download_blob().readall()


def analyze_document(data: bytes) -> str:
    """Document content as markdown (tables/handwriting preserved) via prebuilt-layout."""
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

    poller = _docintel().begin_analyze_document(
        "prebuilt-layout",
        AnalyzeDocumentRequest(bytes_source=data),
        output_content_format="markdown",
    )
    return poller.result().content or ""


def is_text_blob(path: str) -> bool:
    """True for digital/text formats that don't need OCR."""
    return blob_basename(path).lower().endswith(_TEXT_EXTS)


def extract_text(path: str, data: bytes | None = None) -> str:
    """Type-aware extraction: decode digital/text blobs directly, OCR everything else.

    `data` may be supplied if the caller has already downloaded the blob (avoids a
    second fetch); otherwise it is downloaded here.
    """
    if data is None:
        data = download_blob(path)
    if is_text_blob(path):
        text = data.decode("utf-8", "ignore")
        if blob_basename(path).lower().endswith(".json"):
            try:
                return json.dumps(json.loads(text), indent=2)
            except Exception:  # noqa: BLE001
                return text
        return text
    return analyze_document(data)


def upload_text(container: str, name: str, text: str) -> str:
    """Upload UTF-8 text to `<container>/<name>`, overwriting. Returns the blob URL."""
    client = _blob().get_blob_client(container=container, blob=name)
    client.upload_blob(text.encode("utf-8"), overwrite=True)
    return f"{STORAGE_ACCOUNT_URL}/{container}/{name}"
