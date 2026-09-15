"""DuckDB / MotherDuck persistence layer.

Two-level identity:
  * transactions.ref_no  — the bank's reference number (col P), unique per
    physical line. Ingestion is idempotent on it, so re-uploading an
    overlapping statement never double-counts.
  * dedup_key            — order_id | ky | cif | product. Detects a *genuine*
    duplicate charge for the same certificate+period. First-seen-wins: the
    earliest charge (by trans_ts, then seq_no, then ref_no) is legitimate;
    every later charge with the same key is a refundable duplicate.
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable

import duckdb
import pandas as pd

from . import config
from .parser import Txn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    ref_no        VARCHAR PRIMARY KEY,
    stt           VARCHAR,
    trans_date    VARCHAR,
    trans_ts      VARCHAR,
    eff_date      VARCHAR,
    trans_code    VARCHAR,
    debit         DOUBLE,
    credit        DOUBLE,
    balance       DOUBLE,
    seq_no        VARCHAR,
    teller_id     VARCHAR,
    branch        VARCHAR,
    description   VARCHAR,
    corr_account  VARCHAR,
    corr_name     VARCHAR,
    corr_bank     VARCHAR,
    order_id      VARCHAR,
    ky            VARCHAR,
    cif           VARCHAR,
    product       VARCHAR,
    dedup_key     VARCHAR,
    is_duplicate  BOOLEAN DEFAULT FALSE,
    dup_of_ref    VARCHAR,
    source_file   VARCHAR,
    ingested_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS refunds (
    ref_no          VARCHAR PRIMARY KEY,   -- the duplicate txn being refunded
    dedup_key       VARCHAR,
    cif             VARCHAR,
    product         VARCHAR,
    beneficiary_account VARCHAR,           -- (2) from corr_account
    beneficiary_name    VARCHAR,           -- (3) from corr_name
    beneficiary_bank    VARCHAR,           -- (4) per product rule
    amount          DOUBLE,                -- (5) per product rule
    payment_detail  VARCHAR,               -- (6)
    needs_review    BOOLEAN DEFAULT FALSE, -- product had no config rule
    status          VARCHAR DEFAULT 'pending',  -- pending | done | failed
    reason          VARCHAR,
    batch_id        VARCHAR,
    generated_at    TIMESTAMP,
    resolved_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS uploads (
    file_hash     VARCHAR,
    file_name     VARCHAR,
    total_rows    INTEGER,
    insurance_rows INTEGER,
    new_rows      INTEGER,
    new_duplicates INTEGER,
    uploaded_at   TIMESTAMP
);
"""


class Store:
    def __init__(self, conn_str: str | None = None):
        self.conn_str, self.label = config.get_db_target()
        if conn_str:
            self.conn_str = conn_str
        self.con = duckdb.connect(self.conn_str)
        self.con.execute(_SCHEMA)

    def close(self):
        self.con.close()

    # -- ingestion ----------------------------------------------------------
    def ingest(self, txns: Iterable[Txn], source_file: str, summary: dict,
               file_hash: str) -> dict:
        """Insert new transactions (idempotent on ref_no), then recompute
        duplicate flags. Returns an ingest report."""
        now = _dt.datetime.now()
        rows = list(txns)
        cols = [
            "ref_no", "stt", "trans_date", "trans_ts", "eff_date", "trans_code",
            "debit", "credit", "balance", "seq_no", "teller_id", "branch",
            "description", "corr_account", "corr_name", "corr_bank", "order_id",
            "ky", "cif", "product", "dedup_key",
        ]
        before = self.con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        if rows:
            df = pd.DataFrame([t.as_dict() for t in rows])[cols].drop_duplicates(
                subset="ref_no", keep="first"
            )
            df["source_file"] = source_file
            df["ingested_at"] = now
            collist = ", ".join(cols + ["source_file", "ingested_at"])
            # Bulk insert, skipping ref_no values already in the ledger (idempotent).
            self.con.register("staging_df", df)
            self.con.execute(
                f"INSERT INTO transactions ({collist}) "
                f"SELECT {collist} FROM staging_df s "
                f"WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.ref_no = s.ref_no)"
            )
            self.con.unregister("staging_df")
        after = self.con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        new_rows = after - before

        self._recompute_duplicates()

        new_dups = self.con.execute(
            "SELECT COUNT(*) FROM transactions "
            "WHERE is_duplicate AND source_file = ? AND ingested_at = ?",
            [source_file, now],
        ).fetchone()[0]

        self.con.execute(
            "INSERT INTO uploads VALUES (?,?,?,?,?,?,?)",
            [file_hash, source_file, summary.get("total_numbered_rows", 0),
             summary.get("insurance_rows", 0), new_rows, new_dups, now],
        )
        return {
            "new_rows": new_rows,
            "skipped_existing": len(rows) - new_rows,
            "new_duplicates": new_dups,
            "label": self.label,
        }

    def _recompute_duplicates(self) -> None:
        """First-seen-wins across the whole ledger. rn=1 is legitimate."""
        self.con.execute(
            """
            WITH ranked AS (
                SELECT ref_no, dedup_key,
                       ROW_NUMBER() OVER (
                           PARTITION BY dedup_key
                           ORDER BY trans_ts, TRY_CAST(seq_no AS BIGINT), ref_no
                       ) AS rn,
                       FIRST_VALUE(ref_no) OVER (
                           PARTITION BY dedup_key
                           ORDER BY trans_ts, TRY_CAST(seq_no AS BIGINT), ref_no
                       ) AS first_ref
                FROM transactions
            )
            UPDATE transactions t
            SET is_duplicate = (r.rn > 1),
                dup_of_ref   = CASE WHEN r.rn > 1 THEN r.first_ref ELSE NULL END
            FROM ranked r
            WHERE t.ref_no = r.ref_no
            """
        )

    # -- refund queue -------------------------------------------------------
    def pending_duplicates(self):
        """Duplicate transactions not yet resolved (no refund row, or failed).

        A 'done' or 'pending' refund keeps its txn out of the queue so the
        1:1 mapping holds and refunds never regenerate.
        """
        return self.con.execute(
            """
            SELECT t.ref_no, t.dedup_key, t.cif, t.product, t.trans_date,
                   t.corr_account, t.corr_name, t.corr_bank, t.credit, t.ky,
                   t.dup_of_ref
            FROM transactions t
            LEFT JOIN refunds r ON r.ref_no = t.ref_no
            WHERE t.is_duplicate
              AND (r.ref_no IS NULL OR r.status = 'failed')
            ORDER BY t.trans_ts, t.ref_no
            """
        ).fetchall()

    def record_refund_batch(self, refund_rows: list[dict], batch_id: str) -> None:
        """Upsert refund rows as 'pending' for a generated batch. Re-batching a
        previously failed refund resets it to pending under the new batch."""
        now = _dt.datetime.now()
        for r in refund_rows:
            self.con.execute(
                """
                INSERT INTO refunds
                    (ref_no, dedup_key, cif, product, beneficiary_account,
                     beneficiary_name, beneficiary_bank, amount, payment_detail,
                     needs_review, status, reason, batch_id, generated_at, resolved_at)
                VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', NULL, ?, ?, NULL)
                ON CONFLICT (ref_no) DO UPDATE SET
                    status='pending', reason=NULL, batch_id=excluded.batch_id,
                    generated_at=excluded.generated_at, resolved_at=NULL,
                    beneficiary_account=excluded.beneficiary_account,
                    beneficiary_name=excluded.beneficiary_name,
                    beneficiary_bank=excluded.beneficiary_bank,
                    amount=excluded.amount, payment_detail=excluded.payment_detail,
                    needs_review=excluded.needs_review
                """,
                [r["ref_no"], r["dedup_key"], r["cif"], r["product"],
                 r["beneficiary_account"], r["beneficiary_name"],
                 r["beneficiary_bank"], r["amount"], r["payment_detail"],
                 r["needs_review"], batch_id, now],
            )

    def apply_results(self, results: list[dict]) -> dict:
        """Update refund status/reason from an imported result file, matched by
        ref_no. Returns counts by outcome."""
        now = _dt.datetime.now()
        updated = {"done": 0, "failed": 0, "unknown_ref": 0, "other": 0}
        for res in results:
            ref = res.get("ref_no", "").strip()
            status = (res.get("status") or "").strip().lower()
            reason = res.get("reason") or None
            if not ref:
                updated["unknown_ref"] += 1
                continue
            exists = self.con.execute(
                "SELECT 1 FROM refunds WHERE ref_no = ?", [ref]
            ).fetchone()
            if not exists:
                updated["unknown_ref"] += 1
                continue
            norm_status = (
                "done" if status in ("done", "success", "thanh cong", "hoan thanh", "ok")
                else "failed" if status in ("failed", "fail", "that bai", "loi", "error")
                else None
            )
            if norm_status is None:
                updated["other"] += 1
                continue
            self.con.execute(
                "UPDATE refunds SET status=?, reason=?, resolved_at=? WHERE ref_no=?",
                [norm_status, reason, now if norm_status == "done" else None, ref],
            )
            updated[norm_status] += 1
        return updated

    # -- stats --------------------------------------------------------------
    def stats(self) -> dict:
        q = self.con.execute
        total = q("SELECT COUNT(*) FROM transactions").fetchone()[0]
        dups = q("SELECT COUNT(*) FROM transactions WHERE is_duplicate").fetchone()[0]
        by_status = dict(
            q("SELECT status, COUNT(*) FROM refunds GROUP BY status").fetchall()
        )
        pending_q = q(
            """
            SELECT COUNT(*) FROM transactions t
            LEFT JOIN refunds r ON r.ref_no = t.ref_no
            WHERE t.is_duplicate AND (r.ref_no IS NULL OR r.status='failed')
            """
        ).fetchone()[0]
        refunded_amt = q(
            "SELECT COALESCE(SUM(amount),0) FROM refunds WHERE status='done'"
        ).fetchone()[0]
        return {
            "total_transactions": total,
            "duplicates": dups,
            "pending_refunds": pending_q,
            "refunds_by_status": by_status,
            "refunded_amount": refunded_amt,
        }
