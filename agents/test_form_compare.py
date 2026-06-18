"""
Offline tests for the onboarding form comparator (no Azure needed).

    python agents/test_form_compare.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import form_compare as fc  # noqa: E402


def test_aligned_basic():
    pairs = [
        {"field": "Name", "web_ui": "ACME Traders Pvt Ltd", "handwritten": "Acme Traders Pvt. Ltd."},
        {"field": "PAN", "web_ui": "ABCDE1234F", "handwritten": "ABCDE1234F"},
        {"field": "Account No", "web_ui": "123456789012", "handwritten": "123456789012"},
        {"field": "Phone", "web_ui": "9876543210", "handwritten": "9876543219"},  # one digit off
        {"field": "Address", "web_ui": "12 MG Road, Pune", "handwritten": "99 FC Road, Mumbai"},  # clear mismatch
    ]
    res = fc.compare_aligned(pairs, only_in_web=["GST No"], only_in_handwritten=[])
    d = fc.result_to_dict(res)
    # Name normalises equal, PAN/Account exact -> matched.
    assert any(m["field"] == "Name" for m in d["matched"]), d
    assert any(m["field"] == "PAN" for m in d["matched"]), d
    assert any(m["field"] == "Account No" for m in d["matched"]), d
    # Address is a clear mismatch.
    assert any(m["field"] == "Address" for m in d["mismatched"]), d
    # Phone off by one digit -> near or mismatch, not matched.
    assert all(m["field"] != "Phone" for m in d["matched"]), d
    assert d["only_in_web_ui"] == ["GST No"]
    assert 0 <= d["match_pct"] <= 100


def test_compare_forms_alignment():
    web = {"Merchant Name": "ACME Traders", "Address": "12 MG Road", "Account No": "123456789012", "GST No": "27ABCDE1234F1Z5"}
    hand = {"Merchant Name": "ACME Traders", "Address": "12 MG Road", "Account Number": "123456789012"}
    res = fc.compare_forms(web, hand)
    d = fc.result_to_dict(res)
    # "Account No" <-> "Account Number" align via canonical label; all three match.
    assert d["total_common_fields"] == 3, d
    assert d["match_pct"] == 100.0, d
    assert d["status"] == "verified", d
    assert d["only_in_web_ui"] == ["GST No"], d


def test_all_match_verified():
    pairs = [
        {"field": "Name", "web_ui": "John Doe", "handwritten": "John Doe"},
        {"field": "City", "web_ui": "Pune", "handwritten": "pune"},
    ]
    res = fc.compare_aligned(pairs)
    assert res.status == "verified"
    assert res.match_pct == 100.0


def test_report_renders():
    pairs = [{"field": "Name", "web_ui": "A B Corp", "handwritten": "X Y Corp"}]
    res = fc.compare_aligned(pairs)
    txt = fc.render_report(res, merchant="A B Corp", note_meta={"web_ui_file": "web.png", "handwritten_file": "hand.png"})
    assert "FORM VERIFICATION" in txt and "MISMATCHED FIELDS" in txt


if __name__ == "__main__":
    test_aligned_basic()
    test_compare_forms_alignment()
    test_all_match_verified()
    test_report_renders()
    print("ALL CHECKS PASSED")
