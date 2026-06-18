"""
Deterministic two-form onboarding comparator.

The merchant-onboarding "checker" verifies that a HANDWRITTEN form (scanned image)
and a WEB-UI form (portal screenshot or JSON) carry the same field values. The LLM
agent (form-compare-ks) does the fuzzy work it is good at — reading handwriting and
*aligning* equivalent field labels across the two forms (e.g. "Name" == "Merchant
Name"). This module does the deterministic part — value normalisation, similarity
scoring, the match/near/mismatch verdict and the overall match percentage — so the
result is stable, explainable and auditable (same split as contract_format.py).

Input is the agent's aligned output:

    {
      "fields": [
        {"field": "Name", "web_ui": "ACME Traders", "handwritten": "Acme Traders"},
        ...
      ],
      "only_in_web": ["GST No"],
      "only_in_handwritten": []
    }

`compare_forms(web, hand)` is a deterministic fallback that aligns two independent
field dicts by a canonicalised label (used when both inputs are structured JSON).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# Similarity thresholds (on normalised values).
MATCH_THRESHOLD = 0.95   # >= this -> values are considered the same
NEAR_THRESHOLD = 0.80    # [NEAR, MATCH) -> needs human review (likely OCR noise)

MATCH = "match"
NEAR = "near"
MISMATCH = "mismatch"


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
_PUNCT = re.compile(r"[^A-Z0-9]+")
_DIGITS = re.compile(r"\D+")


def _norm(value) -> str:
    """Uppercase, strip punctuation, collapse whitespace — for value comparison."""
    s = "" if value is None else str(value)
    return _PUNCT.sub(" ", s.upper()).strip()


def _norm_label(label) -> str:
    """Canonical key for pairing field labels (drops only trailing noise words).
    Deliberately conservative: semantic alignment of differently-named fields
    (e.g. 'Merchant Name' ~ 'Name') is the agent's job, not this fallback's."""
    s = _norm(label)
    drop = {"NO", "NUMBER", "DETAILS"}
    toks = [t for t in s.split() if t not in drop]
    return " ".join(toks) or s


def _similar(a: str, b: str) -> float:
    """Similarity in [0,1]. Pure-numeric values compared on digits only."""
    na, nb = _norm(a), _norm(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    da, db = _DIGITS.sub("", na), _DIGITS.sub("", nb)
    # If both sides are essentially numbers (account/PAN/phone), compare digits.
    if da and db and len(da) >= len(na) - na.count(" ") and len(db) >= len(nb) - nb.count(" "):
        return 1.0 if da == db else SequenceMatcher(None, da, db).ratio()
    return SequenceMatcher(None, na, nb).ratio()


def _verdict(score: float) -> str:
    if score >= MATCH_THRESHOLD:
        return MATCH
    if score >= NEAR_THRESHOLD:
        return NEAR
    return MISMATCH


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class FieldComparison:
    field: str
    web_ui: str
    handwritten: str
    similarity: float
    verdict: str


@dataclass
class CompareResult:
    match_pct: float
    total_common: int
    matched: list[FieldComparison] = field(default_factory=list)
    near: list[FieldComparison] = field(default_factory=list)
    mismatched: list[FieldComparison] = field(default_factory=list)
    only_in_web: list[str] = field(default_factory=list)
    only_in_handwritten: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.mismatched:
            return "mismatch"
        if self.near:
            return "review"
        return "verified"


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def compare_aligned(
    pairs: list[dict],
    only_in_web: list[str] | None = None,
    only_in_handwritten: list[str] | None = None,
) -> CompareResult:
    """Score the agent's already-aligned field pairs into a verdict + match %."""
    matched: list[FieldComparison] = []
    near: list[FieldComparison] = []
    mismatched: list[FieldComparison] = []

    for p in pairs or []:
        label = str(p.get("field", "")).strip()
        web = "" if p.get("web_ui") is None else str(p.get("web_ui"))
        hand = "" if p.get("handwritten") is None else str(p.get("handwritten"))
        score = round(_similar(web, hand), 4)
        fc = FieldComparison(label, web, hand, score, _verdict(score))
        {MATCH: matched, NEAR: near, MISMATCH: mismatched}[fc.verdict].append(fc)

    total = len(matched) + len(near) + len(mismatched)
    pct = round(100.0 * len(matched) / total, 1) if total else 0.0
    return CompareResult(
        match_pct=pct,
        total_common=total,
        matched=matched,
        near=near,
        mismatched=mismatched,
        only_in_web=list(only_in_web or []),
        only_in_handwritten=list(only_in_handwritten or []),
    )


def compare_forms(web: dict, hand: dict) -> CompareResult:
    """Deterministic fallback: align two independent {label: value} dicts by a
    canonicalised label, then compare. Used when both forms are structured JSON."""
    web = web or {}
    hand = hand or {}
    web_keys = {_norm_label(k): k for k in web}
    hand_keys = {_norm_label(k): k for k in hand}
    common = [k for k in web_keys if k in hand_keys]

    pairs = [
        {
            "field": web_keys[k],
            "web_ui": web[web_keys[k]],
            "handwritten": hand[hand_keys[k]],
        }
        for k in common
    ]
    only_web = [web_keys[k] for k in web_keys if k not in hand_keys]
    only_hand = [hand_keys[k] for k in hand_keys if k not in web_keys]
    return compare_aligned(pairs, only_web, only_hand)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def result_to_dict(res: CompareResult) -> dict:
    def rows(items: list[FieldComparison]) -> list[dict]:
        return [
            {
                "field": f.field,
                "web_ui": f.web_ui,
                "handwritten": f.handwritten,
                "similarity": f.similarity,
                "verdict": f.verdict,
            }
            for f in items
        ]

    return {
        "status": res.status,
        "match_pct": res.match_pct,
        "total_common_fields": res.total_common,
        "matched": rows(res.matched),
        "near_match": rows(res.near),
        "mismatched": rows(res.mismatched),
        "only_in_web_ui": res.only_in_web,
        "only_in_handwritten": res.only_in_handwritten,
    }


def render_report(res: CompareResult, merchant: str = "", note_meta: dict | None = None) -> str:
    """Human-readable verification report."""
    meta = note_meta or {}
    lines: list[str] = []
    lines.append("MERCHANT ONBOARDING — FORM VERIFICATION")
    lines.append("=" * 48)
    if merchant:
        lines.append(f"Merchant      : {merchant}")
    if meta.get("web_ui_file"):
        lines.append(f"Web-UI form   : {meta['web_ui_file']}")
    if meta.get("handwritten_file"):
        lines.append(f"Handwritten   : {meta['handwritten_file']}")
    lines.append(f"Verdict       : {res.status.upper()}")
    lines.append(f"Match         : {res.match_pct}%  ({len(res.matched)}/{res.total_common} common fields)")
    lines.append("")

    if res.mismatched:
        lines.append(f"MISMATCHED FIELDS ({len(res.mismatched)}) — require correction:")
        for f in res.mismatched:
            lines.append(f"  - {f.field}: web='{f.web_ui}'  vs  handwritten='{f.handwritten}'  (sim {f.similarity})")
        lines.append("")
    if res.near:
        lines.append(f"NEAR MATCHES ({len(res.near)}) — likely OCR noise, review:")
        for f in res.near:
            lines.append(f"  - {f.field}: web='{f.web_ui}'  vs  handwritten='{f.handwritten}'  (sim {f.similarity})")
        lines.append("")
    if res.only_in_web:
        lines.append("ONLY IN WEB-UI form: " + ", ".join(res.only_in_web))
    if res.only_in_handwritten:
        lines.append("ONLY IN HANDWRITTEN form: " + ", ".join(res.only_in_handwritten))
    if res.matched:
        lines.append("")
        lines.append(f"MATCHED FIELDS ({len(res.matched)}):")
        for f in res.matched:
            lines.append(f"  - {f.field}: '{f.web_ui}'")
    return "\n".join(lines) + "\n"
