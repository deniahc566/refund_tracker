"""Build refund rows from pending duplicates and write them into the BIDV
bulk-payment template (sheet "Mẫu file_File Template"), filling columns A–G."""
from __future__ import annotations

import copy
from datetime import datetime

import openpyxl
from openpyxl.worksheet.datavalidation import DataValidation

from . import config
from .config import product_rule


def build_refund_rows(pending: list) -> list[dict]:
    """Map pending-duplicate DB rows to refund-file field dicts.

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
        currency = rule.get("currency", config.DEFAULT_CURRENCY)
        # Ref ID appended to the remark so it round-trips through the bank and
        # the result import can reconcile 1:1 by ref_no.
        payment_detail = (
            f"Hoan Phi Bao Hiem {product} Ky {ky} cua KH {cif}"
            f"{config.REFUND_REF_SEP}{ref_no}"
        )
        out.append({
            "ref_no": ref_no,
            "dedup_key": dedup_key,
            "cif": cif,
            "product": product,
            "beneficiary_account": corr_account,   # C
            "beneficiary_name": corr_name,         # B
            "beneficiary_bank": bank,              # D
            "amount": float(amount),               # E
            "currency": currency,                  # F
            "payment_detail": payment_detail,      # G (Nội dung / Remark)
            "needs_review": needs_review,
        })
    return out


def write_refund_file(refund_rows: list[dict], out_path: str) -> str:
    """Fill the bulk-payment template's data sheet (columns A–G, from row 4)
    and save to out_path. All other sheets, headers, droplists and named
    ranges are left untouched so the bank's validation still works."""
    wb = openpyxl.load_workbook(config.TEMPLATE_PATH)
    ws = wb[config.REFUND_SHEET] if config.REFUND_SHEET in wb.sheetnames else wb.active
    # The BIDV template ships with this sheet protected — unlock it so ops can
    # freely fill the Status / Reason / bank-code columns.
    ws.protection.sheet = False
    C = config.REFUND_COL
    hrow = config.REFUND_HEADER_ROW

    # Add "Trạng thái (Done/Fail)" and "Lý do" headers (styled like existing
    # headers) so ops can fill results in the same file that is re-imported.
    tmpl_hdr = ws.cell(row=hrow, column=C["remark"])  # copy style from "Nội dung"
    for col, text in ((config.REFUND_STATUS_COL, config.REFUND_STATUS_HEADER),
                      (config.REFUND_REASON_COL, config.REFUND_REASON_HEADER),
                      (config.REFUND_BANKCODE_COL, config.REFUND_BANKCODE_HEADER)):
        c = ws.cell(row=hrow, column=col, value=text)
        c.font = copy.copy(tmpl_hdr.font)
        c.fill = copy.copy(tmpl_hdr.fill)
        c.border = copy.copy(tmpl_hdr.border)
        c.alignment = copy.copy(tmpl_hdr.alignment)
    ws.column_dimensions[
        openpyxl.utils.get_column_letter(config.REFUND_STATUS_COL)].width = 20
    ws.column_dimensions[
        openpyxl.utils.get_column_letter(config.REFUND_REASON_COL)].width = 30
    ws.column_dimensions[
        openpyxl.utils.get_column_letter(config.REFUND_BANKCODE_COL)].width = 24

    r = config.REFUND_DATA_START
    for i, row in enumerate(refund_rows, start=1):
        ws.cell(row=r, column=C["stt"], value=i)                          # A
        ws.cell(row=r, column=C["name"], value=row["beneficiary_name"])   # B
        ws.cell(row=r, column=C["account"], value=row["beneficiary_account"])  # C
        ws.cell(row=r, column=C["bank"], value=row["beneficiary_bank"])   # D
        ws.cell(row=r, column=C["amount"], value=row["amount"])           # E
        ws.cell(row=r, column=C["currency"], value=row["currency"])       # F
        ws.cell(row=r, column=C["remark"], value=row["payment_detail"])   # G (ref embedded)
        r += 1

    # Done/Fail dropdown on the Status column for the filled rows.
    if refund_rows:
        col = openpyxl.utils.get_column_letter(config.REFUND_STATUS_COL)
        dv = DataValidation(type="list", formula1='"Done,Fail"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{col}{config.REFUND_DATA_START}:{col}{r - 1}")

    wb.save(out_path)
    wb.close()
    return out_path


def default_batch_id() -> str:
    return "BATCH_" + datetime.now().strftime("%Y%m%d_%H%M%S")
