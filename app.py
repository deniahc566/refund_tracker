r"""LiteX Refund Tracker — Streamlit interface.

Flow:
  1. Upload bank statement  -> parse + ingest (idempotent), detect duplicates
  2. Refund queue           -> generate the 'form điền case hoàn' file
  3. Import results         -> mark refunds done/failed (1:1, never regenerate)
  4. Dashboard              -> ledger stats & history

Run:  .\.venv\Scripts\streamlit.exe run Refund_Tracker\app.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

# Bridge Streamlit Cloud secrets -> environment so config.py picks them up.
# (Streamlit Cloud has no .env file; set these under App → Settings → Secrets.)
try:
    for _k in ("MOTHERDUCK_TOKEN", "MD_DATABASE", "REFUND_DB_PATH"):
        if _k in st.secrets:
            os.environ.setdefault(_k, str(st.secrets[_k]))
except Exception:
    pass

from refund_tracker import config
from refund_tracker.db import Store
from refund_tracker.parser import parse_statement, file_hash
from refund_tracker.refund_file import (
    build_refund_rows, write_refund_file, default_batch_id,
)
from refund_tracker.results import parse_result_file

st.set_page_config(page_title="LiteX Refund Tracker", page_icon="💸", layout="wide")


@st.cache_resource
def get_store() -> Store:
    return Store()


store = get_store()

st.title("💸 LiteX Refund Tracker")
st.caption(f"Data store: **{store.label}**")

tab_up, tab_queue, tab_import, tab_dash = st.tabs(
    ["1 · Upload statement", "2 · Refund queue", "3 · Import results", "4 · Dashboard"]
)


def _save_tmp(uploaded) -> str:
    suffix = Path(uploaded.name).suffix or ".xlsx"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded.getbuffer())
    tmp.close()
    return tmp.name


# --- 1. Upload statement ---------------------------------------------------
with tab_up:
    st.subheader("Upload sao kê ngân hàng (BIDV)")
    up = st.file_uploader("Statement .xlsx", type=["xlsx"], key="stmt")
    if up is not None:
        path = _save_tmp(up)
        with st.spinner("Parsing…"):
            txns, summary = parse_statement(path)
        c1, c2, c3 = st.columns(3)
        c1.metric("Numbered rows", summary["total_numbered_rows"])
        c2.metric("Insurance premium rows", summary["insurance_rows"])
        c3.metric("Non-insurance credits skipped", summary["non_insurance_credit_rows"])
        if summary["unmatched_samples"]:
            with st.expander("Skipped credit rows (not insurance premiums)"):
                st.write(summary["unmatched_samples"])

        if txns:
            st.dataframe(
                pd.DataFrame([t.as_dict() for t in txns[:50]])[
                    ["ref_no", "trans_date", "cif", "product", "ky", "order_id",
                     "corr_account", "corr_name", "credit"]
                ],
                use_container_width=True,
            )
            st.caption(f"Showing first 50 of {len(txns)} parsed rows.")

        if st.button("Ingest into ledger", type="primary", disabled=not txns):
            report = store.ingest(txns, up.name, summary, file_hash(path))
            st.success(
                f"Ingested. New rows: {report['new_rows']} · "
                f"Already present (skipped): {report['skipped_existing']} · "
                f"**New duplicates flagged: {report['new_duplicates']}**"
            )
            if report["new_duplicates"]:
                st.info("Go to the **Refund queue** tab to generate the refund file.")


# --- 2. Refund queue -------------------------------------------------------
with tab_queue:
    st.subheader("Pending refunds (duplicate charges)")
    pending = store.pending_duplicates()
    st.write(f"**{len(pending)}** duplicate transaction(s) awaiting refund.")
    if pending:
        refund_rows = build_refund_rows(pending)
        df = pd.DataFrame(refund_rows)
        st.dataframe(
            df[["ref_no", "cif", "product", "beneficiary_account",
                "beneficiary_name", "beneficiary_bank", "amount",
                "payment_detail", "needs_review"]],
            use_container_width=True,
        )
        if df["needs_review"].any():
            st.warning(
                "Some rows use a product with no configured bank/amount rule "
                "(needs_review = True). They fall back to the statement values — "
                "verify before sending."
            )

        if st.button("Generate refund file", type="primary"):
            batch_id = default_batch_id()
            out = str(Path(tempfile.gettempdir()) / f"refund_{batch_id}.xlsx")
            write_refund_file(refund_rows, out)
            store.record_refund_batch(refund_rows, batch_id)
            with open(out, "rb") as f:
                st.download_button(
                    f"⬇ Download {Path(out).name}", f.read(),
                    file_name=f"form_dien_case_hoan_{batch_id}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            st.success(
                f"Batch {batch_id} recorded ({len(refund_rows)} refunds set to "
                f"'pending'). These won't reappear in the queue."
            )
            st.rerun()
    else:
        st.info("Nothing pending. Upload a statement that repeats a prior charge.")


# --- 3. Import results -----------------------------------------------------
with tab_import:
    st.subheader("Import completed refund file")
    st.caption(
        "Re-upload the generated file with the **Status** (Done/Failed) and "
        "**Reason** columns filled in. Matched back by the Ref column."
    )
    res_up = st.file_uploader("Completed refund .xlsx", type=["xlsx"], key="result")
    if res_up is not None:
        path = _save_tmp(res_up)
        results = parse_result_file(path)
        st.write(f"Parsed **{len(results)}** result row(s).")
        st.dataframe(pd.DataFrame(results), use_container_width=True)
        if st.button("Apply results", type="primary", disabled=not results):
            outcome = store.apply_results(results)
            st.success(
                f"Done: {outcome['done']} · Failed (returned to queue): "
                f"{outcome['failed']} · Unknown ref: {outcome['unknown_ref']} · "
                f"Unrecognized status: {outcome['other']}"
            )
            st.rerun()


# --- 4. Dashboard ----------------------------------------------------------
with tab_dash:
    st.subheader("Ledger overview")
    s = store.stats()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total transactions", s["total_transactions"])
    c2.metric("Duplicates detected", s["duplicates"])
    c3.metric("Pending refunds", s["pending_refunds"])
    c4.metric("Refunded (VND)", f"{s['refunded_amount']:,.0f}")
    st.write("**Refunds by status:**", s["refunds_by_status"] or "—")
