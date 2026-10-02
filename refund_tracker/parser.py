"""Parse a BIDV account statement (.xlsx) into structured transaction records.

The description in column L looks like:
    REM Tfr Ac:6330421880 O@L_0E4001_212001_0_0_2594238187_357273116558295040_
    Phi Bao hiem Bao An Tai Khoan Ky 2 cua ma KH 6330421880

From which we extract:
    order_id  = 357273116558295040   (unique per certificate)
    product   = Bao An Tai Khoan
    ky        = Ky 2
    cif       = 6330421880           (labeled CIF, "cua ma KH")
    dedup_key = order_id | ky | cif | product   (NFC-lowercased)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from datetime import datetime

from python_calamine import CalamineWorkbook

from . import config
from .config import COL, norm

# order_id: the long numeric token immediately before "_Phi"
_ORDER_ID = re.compile(r"_(\d{12,25})_Phi", re.IGNORECASE)
# product / ky / cif in one shot. BIDV sometimes writes "cua ma KH null"; those
# rows are still real charges, so the CIF falls back to the "Tfr Ac:" account.
_DETAIL = re.compile(
    r"Phi Bao hiem\s+(?P<product>.+?)\s+Ky\s+(?P<ky>\S+)\s+cua ma KH\s+(?P<cif>\d+|null)\b",
    re.IGNORECASE,
)


@dataclass
class Txn:
    ref_no: str
    stt: str
    trans_date: str
    trans_ts: str  # ISO 8601, sortable; used to pick the earliest charge
    eff_date: str
    trans_code: str
    debit: float
    credit: float
    balance: float
    seq_no: str
    teller_id: str
    branch: str
    description: str
    corr_account: str
    corr_name: str
    corr_bank: str
    order_id: str
    ky: str
    cif: str
    product: str
    dedup_key: str

    def as_dict(self) -> dict:
        return asdict(self)


def make_dedup_key(order_id: str, ky: str, cif: str, product: str) -> str:
    """Stable, normalized dedup key: order_id | ky | cif | product."""
    return " | ".join(norm(x) for x in (order_id, f"Ky {ky}", cif, product))


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_ts(v) -> str:
    """Parse 'dd/mm/yyyy HH:MM:SS' (or a datetime) to sortable ISO 8601."""
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    s = str(v).strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return s  # leave as-is; still deterministic as a tie-breaker string


def _num(v) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return 0.0


# Header keywords (NFC-lowercased substrings) -> column key. BIDV ships two
# layouts: the full one (STT in col B, ref_no in col P, counterparty in M-O)
# and the compact one attached to the split "email n/5" statements (no
# counterparty columns, ref_no in col M). Columns are located by header text;
# anything not found falls back to the fixed COL positions.
_HEADER_KEYS = {
    "stt": ("stt", "(no)"),
    "trans_date": ("ngày giao dịch", "trans.date"),
    "eff_date": ("ngày hiệu lực", "efd.date"),
    "trans_code": ("mã giao dịch", "trans.code"),
    "debit": ("phát sinh nợ", "debit amount"),
    "credit": ("phát sinh có", "credit amount"),
    "balance": ("số dư", "(balance)"),
    "seq_no": ("số chứng từ", "seq no"),
    "teller_id": ("mã gdv", "teller id"),
    "branch": ("mã cn", "(branch)"),
    "description": ("diễn giải", "txn. description"),
    "corr_account": ("tài khoản đối ứng",),
    "corr_name": ("tên đối ứng",),
    "corr_bank": ("ngân hàng đối ứng",),
    "ref_no": ("số tham chiếu", "reference number"),
}
# Counterparty account embedded in the description: "REM Tfr Ac:4270804530 ..."
_DESC_ACCOUNT = re.compile(r"Tfr Ac:\s*(\d+)", re.IGNORECASE)


_MISSING = 10_000


def _detect_columns(raw_rows) -> dict:
    """Map column keys to indices from the header row(s), else fixed COL."""
    for row in raw_rows[:40]:
        cells = [norm(c) for c in row]
        if not any("số tham chiếu" in c or "reference number" in c for c in cells):
            continue
        found: dict = {}
        for idx, cell in enumerate(cells):
            for key, words in _HEADER_KEYS.items():
                if key not in found and any(cell == w or (len(w) > 4 and w in cell) for w in words):
                    found[key] = idx
                    break
        if "description" in found and "ref_no" in found:
            # Columns absent from this layout point past the row -> None.
            return {k: found.get(k, _MISSING) for k in COL}
    return dict(COL)


def parse_statement(path: str) -> tuple[list[Txn], dict]:
    """Return (insurance_transactions, summary).

    Only rows that are (a) numbered transactions, (b) credit > 0, and
    (c) match the insurance description pattern are returned as Txn records.
    `summary` reports counts for the upload screen.
    """
    # Read with python-calamine (Rust) — ~10x faster than openpyxl. It returns
    # ragged rows (trailing empties trimmed); get() below tolerates short rows.
    wb = CalamineWorkbook.from_path(path)
    names = wb.sheet_names
    sheet = config.STMT_SHEET if config.STMT_SHEET in names else names[0]
    raw_rows = wb.get_sheet_by_name(sheet).to_python(skip_empty_area=False)
    cols = _detect_columns(raw_rows)

    total_rows = 0
    unmatched: list[str] = []
    txns: list[Txn] = []
    seen_refs: set[str] = set()

    for row in raw_rows:
        get = lambda key: row[cols[key]] if cols[key] < len(row) else None  # noqa: E731
        stt = get("stt")
        s = str(stt).strip()
        if s.endswith(".0"):  # tolerate numeric STT like 2.0
            s = s[:-2]
        if not s.isdigit():
            continue
        total_rows += 1

        desc = str(get("description") or "")
        credit = _num(get("credit"))
        m = _DETAIL.search(desc)
        if m is None or credit <= 0:
            if credit > 0:
                unmatched.append(desc[:80])
            continue

        oid_m = _ORDER_ID.search(desc)
        order_id = oid_m.group(1) if oid_m else ""
        product = m.group("product").strip()
        ky = m.group("ky").strip()
        cif = m.group("cif").strip()
        if not cif.isdigit():
            acc = _DESC_ACCOUNT.search(desc)
            cif = acc.group(1) if acc else ""
        ref_no = str(get("ref_no") or "").strip()
        if not ref_no or ref_no in seen_refs:
            # Missing/duplicate reference within one file — skip to keep PK sane.
            continue
        seen_refs.add(ref_no)

        txns.append(
            Txn(
                ref_no=ref_no,
                stt=s,
                trans_date=str(get("trans_date") or "").strip(),
                trans_ts=_parse_ts(get("trans_date")),
                eff_date=str(get("eff_date") or "").strip(),
                trans_code=str(get("trans_code") or "").strip(),
                debit=_num(get("debit")),
                credit=credit,
                balance=_num(get("balance")),
                seq_no=str(get("seq_no") or "").strip(),
                teller_id=str(get("teller_id") or "").strip(),
                branch=str(get("branch") or "").strip(),
                description=desc,
                corr_account=str(get("corr_account") or "").strip()
                or (acc_m.group(1) if (acc_m := _DESC_ACCOUNT.search(desc)) else ""),
                corr_name=str(get("corr_name") or "").strip(),
                corr_bank=str(get("corr_bank") or "").strip(),
                order_id=order_id,
                ky=ky,
                cif=cif,
                product=product,
                dedup_key=make_dedup_key(order_id, ky, cif, product),
            )
        )

    summary = {
        "total_numbered_rows": total_rows,
        "insurance_rows": len(txns),
        "non_insurance_credit_rows": len(unmatched),
        "unmatched_samples": unmatched[:10],
    }
    return txns, summary
