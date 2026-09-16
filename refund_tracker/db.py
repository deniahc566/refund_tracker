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
    bank_txn_code   VARCHAR,               -- bank's transaction id (from result)
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
        # Migration for pre-existing DBs (schema above only runs for new tables).
        self.con.execute(
            "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS bank_txn_code VARCHAR"
        )

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
            bank_code = (res.get("bank_txn_code") or "").strip()
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
                ref_rows.append({"ref_no": ref_no, "status": norm,
                                 "reason": reason, "bank_txn_code": bank_code})
            else:
                fallback.append({"account": account, "remark": remark,
                                 "status": norm, "reason": reason,
                                 "bank_txn_code": bank_code})

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
            # Keep any existing bank code when the incoming one is blank.
            self.con.execute(
                "UPDATE refunds r SET status = s.status, reason = s.reason, "
                "bank_txn_code = COALESCE(NULLIF(s.bank_txn_code, ''), r.bank_txn_code), "
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
                "UPDATE refunds SET status=?, reason=?, "
                "bank_txn_code=COALESCE(NULLIF(?, ''), bank_txn_code), "
                "resolved_at=? WHERE ref_no=?",
                [fb["status"], fb["reason"], fb["bank_txn_code"],
                 now if fb["status"] == "done" else None, matches[0][0]],
            )
            updated[fb["status"]] += 1
        return updated

    # -- heatmap ------------------------------------------------------------
    @_synchronized
    def data_years(self) -> list[int]:
        """Years present in the ledger (by transaction date), newest first."""
        rows = self.con.execute(
            "SELECT DISTINCT substr(trans_ts, 1, 4) AS y FROM transactions "
            "WHERE trans_ts <> '' AND trans_ts IS NOT NULL ORDER BY y DESC"
        ).fetchall()
        return [int(r[0]) for r in rows if r[0] and str(r[0]).isdigit()]

    @_synchronized
    def daily_counts(self, year: int) -> dict:
        """{'YYYY-MM-DD': (txn_count, dup_count)} for the given year."""
        rows = self.con.execute(
            "SELECT substr(trans_ts, 1, 10) AS d, COUNT(*), "
            "COUNT(*) FILTER (WHERE is_duplicate) "
            "FROM transactions WHERE substr(trans_ts, 1, 4) = ? GROUP BY 1",
            [str(year)],
        ).fetchall()
        return {r[0]: (int(r[1]), int(r[2])) for r in rows if r[0]}

    # -- transaction lookup -------------------------------------------------
    @staticmethod
    def _lookup_where(ref_no, order_id, account, charge_from, charge_to,
                      refund_from, refund_to, kinds, statuses):
        """Build the shared WHERE clause + params for the lookup/count queries."""
        # Effective refund status: originals -> 'none'; duplicates -> refund
        # status, defaulting to 'pending' when no refund row exists yet.
        eff_status = ("COALESCE(r.status, CASE WHEN t.is_duplicate "
                      "THEN 'pending' ELSE 'none' END)")
        where, params = ["1=1"], []
        if ref_no:
            where.append("t.ref_no ILIKE ?"); params.append(f"%{ref_no}%")
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
        if kinds:  # subset of {True, False} on is_duplicate
            clauses = []
            if True in kinds:
                clauses.append("t.is_duplicate")
            if False in kinds:
                clauses.append("NOT t.is_duplicate")
            if clauses:
                where.append("(" + " OR ".join(clauses) + ")")
        if statuses:
            where.append(eff_status + " IN (" + ",".join(["?"] * len(statuses)) + ")")
            params.extend(statuses)
        return " AND ".join(where), params

    @_synchronized
    def transaction_lookup_count(self, ref_no=None, order_id=None, account=None,
                                 charge_from=None, charge_to=None, refund_from=None,
                                 refund_to=None, kinds=None, statuses=None) -> int:
        """Total rows matching the lookup filters (for pagination)."""
        where, params = self._lookup_where(ref_no, order_id, account, charge_from,
                                            charge_to, refund_from, refund_to,
                                            kinds, statuses)
        return self.con.execute(
            f"SELECT COUNT(*) FROM transactions t "
            f"LEFT JOIN refunds r ON r.ref_no = t.ref_no WHERE {where}", params
        ).fetchone()[0]

    @_synchronized
    def transaction_lookup(self, ref_no=None, order_id=None, account=None,
                           charge_from=None, charge_to=None, refund_from=None,
                           refund_to=None, kinds=None, statuses=None,
                           limit=1000, offset=0):
        """Transaction-centric lookup (paginated via limit/offset). Returns every
        insurance charge — the legitimate first charge ("Gốc") and each later
        duplicate ("Trùng") — with its details and refund state, as a pandas
        DataFrame with Vietnamese column headers.
        """
        where, params = self._lookup_where(ref_no, order_id, account, charge_from,
                                            charge_to, refund_from, refund_to,
                                            kinds, statuses)
        sql = f"""
            SELECT
                t.ref_no                         AS "Mã tham chiếu",
                CASE WHEN t.is_duplicate THEN 'Trùng' ELSE 'Gốc' END AS "Phân loại",
                t.order_id                       AS "OrderID",
                t.cif                            AS "CIF",
                t.product                        AS "Sản phẩm",
                t.ky                             AS "Kỳ",
                t.corr_account                   AS "STK hưởng",
                t.corr_name                      AS "Tên hưởng",
                t.credit                         AS "Số tiền",
                t.trans_date                     AS "Ngày thu phí",
                CASE
                    WHEN NOT t.is_duplicate  THEN '—'
                    WHEN r.status = 'done'   THEN 'Hoàn thành'
                    WHEN r.status = 'failed' THEN 'Thất bại'
                    ELSE 'Chờ'
                END                              AS "Trạng thái hoàn",
                strftime(r.resolved_at, '%d/%m/%Y %H:%M:%S') AS "Ngày hoàn",
                r.bank_txn_code                  AS "FT GD hoàn",
                t.dup_of_ref                     AS "Mã tham chiếu gốc",
                o.trans_date                     AS "Ngày thu phí gốc",
                r.reason                         AS "Lý do"
            FROM transactions t
            LEFT JOIN refunds r ON r.ref_no = t.ref_no
            LEFT JOIN transactions o ON o.ref_no = t.dup_of_ref
            WHERE {where}
            ORDER BY t.trans_ts DESC NULLS LAST, t.ref_no
            LIMIT ? OFFSET ?
        """
        return self.con.execute(sql, params + [int(limit), int(offset)]).df()

    # -- single-case manual entry -------------------------------------------
    @_synchronized
    def lookup_by_ref(self, ref_no: str) -> dict | None:
        """Look up one transaction by its reference. Returns a dict of its
        details + current refund state (or None if the ref isn't found)."""
        ref_no = (ref_no or "").strip()
        if not ref_no:
            return None
        row = self.con.execute(
            """
            SELECT t.ref_no, t.is_duplicate, t.order_id, t.cif, t.product, t.ky,
                   t.corr_account, t.corr_name, t.trans_date, t.dup_of_ref,
                   r.status, r.reason, r.bank_txn_code, r.amount
            FROM transactions t
            LEFT JOIN refunds r ON r.ref_no = t.ref_no
            WHERE t.ref_no = ?
            """,
            [ref_no],
        ).fetchone()
        if not row:
            return None
        cols = ["ref_no", "is_duplicate", "order_id", "cif", "product", "ky",
                "corr_account", "corr_name", "trans_date", "dup_of_ref",
                "status", "reason", "bank_txn_code", "amount"]
        return dict(zip(cols, row))

    @_synchronized
    def apply_manual(self, ref_no: str, status: str, bank_txn_code: str = "",
                     reason: str | None = None) -> dict:
        """Update one case entered on-screen. Ensures a refund row exists for the
        duplicate (creating it from the transaction if needed), then applies the
        status / bank code / reason. Returns {'ok': bool, 'msg': str}."""
        from .refund_file import build_refund_rows  # local import: no cycle
        ref_no = (ref_no or "").strip()
        info = self.lookup_by_ref(ref_no)
        if info is None:
            return {"ok": False, "msg": f"Không tìm thấy giao dịch với mã {ref_no}."}
        if not info["is_duplicate"]:
            return {"ok": False,
                    "msg": f"Giao dịch {ref_no} không phải giao dịch trùng — không cần hoàn."}
        # Build (or ensure) the refund row from the pending-duplicate tuple.
        tup = self.con.execute(
            "SELECT ref_no, dedup_key, cif, product, trans_date, corr_account, "
            "corr_name, corr_bank, credit, ky, dup_of_ref FROM transactions "
            "WHERE ref_no = ?", [ref_no],
        ).fetchone()
        self.ensure_pending_refunds(build_refund_rows([tup]))
        outcome = self.apply_results([{
            "ref_no": ref_no, "status": status, "reason": reason,
            "bank_txn_code": bank_txn_code,
        }])
        norm = "hoàn thành" if outcome["done"] else "thất bại" if outcome["failed"] else status
        return {"ok": True, "msg": f"Đã cập nhật case {ref_no} → {norm}."}

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
