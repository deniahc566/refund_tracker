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
import functools
import threading
from typing import Iterable

import duckdb
import pandas as pd


def _synchronized(method):
    """Serialize DB access: one DuckDB/MotherDuck connection is shared across
    Streamlit's rerun threads, and DuckDB connections don't allow concurrent
    use. A reentrant lock makes overlapping tab reruns safe."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper

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
        self._lock = threading.RLock()
        self.con = duckdb.connect(self.conn_str)
        self.con.execute(_SCHEMA)

    def close(self):
        self.con.close()

    # -- ingestion ----------------------------------------------------------
    @_synchronized
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

        # Only recompute when something was actually inserted, and only for the
        # dedup_keys touched by this batch — not the whole (large) ledger.
        if new_rows:
            self._recompute_duplicates(ingested_at=now)

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

    @_synchronized
    def _recompute_duplicates(self, ingested_at=None) -> None:
        """First-seen-wins ranking. rn=1 is legitimate, the rest are duplicates.

        With `ingested_at`, only the dedup_keys touched by that batch are
        re-ranked (correct because first-seen-wins is per-key), which keeps the
        cost proportional to the new file rather than the whole ledger. Without
        it, the entire table is recomputed.
        """
        if ingested_at is None:
            scope = "SELECT ref_no, dedup_key, trans_ts, seq_no FROM transactions"
            params: list = []
        else:
            scope = (
                "SELECT ref_no, dedup_key, trans_ts, seq_no FROM transactions "
                "WHERE dedup_key IN ("
                "  SELECT DISTINCT dedup_key FROM transactions WHERE ingested_at = ?"
                ")"
            )
            params = [ingested_at]
        self.con.execute(
            f"""
            WITH scope AS ({scope}),
            ranked AS (
                SELECT ref_no,
                       ROW_NUMBER() OVER (
                           PARTITION BY dedup_key
                           ORDER BY trans_ts, TRY_CAST(seq_no AS BIGINT), ref_no
                       ) AS rn,
                       FIRST_VALUE(ref_no) OVER (
                           PARTITION BY dedup_key
                           ORDER BY trans_ts, TRY_CAST(seq_no AS BIGINT), ref_no
                       ) AS first_ref
                FROM scope
            )
            UPDATE transactions t
            SET is_duplicate = (r.rn > 1),
                dup_of_ref   = CASE WHEN r.rn > 1 THEN r.first_ref ELSE NULL END
            FROM ranked r
            WHERE t.ref_no = r.ref_no
            """,
            params,
        )

    # -- refund queue -------------------------------------------------------
    @_synchronized
    def pending_duplicates(self):
        """Every duplicate transaction still awaiting a refund — i.e. anything
        NOT yet marked 'done'. Covers not-yet-generated duplicates, generated
        'pending' ones, and 'failed' ones. Only a 'done' refund leaves the
        queue, so the tab always lists what still owes a refund and the file
        can be regenerated/downloaded at any time.
        """
        return self.con.execute(
            """
            SELECT t.ref_no, t.dedup_key, t.cif, t.product, t.trans_date,
                   t.corr_account, t.corr_name, t.corr_bank, t.credit, t.ky,
                   t.dup_of_ref
            FROM transactions t
            LEFT JOIN refunds r ON r.ref_no = t.ref_no
            WHERE t.is_duplicate
              AND (r.ref_no IS NULL OR r.status <> 'done')
            ORDER BY t.trans_ts, t.ref_no
            """
        ).fetchall()

    @_synchronized
    def ensure_pending_refunds(self, refund_rows: list[dict]) -> int:
        """Create a refund row (status 'pending') for any pending duplicate that
        doesn't have one yet, so the result-import step can match it by ref_no.
        Idempotent: existing rows (pending/failed/done) are left untouched.
        Returns how many new rows were created. Single bulk INSERT (anti-join)
        instead of one round-trip per row."""
        if not refund_rows:
            return 0
        now = _dt.datetime.now()
        cols = ["ref_no", "dedup_key", "cif", "product", "beneficiary_account",
                "beneficiary_name", "beneficiary_bank", "amount", "payment_detail",
                "needs_review"]
        df = pd.DataFrame([{c: r[c] for c in cols} for r in refund_rows]
                          ).drop_duplicates(subset="ref_no", keep="first")
        df["status"] = "pending"
        df["reason"] = None
        df["batch_id"] = "AUTO"
        df["generated_at"] = now
        df["resolved_at"] = None
        allcols = cols + ["status", "reason", "batch_id", "generated_at", "resolved_at"]
        before = self.con.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
        self.con.register("ref_stage", df)
        self.con.execute(
            f"INSERT INTO refunds ({', '.join(allcols)}) "
            f"SELECT {', '.join(allcols)} FROM ref_stage s "
            f"WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.ref_no = s.ref_no)"
        )
        self.con.unregister("ref_stage")
        after = self.con.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
        return after - before

    @_synchronized
    def apply_results(self, results: list[dict]) -> dict:
        """Update refund status/reason from an imported result file. Matched by
        ref_no (column L) for an exact 1:1 link, falling back to beneficiary
        account + remark when the ref is missing. Returns counts by outcome;
        `ambiguous` counts fallback rows that match more than one refund."""
        now = _dt.datetime.now()
        updated = {"done": 0, "failed": 0, "unknown_ref": 0, "ambiguous": 0, "other": 0}
        done_words = ("done", "success", "thanh cong", "hoan thanh", "ok", "thành công")
        fail_words = ("failed", "fail", "that bai", "loi", "error", "thất bại")

        ref_rows: list[dict] = []       # matched exactly by ref_no (bulk path)
        fallback: list[dict] = []       # no ref -> account+remark (rare, loop)
        for res in results:
            ref_no = (res.get("ref_no") or "").strip()
            account = (res.get("beneficiary_account") or "").strip()
            remark = (res.get("payment_detail") or "").strip()
            status = (res.get("status") or "").strip().lower()
            reason = res.get("reason") or None
            if not ref_no and not account and not remark:
                updated["unknown_ref"] += 1
                continue
            # No status column present in the executed file => Done. An optional
            # Status column can still flag specific rows Failed.
            norm = ("done" if not status or status in done_words
                    else "failed" if status in fail_words else None)
            if norm is None:
                updated["other"] += 1
                continue
            if ref_no:
                ref_rows.append({"ref_no": ref_no, "status": norm, "reason": reason})
            else:
                fallback.append({"account": account, "remark": remark,
                                 "status": norm, "reason": reason})

        # -- Bulk path: one UPDATE via join, regardless of row count ---------
        if ref_rows:
            df = pd.DataFrame(ref_rows).drop_duplicates(subset="ref_no", keep="last")
            self.con.register("res_stage", df)
            counts = dict(self.con.execute(
                "SELECT s.status, COUNT(*) FROM res_stage s "
                "JOIN refunds r ON r.ref_no = s.ref_no GROUP BY s.status"
            ).fetchall())
            updated["done"] += int(counts.get("done", 0))
            updated["failed"] += int(counts.get("failed", 0))
            updated["unknown_ref"] += len(df) - sum(int(v) for v in counts.values())
            self.con.execute(
                "UPDATE refunds r SET status = s.status, reason = s.reason, "
                "resolved_at = CASE WHEN s.status = 'done' THEN ? ELSE NULL END "
                "FROM res_stage s WHERE r.ref_no = s.ref_no",
                [now],
            )
            self.con.unregister("res_stage")

        # -- Fallback path: account + remark (usually empty) -----------------
        for fb in fallback:
            matches = self.con.execute(
                "SELECT ref_no FROM refunds "
                "WHERE beneficiary_account = ? AND payment_detail = ? "
                "ORDER BY (status = 'done') ASC, ref_no",
                [fb["account"], fb["remark"]],
            ).fetchall()
            if not matches:
                updated["unknown_ref"] += 1
                continue
            if len(matches) > 1:
                updated["ambiguous"] += 1
            self.con.execute(
                "UPDATE refunds SET status=?, reason=?, resolved_at=? WHERE ref_no=?",
                [fb["status"], fb["reason"],
                 now if fb["status"] == "done" else None, matches[0][0]],
            )
            updated[fb["status"]] += 1
        return updated

    # -- history / lookup ---------------------------------------------------
    @_synchronized
    def refund_history(self, order_id=None, account=None, charge_from=None,
                       charge_to=None, refund_from=None, refund_to=None,
                       statuses=None):
        """Searchable refund history. Each row is a refunded/duplicate charge
        joined to its original ("giao dịch gốc" via dup_of_ref). Filters:
        order_id / account (contains), charge-date range (on the duplicate's
        trans date), refund-date range (resolved_at), and status list.
        Returns a pandas DataFrame with Vietnamese column headers."""
        where, params = ["1=1"], []
        if order_id:
            where.append("t.order_id ILIKE ?"); params.append(f"%{order_id}%")
        if account:
            where.append("t.corr_account ILIKE ?"); params.append(f"%{account}%")
        if charge_from:
            where.append("substr(t.trans_ts, 1, 10) >= ?"); params.append(str(charge_from))
        if charge_to:
            where.append("substr(t.trans_ts, 1, 10) <= ?"); params.append(str(charge_to))
        if refund_from:
            where.append("CAST(r.resolved_at AS DATE) >= ?"); params.append(str(refund_from))
        if refund_to:
            where.append("CAST(r.resolved_at AS DATE) <= ?"); params.append(str(refund_to))
        if statuses:
            where.append("r.status IN (" + ",".join(["?"] * len(statuses)) + ")")
            params.extend(statuses)
        sql = f"""
            SELECT
                t.order_id                       AS "OrderID",
                t.cif                            AS "CIF",
                t.product                        AS "Sản phẩm",
                t.ky                             AS "Kỳ",
                t.corr_account                   AS "STK hưởng",
                t.corr_name                      AS "Tên hưởng",
                r.amount                         AS "Số tiền",
                t.trans_date                     AS "Ngày thu phí",
                strftime(r.resolved_at, '%d/%m/%Y %H:%M:%S') AS "Ngày hoàn",
                r.status                         AS "Trạng thái",
                r.reason                         AS "Lý do",
                t.ref_no                         AS "Mã GD (trùng)",
                t.dup_of_ref                     AS "Mã GD gốc",
                o.trans_date                     AS "Ngày thu phí (GD gốc)"
            FROM refunds r
            JOIN transactions t ON t.ref_no = r.ref_no
            LEFT JOIN transactions o ON o.ref_no = t.dup_of_ref
            WHERE {' AND '.join(where)}
            ORDER BY r.resolved_at DESC NULLS LAST, t.trans_ts DESC
        """
        return self.con.execute(sql, params).df()

    # -- stats --------------------------------------------------------------
    @_synchronized
    def stats(self) -> dict:
        # All scalar counts in one round-trip (stats runs on every rerun; on
        # MotherDuck each separate query is a network hop).
        total, dups, pending, refunded_amt = self.con.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM transactions),
                (SELECT COUNT(*) FROM transactions WHERE is_duplicate),
                (SELECT COUNT(*) FROM transactions t
                 LEFT JOIN refunds r ON r.ref_no = t.ref_no
                 WHERE t.is_duplicate AND (r.ref_no IS NULL OR r.status <> 'done')),
                (SELECT COALESCE(SUM(amount), 0) FROM refunds WHERE status = 'done')
            """
        ).fetchone()
        by_status = dict(
            self.con.execute(
                "SELECT status, COUNT(*) FROM refunds GROUP BY status"
            ).fetchall()
        )
        return {
            "total_transactions": total,
            "duplicates": dups,
            "pending_refunds": pending,
            "refunds_by_status": by_status,
            "refunded_amount": refunded_amt,
        }
