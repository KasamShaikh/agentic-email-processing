"""
Test the contract-note formatter (and, optionally, the live extraction pipeline).

Default (offline, no Azure): rebuilds the two notes whose maths we verified against
the provided sample output files and asserts the formatter reproduces the exact
header totals and transaction amounts, plus the exchange/Buy-Sell file grouping.

    python agents/test_contract_note.py

Live mode (needs `az login` + the dashboard deps): runs a local PDF/image file
through Azure AI Document Intelligence + the contract-note agent + the formatter,
and prints the generated ASCII file(s) — nothing is uploaded.

    python agents/test_contract_note.py --live "<path-to-pdf-or-image>"
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# Import the formatter from the dashboard package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import contract_format as cf  # noqa: E402


def _purchase_note() -> cf.ContractNote:
    # Verified against NSE_AXIS_Pur_ANANDRATHI_20260526.txt -> header total -9312.92
    return cf.ContractNote(
        broker_client_code="DIRB28M107",
        trade_date="20260526",
        contract_note_no="0502915",
        exchange="NSE",
        transaction_type="P",
        tax_amount=8.38,
        exchange_levy=0.29,
        stt=9.00,
        stamp_duty=1.00,
        trades=[
            cf.Trade("P", "INE397D01024", "BHARTI AIRTEL LIMITED", 4, 1849.6000, 9.2500),
            cf.Trade("P", "INE397D01024", "BHARTI AIRTEL LIMITED", 1, 1849.6000, 9.2500),
        ],
    )


def _sale_note() -> cf.ContractNote:
    # Verified against AXIS_NSE_SALE_20260526_DBFS-SAMPLE.txt -> header total 97158.11
    return cf.ContractNote(
        broker_client_code="KOD2021-NC",
        trade_date="20260526",
        contract_note_no="22392-26",
        exchange="NSE",
        transaction_type="S",
        tax_amount=123.57,
        stt=98.00,
        trades=[
            cf.Trade("S", "INE151A01013", "TATA COMMUNICATIONS LTD", 14, 2005.00, 14.0350),
            cf.Trade("S", "INE151A01013", "TATA COMMUNICATIONS LTD", 9, 2005.00, 14.0350),
            cf.Trade("S", "INE750C01026", "MARKSANS PHARMA LIMITED", 32, 248.70, 1.7409),
            cf.Trade("S", "INE750C01026", "MARKSANS PHARMA LIMITED", 17, 248.69, 1.7408),
            cf.Trade("S", "INE750C01026", "MARKSANS PHARMA LIMITED", 41, 248.61, 1.7403),
            cf.Trade("S", "INE0CLI01024", "RATEGAIN TRAVEL TECHNOLOGIES LIMITED", 21, 739.30, 5.1751),
            cf.Trade("S", "INE0CLI01024", "RATEGAIN TRAVEL TECHNOLOGIES LIMITED", 19, 739.30, 5.1751),
        ],
    )


def offline_test() -> int:
    failures = 0

    # 1) Transaction-amount formula (purchase negative, sale positive).
    amt_pur = cf.trade_amount(4, 1849.6000, 9.2500, "P")
    if amt_pur != Decimal("-7435.40"):
        print(f"FAIL purchase amount: {amt_pur} != -7435.40")
        failures += 1
    amt_sale = cf.trade_amount(14, 2005.00, 14.0350, "S")
    if amt_sale != Decimal("27873.51"):
        print(f"FAIL sale amount: {amt_sale} != 27873.51")
        failures += 1

    # 2) Header-total reconciliation (trades +/- charges) for both notes.
    pur_total = cf._header_total(_purchase_note())
    if pur_total != Decimal("-9312.92"):
        print(f"FAIL purchase header total: {pur_total} != -9312.92")
        failures += 1
    sale_total = cf._header_total(_sale_note())
    if sale_total != Decimal("97158.11"):
        print(f"FAIL sale header total: {sale_total} != 97158.11")
        failures += 1

    # 3) Record structure: H = 12 fields, T = 9 fields, count matches.
    note = _purchase_note()
    lines = cf.format_note(note).splitlines()
    h = lines[0].split("|")
    if not (h[0] == "H" and len(h) == 12):
        print(f"FAIL header field count: {len(h)} (expected 12)")
        failures += 1
    if h[10] != str(len(note.trades)):
        print(f"FAIL header trade count: {h[10]} != {len(note.trades)}")
        failures += 1
    for tl in lines[1:]:
        t = tl.split("|")
        if not (t[0] == "T" and len(t) == 9):
            print(f"FAIL trade field count: {len(t)} (expected 9)")
            failures += 1
            break

    # 4) Grouping by exchange x Buy/Sell -> two files, exchange-first names.
    files, warnings = cf.group_and_format([_purchase_note(), _sale_note()])
    if set(files) != {"NSE_PUR_20260526.txt", "NSE_SALE_20260526.txt"}:
        print(f"FAIL filenames: {sorted(files)}")
        failures += 1

    print("\n--- Sample output: NSE_PUR_20260526.txt ---")
    print(files.get("NSE_PUR_20260526.txt", "").rstrip())
    print("\n--- Sample output: NSE_SALE_20260526.txt ---")
    print(files.get("NSE_SALE_20260526.txt", "").rstrip())
    if warnings:
        print("\nWarnings:", warnings)

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return failures


def live_test(file_path: str) -> int:
    import json
    import os

    from azure.identity import DefaultAzureCredential
    from azure.ai.agents import AgentsClient

    import contract_pipeline as cp

    endpoint = os.environ.get(
        "PROJECT_ENDPOINT",
        "https://agentic-email-foundry-ks.services.ai.azure.com/api/projects/email-agentic-ks",
    )
    cred = DefaultAzureCredential()
    agents = AgentsClient(
        endpoint=endpoint, credential=cred,
        credential_scopes=["https://ai.azure.com/.default"],
    )
    contract_id = next(
        (a.id for a in agents.list_agents() if a.name == "contract-note-ks"), None
    )
    if not contract_id:
        print("contract-note-ks not found — run deploy_agents.py first.")
        return 1

    data = Path(file_path).read_bytes()
    print(f"Analyzing {file_path} with Document Intelligence ...")
    content = cp.analyze_document(data)
    print(f"  extracted {len(content)} chars")

    def run_agent(c: str) -> dict:
        thread = agents.threads.create()
        agents.messages.create(thread_id=thread.id, role="user", content=c)
        run = agents.runs.create_and_process(thread_id=thread.id, agent_id=contract_id)
        text = ""
        for msg in agents.messages.list(thread_id=thread.id):
            md = msg.as_dict()
            if md.get("role") == "assistant":
                text = "\n".join(
                    p.get("text", {}).get("value", "")
                    for p in md.get("content", []) if p.get("type") == "text"
                ).strip()
                if text:
                    break
        return {"status": str(run.status).split(".")[-1].lower(), "text": text}

    res = run_agent(content)
    print(f"  agent status: {res['status']}")
    obj = cp._parse_json(res["text"])
    if not obj:
        print("Could not parse JSON from agent. Raw:\n", res["text"][:1000])
        return 1
    note, warnings = cf.note_from_dict(obj, cf.load_security_master())
    files, gw = cf.group_and_format([note])
    for name, text in files.items():
        print(f"\n--- {name} ---\n{text.rstrip()}")
    if warnings or gw:
        print("\nWarnings:", warnings + gw)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--live":
        sys.exit(live_test(sys.argv[2]))
    sys.exit(offline_test())
