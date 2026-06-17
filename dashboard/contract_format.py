"""
Contract-note ASCII formatter (PIS LEC upload format).

Turns normalised contract-note data (as extracted by `contract-note-ks`) into the
pipe-delimited `H`/`T` ASCII files defined by the customer's instructions doc
("Structure of ASCII file which will be used to upload LEC transactions in PIS
Software").

Spec (authoritative — from the instructions doc):

  Header `H` (12 fields):
    1  Record Type            'H'
    2  Broker Client Code     char(10)
    3  Trade Date             YYYYMMDD
    4  Contract Note No.      char(40)
    5  Tax Amount (GST)       dec(10,2)
    6  Education Cess         dec(10,2)
    7  Exchange Levy          dec(10,2)
    8  STT                    dec(10,2)
    9  Stamp Duty             dec(10,2)
    10 Others                 dec(10,2)
    11 No. of Transactions    int  (= count of T rows under this H)
    12 Total Contract Note Amt dec(15,2)

  Detail `T` (9 fields):
    1  Record Type            'T'
    2  Broker Client Code     char(10)
    3  Contract Note No.      char(40)
    4  Transaction Type       'S' | 'P'
    5  ISIN                   char(12)
    6  Quantity               dec(16,3)
    7  Rate Per Scrip         dec(15,4)
    8  Brokerage Rate/Scrip   dec(20,10)
    9  Transaction Amount     dec(16,2)
         Sales:    Quantity * (Rate - Brokerage)
         Purchase: Quantity * (Rate + Brokerage)

Other instructions:
  - Exchange-wise files; filename starts with the exchange (e.g. NSE_20090505.TXT).
  - Two files per broker per trade date: one Purchase, one Sales.
  - Header transaction count must equal the actual number of T rows.
  - Header total must reconcile to the trade amounts plus all header charges.

Sign convention: the provided samples carry **Purchase amounts as negative** and
Sales as positive, and the header total only reconciles to the samples when signed
that way. The doc's bare formula is unsigned; we follow the samples here. Flip
`PURCHASE_NEGATIVE` to disable.

Field precision is centralised below so it can be matched to either the doc spec
(default) or the simpler sample style if the customer prefers.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

SEP = "|"

# --- precision (doc spec) --------------------------------------------------- #
QTY_DECIMALS = 3
RATE_DECIMALS = 4
BROKERAGE_DECIMALS = 10
AMOUNT_DECIMALS = 2

PURCHASE_NEGATIVE = True  # samples show Purchase amounts (and header total) negative

# Default security master: agents/security_master.csv (repo-root/agents).
DEFAULT_MASTER = Path(__file__).resolve().parents[1] / "agents" / "security_master.csv"


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _fmt(value, decimals: int) -> str:
    q = Decimal(1).scaleb(-decimals)  # e.g. 0.01 for 2 dp
    return str(_dec(value).quantize(q, rounding=ROUND_HALF_UP))


def trade_amount(qty, rate, brokerage, ttype: str) -> Decimal:
    """Signed transaction amount per the doc formula + sample sign convention."""
    q, r, b = _dec(qty), _dec(rate), _dec(brokerage)
    if ttype == "S":
        magnitude = q * (r - b)
        sign = Decimal(1)
    else:  # Purchase
        magnitude = q * (r + b)
        sign = Decimal(-1) if PURCHASE_NEGATIVE else Decimal(1)
    return (magnitude * sign).quantize(
        Decimal(1).scaleb(-AMOUNT_DECIMALS), rounding=ROUND_HALF_UP
    )


# --------------------------------------------------------------------------- #
# Security master (scrip name -> ISIN)
# --------------------------------------------------------------------------- #
def _norm_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (name or "").upper()).strip()


def load_security_master(path: Path | str | None = None) -> dict[str, str]:
    """Load a `name|isin` (or `name,isin`) lookup keyed by normalised scrip name."""
    p = Path(path) if path else DEFAULT_MASTER
    table: dict[str, str] = {}
    if not p.exists():
        return table
    with p.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(1024)
        fh.seek(0)
        delim = "|" if sample.count("|") >= sample.count(",") else ","
        for row in csv.reader(fh, delimiter=delim):
            if len(row) < 2:
                continue
            name, isin = row[0].strip(), row[1].strip()
            if not name or name.startswith("#"):
                continue
            if name.lower() in {"scrip_name", "name", "symbol"}:
                continue
            table[_norm_name(name)] = isin
    return table


def resolve_isin(scrip_name: str, master: dict[str, str]) -> str:
    """Best-effort scrip-name -> ISIN. Exact normalised match, then prefix match."""
    key = _norm_name(scrip_name)
    if not key:
        return ""
    if key in master:
        return master[key]
    for name, isin in master.items():
        if name.startswith(key) or key.startswith(name):
            return isin
    return ""


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    transaction_type: str
    isin: str
    scrip_name: str
    quantity: float
    rate_per_scrip: float
    brokerage_rate_per_scrip: float


@dataclass
class ContractNote:
    broker_client_code: str
    trade_date: str
    contract_note_no: str
    exchange: str
    transaction_type: str
    tax_amount: float = 0.0
    education_cess: float = 0.0
    exchange_levy: float = 0.0
    stt: float = 0.0
    stamp_duty: float = 0.0
    others: float = 0.0
    trades: list[Trade] = field(default_factory=list)

    def total_charges(self) -> Decimal:
        return (
            _dec(self.tax_amount) + _dec(self.education_cess) + _dec(self.exchange_levy)
            + _dec(self.stt) + _dec(self.stamp_duty) + _dec(self.others)
        )


def _trade_line(note: ContractNote, t: Trade) -> str:
    return SEP.join(
        [
            "T",
            note.broker_client_code,
            note.contract_note_no,
            t.transaction_type,
            t.isin,
            _fmt(t.quantity, QTY_DECIMALS),
            _fmt(t.rate_per_scrip, RATE_DECIMALS),
            _fmt(t.brokerage_rate_per_scrip, BROKERAGE_DECIMALS),
            _fmt(trade_amount(t.quantity, t.rate_per_scrip, t.brokerage_rate_per_scrip, t.transaction_type), AMOUNT_DECIMALS),
        ]
    )


def _header_total(note: ContractNote) -> Decimal:
    trades_sum = sum(
        (trade_amount(t.quantity, t.rate_per_scrip, t.brokerage_rate_per_scrip, t.transaction_type)
         for t in note.trades),
        Decimal(0),
    )
    # Charges increase a purchase debit / reduce sale proceeds -> subtract in both.
    return (trades_sum - note.total_charges()).quantize(
        Decimal(1).scaleb(-AMOUNT_DECIMALS), rounding=ROUND_HALF_UP
    )


def _header_line(note: ContractNote) -> str:
    return SEP.join(
        [
            "H",
            note.broker_client_code,
            note.trade_date,
            note.contract_note_no,
            _fmt(note.tax_amount, AMOUNT_DECIMALS),
            _fmt(note.education_cess, AMOUNT_DECIMALS),
            _fmt(note.exchange_levy, AMOUNT_DECIMALS),
            _fmt(note.stt, AMOUNT_DECIMALS),
            _fmt(note.stamp_duty, AMOUNT_DECIMALS),
            _fmt(note.others, AMOUNT_DECIMALS),
            str(len(note.trades)),
            _fmt(_header_total(note), AMOUNT_DECIMALS),
        ]
    )


def format_note(note: ContractNote) -> str:
    """One contract note -> its H line followed by its T lines."""
    lines = [_header_line(note)]
    lines.extend(_trade_line(note, t) for t in note.trades)
    return "\n".join(lines)


def _filename(exchange: str, ttype: str, trade_date: str) -> str:
    """Exchange-first filename (doc rule), with Purchase/Sales split."""
    kind = "SALE" if ttype == "S" else "PUR"
    ex = (exchange or "XXX").upper()
    return f"{ex}_{kind}_{trade_date}.txt"


def group_and_format(notes: list[ContractNote]) -> tuple[dict[str, str], list[str]]:
    """Group notes by (exchange, transaction type) -> {filename: file_text}.

    Returns (files, warnings). One file per exchange × Purchase/Sales, per the doc
    ("exchange-wise files ... two files per broker per trade date").
    """
    warnings: list[str] = []
    groups: dict[tuple[str, str], list[ContractNote]] = {}
    for n in notes:
        types = {t.transaction_type for t in n.trades} or {n.transaction_type}
        if len(types) > 1:
            warnings.append(
                f"Contract note {n.contract_note_no} mixes Buy and Sell rows; "
                f"grouped by header type '{n.transaction_type}'."
            )
        key = (n.exchange.upper(), n.transaction_type)
        groups.setdefault(key, []).append(n)

    files: dict[str, str] = {}
    for (exchange, ttype), grp in groups.items():
        trade_date = grp[0].trade_date or ""
        text = "\n".join(format_note(n) for n in grp) + "\n"
        files[_filename(exchange, ttype, trade_date)] = text
    return files, warnings


def combine_and_format(notes: list[ContractNote]) -> tuple[dict[str, str], list[str]]:
    """All notes from one email (any number of attachments) -> ONE PIS file.

    The PIS ASCII format is a flat sequence of `H` ... `T` ... blocks, so multiple
    contract notes (and both Purchase and Sales rows) live in a single file. This
    keeps one email = one output file, regardless of how many attachments it had.
    Returns ({filename: text}, warnings).
    """
    if not notes:
        return {}, []
    warnings: list[str] = []
    exchanges = sorted({n.exchange.upper() for n in notes if n.exchange})
    if len(exchanges) > 1:
        warnings.append(
            "Email has contract notes from multiple exchanges "
            f"({', '.join(exchanges)}); combined into a single file."
        )
    ex = exchanges[0] if len(exchanges) == 1 else "MULTI"
    trade_date = next((n.trade_date for n in notes if n.trade_date), "") or "UNDATED"
    note_no = next((n.contract_note_no for n in notes if n.contract_note_no), "")
    suffix = re.sub(r"[^A-Za-z0-9]+", "", note_no)[:12]
    fname = f"{ex}_{trade_date}{('_' + suffix) if suffix else ''}.txt"
    text = "\n".join(format_note(n) for n in notes) + "\n"
    return {fname: text}, warnings


# --------------------------------------------------------------------------- #
# Build notes from the agent's JSON, resolving ISINs
# --------------------------------------------------------------------------- #
def note_from_dict(data: dict, master: dict[str, str] | None = None) -> tuple[ContractNote, list[str]]:
    """Build a ContractNote from the extraction-agent JSON; resolve missing ISINs."""
    master = master if master is not None else {}
    warnings: list[str] = []
    htype = (data.get("transaction_type") or "P").upper()[:1]
    trades: list[Trade] = []
    for t in data.get("trades", []) or []:
        ttype = (t.get("transaction_type") or htype).upper()[:1]
        isin = (t.get("isin") or "").strip().upper()
        scrip = (t.get("scrip_name") or "").strip()
        if not isin:
            isin = resolve_isin(scrip, master)
            if not isin:
                warnings.append(f"ISIN unresolved for '{scrip}' — needs review.")
        trades.append(
            Trade(
                transaction_type=ttype,
                isin=isin,
                scrip_name=scrip,
                quantity=t.get("quantity") or 0,
                rate_per_scrip=t.get("rate_per_scrip") or 0,
                brokerage_rate_per_scrip=t.get("brokerage_rate_per_scrip") or 0,
            )
        )
    note = ContractNote(
        broker_client_code=(data.get("broker_client_code") or "").strip(),
        trade_date=(data.get("trade_date") or "").strip(),
        contract_note_no=(data.get("contract_note_no") or "").strip(),
        exchange=(data.get("exchange") or "").strip().upper(),
        transaction_type=htype,
        tax_amount=data.get("tax_amount") or 0,
        education_cess=data.get("education_cess") or 0,
        exchange_levy=data.get("exchange_levy") or 0,
        stt=data.get("stt") or 0,
        stamp_duty=data.get("stamp_duty") or 0,
        others=data.get("others") or 0,
        trades=trades,
    )
    return note, warnings
