r"""LiteX Refund Tracker — Streamlit interface.

Flow:
  1. Upload bank statement  -> parse + ingest (idempotent), detect duplicates
  2. Refund queue           -> generate the 'form điền case hoàn' file
  3. Import results         -> mark refunds done/failed (1:1, never regenerate)
  4. Dashboard              -> ledger stats & history

Run:  .\.venv\Scripts\streamlit.exe run Refund_Tracker\app.py
"""
from __future__ import annotations

import json
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

from refund_tracker.batch import FileResult, aggregate, process_one_file
from refund_tracker.db import Store
from refund_tracker.refund_file import (
    build_refund_rows, write_refund_file, default_batch_id,
)
from refund_tracker.results import parse_result_file
from refund_tracker.ui import header, heatmap_html, inject_theme

st.set_page_config(page_title="LiteX Hoàn phí", page_icon="💸", layout="wide")
inject_theme()


@st.cache_resource
def get_store() -> Store:
    return Store()


store = get_store()

header("Theo dõi hoàn phí")

# --- Tổng quan: hiển thị giữa header và các tab ----------------------------
_s = store.stats()
d1, d2, d3, d4 = st.columns(4)
d1.metric("Tổng giao dịch", f"{_s['total_transactions']:,}")
d2.metric("Giao dịch trùng", f"{_s['duplicates']:,}")
d3.metric("Chờ hoàn", f"{_s['pending_refunds']:,}")
d4.metric("Đã hoàn (VND)", f"{_s['refunded_amount']:,.0f}")

# --- Heatmap theo ngày (kiểu GitHub) ---------------------------------------
_years = store.data_years() or [pd.Timestamp.now().year]
hm1, hm2 = st.columns([2, 1])
_metric_label = hm1.radio(
    "Số lượng giao dịch theo ngày",
    ["Số giao dịch thu phí", "Số giao dịch trùng"],
    horizontal=True, key="hm_metric",
)
_year = hm2.selectbox("Năm", _years, key="hm_year")
_metric = "dup" if _metric_label == "Số giao dịch trùng" else "txn"
st.markdown(
    heatmap_html(store.daily_counts(int(_year)), int(_year), _metric),
    unsafe_allow_html=True,
)
st.divider()

tab_up, tab_queue, tab_import, tab_history = st.tabs(
    ["1 · Tải sao kê", "2 · Hàng chờ hoàn", "3 · Nhập kết quả", "4 · Lịch sử hoàn phí"]
)


def _save_bytes(name: str, data: bytes) -> str:
    """Write uploaded bytes to a temp file and return its path."""
    suffix = Path(name).suffix or ".xlsx"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return tmp.name


# --- 1. Upload statement ---------------------------------------------------
with tab_up:
    st.subheader("Tải sao kê ngân hàng")
    # A rotating key lets us clear the uploader after processing: bumping it
    # remounts the widget empty, so files don't retain between batches.
    ups = st.file_uploader(
        "File sao kê .xlsx (một hoặc nhiều)",
        type=["xlsx"],
        accept_multiple_files=True,
        key=f"stmt_{st.session_state.get('stmt_key', 0)}",
    )
    ups = ups or []
    n_files = len(ups)
    if n_files:
        st.caption(f"Đã chọn {n_files} file.")

    if st.button(f"Xử lý {n_files} file", type="primary", disabled=(n_files == 0)):
        # Deterministic order (sorted by filename) for predictable UX. Duplicate
        # detection itself is order-independent (recomputed by trans_ts in the DB).
        files = sorted(ups, key=lambda f: f.name)
        results: list[FileResult] = []
        progress = st.progress(0.0, text=f"Bắt đầu… (0/{n_files})")
        for i, uploaded in enumerate(files, start=1):
            with st.status(
                f"Đang xử lý {uploaded.name} ({i}/{n_files})…", expanded=False
            ) as status_box:
                try:
                    tmp_path = _save_bytes(uploaded.name, uploaded.getvalue())
                    res = process_one_file(store, tmp_path, uploaded.name)
                except Exception as exc:  # noqa: BLE001 — never abort the batch
                    res = FileResult(
                        filename=uploaded.name,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                results.append(res)
                status_box.update(
                    label=(
                        f"✓ {uploaded.name} — {res.new_rows} mới, "
                        f"{res.new_duplicates} trùng, "
                        f"{res.skipped_existing} đã có"
                    ) if res.ok else f"✗ {uploaded.name} — {res.error}",
                    state="complete" if res.ok else "error",
                )
            progress.progress(i / n_files, text=f"Đã xử lý {i}/{n_files} file")
        progress.empty()
        # Persist the summary, then clear the uploader (fresh key) and rerun so
        # the file list empties while the results below stay visible.
        st.session_state["stmt_results"] = [r.as_row() for r in results]
        st.session_state["stmt_agg"] = aggregate(results)
        st.session_state["stmt_key"] = st.session_state.get("stmt_key", 0) + 1
        st.rerun()

    # -- Results from the last batch (persist after the uploader is cleared) -
    agg = st.session_state.get("stmt_agg")
    if agg:
        st.markdown("#### Tổng kết")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Số file xử lý", agg["files_processed"])
        c2.metric("File lỗi", agg["files_failed"])
        c3.metric("Tổng dòng mới", agg["total_new_rows"])
        c4.metric("Trùng mới", agg["total_new_duplicates"])
        st.markdown("#### Kết quả từng file")
        st.dataframe(
            pd.DataFrame(st.session_state.get("stmt_results", [])),
            use_container_width=True,
            hide_index=True,
        )
        if agg["files_failed"]:
            st.warning(
                f"{agg['files_failed']} file bị lỗi — xem cột **Lỗi** ở trên. "
                f"Các file còn lại vẫn được xử lý."
            )
        if agg["total_new_duplicates"]:
            st.info(
                f"**Phát hiện {agg['total_new_duplicates']} giao dịch trùng mới.** "
                f"Sang tab **Hàng chờ hoàn** để tải file hoàn phí."
            )
        elif agg["files_processed"]:
            st.success("Đã xử lý xong. Không có giao dịch trùng mới.")


# --- 2. Refund queue -------------------------------------------------------
@st.cache_data(show_spinner=False)
def _build_refund_bytes(payload: str) -> bytes:
    """Build the bulk-payment file bytes. Cached by content (`payload` = the
    refund rows as JSON) so the 186 KB template isn't re-loaded on every rerun
    — only when the pending set actually changes."""
    refund_rows = json.loads(payload)
    out = str(Path(tempfile.gettempdir()) / f"refund_{default_batch_id()}.xlsx")
    write_refund_file(refund_rows, out)
    with open(out, "rb") as f:
        return f.read()


with tab_queue:
    st.subheader("Các khoản chờ hoàn")
    pending = store.pending_duplicates()
    st.write(f"**{len(pending)}** khoản đang chờ hoàn (chưa hoàn thành).")

    if pending:
        refund_rows = build_refund_rows(pending)
        # Make sure each pending duplicate has a refund row so the result-import
        # step can match it later. Idempotent — only creates missing rows.
        store.ensure_pending_refunds(refund_rows)

        df = pd.DataFrame(refund_rows)
        view = df[["ref_no", "cif", "product", "beneficiary_account",
                   "beneficiary_name", "beneficiary_bank", "amount", "currency",
                   "payment_detail", "needs_review"]].rename(columns={
            "ref_no": "Mã tham chiếu", "cif": "CIF", "product": "Sản phẩm",
            "beneficiary_account": "TK hưởng", "beneficiary_name": "Tên hưởng",
            "beneficiary_bank": "Ngân hàng", "amount": "Số tiền",
            "currency": "Loại tiền", "payment_detail": "Nội dung",
            "needs_review": "Cần kiểm tra",
        })
        st.dataframe(view, use_container_width=True)
        if df["needs_review"].any():
            st.warning(
                "Một số dòng thuộc sản phẩm chưa cấu hình ngân hàng/số tiền "
                "(Cần kiểm tra = True) — đang lấy tạm giá trị từ sao kê. Hãy "
                "kiểm tra trước khi gửi."
            )

        # Always-available download: the file is rebuilt from the current queue
        # on every render, so it can be downloaded at any time.
        st.download_button(
            f"⬇ Tải file hoàn phí ({len(refund_rows)} dòng)",
            _build_refund_bytes(json.dumps(refund_rows, sort_keys=True, default=str)),
            file_name=f"bulk_payment_{default_batch_id()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            key="refund_dl",
        )
        st.caption(
            "Các khoản vẫn nằm đây cho đến khi được đánh dấu Hoàn thành ở tab "
            "**Nhập kết quả**. Khoản thất bại vẫn ở lại hàng chờ. Tải file bất cứ lúc nào."
        )
    else:
        st.info("Không có khoản nào chờ hoàn. Hãy tải sao kê có giao dịch bị thu trùng.")


# --- 3. Import results -----------------------------------------------------
with tab_import:
    st.subheader("Nhập file kết quả đã thực hiện")
    res_up = st.file_uploader(
        "File hoàn phí đã thực hiện (.xlsx)",
        type=["xlsx"],
        key=f"result_{st.session_state.get('result_key', 0)}",
    )
    if res_up is not None:
        results = parse_result_file(_save_bytes(res_up.name, res_up.getvalue()))
        st.caption(f"**{res_up.name}** — đọc được {len(results)} dòng kết quả.")
        st.dataframe(
            pd.DataFrame(results).rename(columns={
                "ref_no": "Mã tham chiếu", "beneficiary_account": "TK hưởng",
                "payment_detail": "Nội dung", "status": "Trạng thái", "reason": "Lý do",
            }),
            use_container_width=True,
        )
        if st.button("Cập nhật kết quả", type="primary", disabled=not results):
            st.session_state["result_outcome"] = store.apply_results(results)
            # Clear the uploader (fresh key) so the file doesn't retain.
            st.session_state["result_key"] = st.session_state.get("result_key", 0) + 1
            st.rerun()

    # Outcome persists after the uploaded file is cleared.
    oc = st.session_state.get("result_outcome")
    if oc:
        st.success(
            f"Hoàn thành: {oc['done']} · Thất bại (trả về hàng chờ): "
            f"{oc['failed']} · Không khớp: {oc['unknown_ref']} · "
            f"Nhập nhằng (khớp >1 theo TK+nội dung): {oc['ambiguous']} · "
            f"Trạng thái không nhận diện: {oc['other']}"
        )


# --- 4. Lịch sử hoàn phí ---------------------------------------------------
_STATUS_LABELS = {"Hoàn thành": "done", "Thất bại": "failed", "Chờ": "pending"}

with tab_history:
    st.subheader("Lịch sử hoàn phí")
    c1, c2 = st.columns(2)
    f_order = c1.text_input("OrderID", key="h_order")
    f_stk = c2.text_input("STK (tài khoản hưởng)", key="h_stk")
    c3, c4 = st.columns(2)
    charge_rng = c3.date_input("Ngày thu phí (từ – đến)", value=(),
                               format="DD/MM/YYYY", key="h_charge")
    refund_rng = c4.date_input("Ngày hoàn (từ – đến)", value=(),
                               format="DD/MM/YYYY", key="h_refund")
    picked_status = st.multiselect("Trạng thái", list(_STATUS_LABELS.keys()),
                                   key="h_status")

    def _range(v):
        """A st.date_input range -> (from, to); tolerant of 0/1/2 picks."""
        if isinstance(v, (list, tuple)):
            if len(v) == 2:
                return v[0], v[1]
            if len(v) == 1:
                return v[0], v[0]
        return None, None

    cf, ct = _range(charge_rng)
    rf, rt = _range(refund_rng)
    hist = store.refund_history(
        order_id=f_order.strip() or None,
        account=f_stk.strip() or None,
        charge_from=cf, charge_to=ct,
        refund_from=rf, refund_to=rt,
        statuses=[_STATUS_LABELS[s] for s in picked_status] or None,
    )
    st.write(f"**{len(hist)}** kết quả.")
    st.dataframe(hist, use_container_width=True, hide_index=True)
    if len(hist):
        st.download_button(
            "⬇ Tải CSV",
            hist.to_csv(index=False).encode("utf-8-sig"),
            file_name="lich_su_hoan_phi.csv",
            mime="text/csv",
            key="hist_csv",
        )
