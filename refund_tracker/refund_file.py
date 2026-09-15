"""Build refund rows from pending duplicates and write them into the
'form điền case hoàn' template, preserving its formatting."""
from __future__ import annotations

import copy
from datetime import datetime

import openpyxl

from . import config
from .config import product_rule


def build_refund_rows(pending: list) -> list[dict]:
    """Map pending-duplicate DB rows to refund-form field dicts.

    `pending` rows come from Store.pending_duplicates() with columns:
    (ref_no, dedup_key, cif, product, trans_date, corr_account, corr_name,
     corr_bank, credit, ky, dup_of_ref)
    """
    out: list[dict] = []
    for (ref_no, dedup_key, cif, product, _tdate, corr_account, corr_name,
         corr_bank, credit, ky, _dup_of) in pending:
        rule = product_rule(product)
        needs_review = not rule
        bank = rule.get("beneficiary_bank") or corr_bank
        amount = rule.get("refund_amount")
        if amount is None:
            amount = credit  # fall back to the charged amount
        payment_detail = f"Hoan Phi Bao Hiem {product} Ky {ky} cua KH {cif}"
        out.append({
            "ref_no": ref_no,
            "dedup_key": dedup_key,
            "cif": cif,
            "product": product,
            "beneficiary_account": corr_account,
            "beneficiary_name": corr_name,
            "beneficiary_bank": bank,
            "amount": float(amount),
            "payment_detail": payment_detail,
            "needs_review": needs_review,
        })
    return out


def _style_from(cell):
    """Copy a cell's style so appended header cells match the template."""
    return {
        "font": copy.copy(cell.font),
        "fill": copy.copy(cell.fill),
        "border": copy.copy(cell.border),
        "alignment": copy.copy(cell.alignment),
    }


def write_refund_file(refund_rows: list[dict], out_path: str) -> str:
    """Fill the template with refund rows and save to out_path. Adds a hidden-
    intent 'Ref' column (G) carrying ref_no for reliable result round-trip."""
    wb = openpyxl.load_workbook(config.TEMPLATE_PATH)
    ws = wb.active

    # Header for the appended Ref column, styled like the existing headers.
    hdr = ws.cell(row=config.REFUND_HEADER_ROW, column=1)
    style = _style_from(hdr)
    for col, text in (
        (config.REFUND_REF_COL, config.REFUND_REF_HEADER),
        (config.REFUND_STATUS_COL, config.REFUND_STATUS_HEADER),
        (config.REFUND_REASON_COL, config.REFUND_REASON_HEADER),
    ):
        c = ws.cell(row=config.REFUND_HEADER_ROW, column=col, value=text)
        for k, v in style.items():
            setattr(c, k, v)
    ws.column_dimensions[
        openpyxl.utils.get_column_letter(config.REFUND_REASON_COL)
    ].width = 30

    r = config.REFUND_DATA_START
    for i, row in enumerate(refund_rows, start=1):
        ws.cell(row=r, column=1, value=i)                          # (1) STT
        ws.cell(row=r, column=2, value=row["beneficiary_account"]) # (2)
        ws.cell(row=r, column=3, value=row["beneficiary_name"])    # (3)
        ws.cell(row=r, column=4, value=row["beneficiary_bank"])    # (4)
        ws.cell(row=r, column=5, value=row["amount"])              # (5)
        ws.cell(row=r, column=6, value=row["payment_detail"])      # (6)
        ws.cell(row=r, column=config.REFUND_REF_COL, value=row["ref_no"])
        r += 1

    wb.save(out_path)
    wb.close()
    return out_path


def default_batch_id() -> str:
    return "BATCH_" + datetime.now().strftime("%Y%m%d_%H%M%S")
