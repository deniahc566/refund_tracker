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
TEMPLATE_PATH = PROJECT_ROOT / "templates" / "form_dien_case_hoan.xlsx"
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
# Unknown products fall back to the statement's own values and are flagged.
PRODUCT_CONFIG: dict[str, dict] = {
    "bao an tai khoan": {"beneficiary_bank": "BIDV", "refund_amount": 5000},
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

# --- Refund form layout ----------------------------------------------------
REFUND_HEADER_ROW = 2   # column headers live here in the template
REFUND_DATA_START = 3   # first data row
# Extra round-trip column appended to the template (empty col G).
REFUND_REF_COL = 7      # G — carries the transaction ref_no for result import
REFUND_REF_HEADER = "Mã tham chiếu\n(Ref)"
# Result-import extra columns (appended after Ref).
REFUND_STATUS_COL = 8   # H
REFUND_REASON_COL = 9   # I
REFUND_STATUS_HEADER = "Trạng thái\n(Status)"
REFUND_REASON_HEADER = "Lý do\n(Reason)"
