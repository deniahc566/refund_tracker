"""Configuration: DB connection, product rules, and column layout.

Environment variables (optional, loaded from a .env file if present):
    MOTHERDUCK_TOKEN   MotherDuck auth token. If set, the app connects to
                       MotherDuck cloud (md:<MD_DATABASE>). If absent, it falls
                       back to a local DuckDB file so the app still runs in dev.
    MD_DATABASE        MotherDuck database name (default: "refund_tracker").
    REFUND_DB_PATH     Local DuckDB file path used when no token is set
                       (default: <project>/refund_tracker.duckdb).
"""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path

# --- Paths -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Refund output uses the BIDV bulk-payment template (sheet "Mẫu file_File Template").
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "bulk_payment_template.xlsx"
DEFAULT_LOCAL_DB = PROJECT_ROOT / "refund_tracker.duckdb"


def _load_dotenv() -> None:
    """Minimal .env loader (no external dependency). KEY=VALUE per line."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()


def get_db_target() -> tuple[str, str]:
    """Return (connection_string, human_label) for DuckDB/MotherDuck.

    MotherDuck is used when MOTHERDUCK_TOKEN is set; otherwise a local file.
    """
    token = os.environ.get("MOTHERDUCK_TOKEN", "").strip()
    if token:
        db = os.environ.get("MD_DATABASE", "refund_tracker").strip()
        # duckdb reads the token from the MOTHERDUCK_TOKEN env var automatically.
        return f"md:{db}", f"MotherDuck (md:{db})"
    local = os.environ.get("REFUND_DB_PATH", str(DEFAULT_LOCAL_DB))
    return local, f"Local DuckDB ({local})"


# --- Product rules ---------------------------------------------------------
# Refund bank + amount per product. Keys are NFC-lowercased product names.
# `beneficiary_bank` must match a value in the template's bank droplist
# ("DS ngân hàng hưởng_Bank list" column C). Unknown products fall back to the
# statement's own values and are flagged for review.
BIDV_BANK = "01202001 - Ngan hang TMCP Dau tu va Phat trien Viet Nam - BIDV"
DEFAULT_CURRENCY = "VND"
PRODUCT_CONFIG: dict[str, dict] = {
    "bao an tai khoan": {
        "beneficiary_bank": BIDV_BANK,
        "refund_amount": 5000,
        "currency": DEFAULT_CURRENCY,
    },
}


def norm(text) -> str:
    """NFC-normalize + lowercase + collapse spaces, for stable matching."""
    s = "" if text is None else str(text)
    s = unicodedata.normalize("NFC", s).lower().strip()
    return " ".join(s.split())


def product_rule(product: str) -> dict:
    """Look up refund bank/amount for a product name (NFC-lower keyed)."""
    return PRODUCT_CONFIG.get(norm(product), {})


# --- Statement column layout (0-indexed within a row tuple) ----------------
# Sheet "bc3a": header at rows 15-16, data rows carry a numeric STT in col B.
STMT_SHEET = "bc3a"
COL = {
    "stt": 1,        # B  (No)
    "trans_date": 2, # C  Ngày giao dịch
    "eff_date": 3,   # D  Ngày hiệu lực
    "trans_code": 4, # E  Mã giao dịch
    "debit": 5,      # F  Phát sinh nợ
    "credit": 6,     # G  Phát sinh có
    "balance": 7,    # H  Số dư
    "seq_no": 8,     # I  Số chứng từ
    "teller_id": 9,  # J  Mã GDV
    "branch": 10,    # K  Mã CN
    "description": 11,   # L  Diễn giải  <-- dedup source
    "corr_account": 12,  # M  Tài khoản đối ứng  -> refund account (2)
    "corr_name": 13,     # N  Tên đối ứng         -> beneficiary name (3)
    "corr_bank": 14,     # O  Ngân hàng đối ứng
    "ref_no": 15,        # P  Số tham chiếu       -> unique txn PK
}

# --- Refund file layout (BIDV bulk-payment template) -----------------------
# Sheet "Mẫu file_File Template": notes in rows 1-2, headers in row 3, data
# from row 4. We fill columns A–G (the mandatory + content fields); H–O
# (personal-ID / branch / extra info) are left blank.
REFUND_SHEET = "Mẫu file_File Template"
REFUND_HEADER_ROW = 3
REFUND_DATA_START = 4
REFUND_COL = {
    "stt": 1,       # A  STT
    "name": 2,      # B  Tên người hưởng*      <- corr_name (N)
    "account": 3,   # C  Tài khoản hưởng        <- corr_account (M)
    "bank": 4,      # D  Ngân hàng hưởng*       <- product rule (BIDV droplist)
    "amount": 5,    # E  Số tiền*               <- product rule (5000)
    "currency": 6,  # F  Loại tiền*             <- VND
    "remark": 7,    # G  Nội dung*              <- payment_detail
}
# The transaction reference is embedded at the END of the remark (column G),
# as "... cua KH <cif> - <ref_no>", so it round-trips through the bank and
# results reconcile 1:1 by ref_no. This separator marks where it starts.
REFUND_REF_SEP = " - "
# Result-import: statuses are read from columns appended AFTER the template's
# own columns (O = 15), so they never collide with real template fields.
REFUND_STATUS_COL = 16    # P
REFUND_REASON_COL = 17    # Q
REFUND_BANKCODE_COL = 18  # R
REFUND_STATUS_HEADER = "Trạng thái (Done/Fail)"
REFUND_REASON_HEADER = "Lý do (không bắt buộc)"
REFUND_BANKCODE_HEADER = "FT GD hoàn"
