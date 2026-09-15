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
# product / ky / cif in one shot
_DETAIL = re.compile(
    r"Phi Bao hiem\s+(?P<product>.+?)\s+Ky\s+(?P<ky>\S+)\s+cua ma KH\s+(?P<cif>\d+)",
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


def parse_statement(path: str) -> tuple[list[Txn], dict]:
    """Return (insurance_transactions, summary).

    Only rows that are (a) numbered transactions, (b) credit > 0, and
    (c) match the insurance description pattern are returned as Txn records.
    `summary` reports counts for the upload screen.
    """
    # Read with python-calamine (Rust) — ~10x faster than openpyxl. It returns
    # ragged rows (trailing empties trimmed), so pad each row to a fixed width.
    wb = CalamineWorkbook.from_path(path)
    names = wb.sheet_names
    sheet = config.STMT_SHEET if config.STMT_SHEET in names else names[0]
    raw_rows = wb.get_sheet_by_name(sheet).to_python(skip_empty_area=False)
    width = max(COL.values()) + 1

    total_rows = 0
    unmatched: list[str] = []
    txns: list[Txn] = []
    seen_refs: set[str] = set()

    for row in raw_rows:
        if len(row) < width:
            row = list(row) + [None] * (width - len(row))
        stt = row[COL["stt"]]
        s = str(stt).strip()
        if s.endswith(".0"):  # tolerate numeric STT like 2.0
            s = s[:-2]
        if not s.isdigit():
            continue
        total_rows += 1

        desc = str(row[COL["description"]] or "")
        credit = _num(row[COL["credit"]])
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
        ref_no = str(row[COL["ref_no"]] or "").strip()
        if not ref_no or ref_no in seen_refs:
            # Missing/duplicate reference within one file — skip to keep PK sane.
            continue
        seen_refs.add(ref_no)

        txns.append(
            Txn(
                ref_no=ref_no,
                stt=s,
                trans_date=str(row[COL["trans_date"]] or "").strip(),
                trans_ts=_parse_ts(row[COL["trans_date"]]),
                eff_date=str(row[COL["eff_date"]] or "").strip(),
                trans_code=str(row[COL["trans_code"]] or "").strip(),
                debit=_num(row[COL["debit"]]),
                credit=credit,
                balance=_num(row[COL["balance"]]),
                seq_no=str(row[COL["seq_no"]] or "").strip(),
                teller_id=str(row[COL["teller_id"]] or "").strip(),
                branch=str(row[COL["branch"]] or "").strip(),
                description=desc,
                corr_account=str(row[COL["corr_account"]] or "").strip(),
                corr_name=str(row[COL["corr_name"]] or "").strip(),
                corr_bank=str(row[COL["corr_bank"]] or "").strip(),
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
