r"""LiteX Refund Tracker — Streamlit interface.

Flow:
  1. Upload bank statement  -> parse + ingest (idempotent), detect duplicates
  2. Refund queue           -> generate the 'form điền case hoàn' file
  3. Import results         -> mark refunds done/failed (1:1, never regenerate)
  4. Transaction lookup     -> search every charge (Gốc/Trùng) + refund state

Run:  .\.venv\Scripts\streamlit.exe run Refund_Tracker\app.py
"""
from __future__ import annotations

import json
import os
import re
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
from refund_tracker.batch import FileResult, aggregate, process_one_file
from refund_tracker.db import Store
from refund_tracker.refund_file import (
    build_refund_rows, write_refund_file, default_batch_id,
)
from refund_tracker.results import parse_result_file
from refund_tracker.ui import header, heatmap_html, inject_theme

st.set_page_config(page_title="LiteX Hoàn phí", page_icon=str(config.ICON_PATH),
                   layout="wide")
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
    ["1 · Tải sao kê", "2 · Hàng chờ hoàn", "3 · Nhập kết quả", "4 · Tra cứu giao dịch"]
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
    st.subheader("Nhập kết quả")

    up_tab, one_tab = st.tabs(["Theo file", "Từng case"])

    # -- 3a. Import by file --------------------------------------------------
    with up_tab:
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
                    "payment_detail": "Nội dung", "status": "Trạng thái",
                    "reason": "Lý do", "bank_txn_code": "FT GD hoàn",
                }),
                use_container_width=True,
            )
            if st.button("Cập nhật kết quả", type="primary", disabled=not results):
                st.session_state["result_outcome"] = store.apply_results(results)
                st.session_state["result_key"] = st.session_state.get("result_key", 0) + 1
                st.rerun()

        oc = st.session_state.get("result_outcome")
        if oc:
            st.success(
                f"Hoàn thành: {oc['done']} · Thất bại (trả về hàng chờ): "
                f"{oc['failed']} · Không khớp: {oc['unknown_ref']} · "
                f"Nhập nhằng (khớp >1 theo TK+nội dung): {oc['ambiguous']} · "
                f"Trạng thái không nhận diện: {oc['other']}"
            )

    # -- 3b. Enter one case on-screen ---------------------------------------
    with one_tab:
        st.caption("Nhập mã tham chiếu → tra cứu giao dịch trùng → nhập trạng "
                   "thái hoàn và Mã giao dịch Ngân hàng.")
        ref_in = st.text_input("Mã tham chiếu", key="one_ref")
        if st.button("Tra cứu", key="one_lookup"):
            st.session_state["one_searched"] = True
            st.session_state["one_info"] = store.lookup_by_ref(ref_in)
            st.session_state["one_msg"] = None

        if st.session_state.get("one_searched"):
            info = st.session_state.get("one_info")
            if info is None:
                st.info("Không tìm thấy giao dịch với mã tham chiếu này.")
            elif not info["is_duplicate"]:
                st.warning(
                    f"Giao dịch **{info['ref_no']}** là giao dịch **gốc**, không "
                    f"phải giao dịch trùng — không cần hoàn."
                )
            else:
                cur = {"done": "Hoàn thành", "failed": "Thất bại"}.get(
                    info["status"], "Chờ")
                st.markdown(
                    f"**OrderID:** {info['order_id']} · **CIF:** {info['cif']} · "
                    f"**Sản phẩm:** {info['product']} · **Kỳ:** {info['ky']}  \n"
                    f"**TK hưởng:** {info['corr_account']} — {info['corr_name']}  \n"
                    f"**Ngày thu phí:** {info['trans_date']} · "
                    f"**Trạng thái hiện tại:** {cur}"
                )
                c1, c2 = st.columns(2)
                st_label = c1.selectbox("Trạng thái hoàn", ["Done", "Fail"],
                                        key="one_status")
                bankcode = c2.text_input("FT GD hoàn", key="one_bank",
                                         value=info.get("bank_txn_code") or "")
                reason = st.text_input("Lý do (không bắt buộc)", key="one_reason",
                                       value=info.get("reason") or "")
                if st.button("Cập nhật case", type="primary", key="one_apply"):
                    r = store.apply_manual(info["ref_no"], st_label,
                                           bankcode.strip(), reason.strip() or None)
                    st.session_state["one_msg"] = r
                    if r["ok"]:
                        st.session_state["one_info"] = store.lookup_by_ref(info["ref_no"])
                    st.rerun()

        msg = st.session_state.get("one_msg")
        if msg:
            (st.success if msg["ok"] else st.warning)(msg["msg"])


# --- 4. Tra cứu giao dịch --------------------------------------------------
_STATUS_LABELS = {"Chờ": "pending", "Hoàn thành": "done", "Thất bại": "failed"}
_KIND_LABELS = {"Gốc": False, "Trùng": True}

# Color the two categorical columns to match the design system (see ui.py):
# duplicates/failed in red, done in green, pending in amber, originals muted.
_KIND_COLOR = {"Trùng": "#F32B2B", "Gốc": "#00BF36"}
_STATUS_COLOR = {"Hoàn thành": "#00BF36", "Thất bại": "#F32B2B",
                 "Chờ": "#FF9900", "—": "#888888"}


def _style_lookup(df: pd.DataFrame):
    """A pandas Styler that tints Phân loại / Trạng thái hoàn by value."""
    sty = df.style
    if "Phân loại" in df.columns:
        sty = sty.map(
            lambda v: f"color:{_KIND_COLOR.get(v, '#212121')};font-weight:600",
            subset=["Phân loại"],
        )
    if "Trạng thái hoàn" in df.columns:
        sty = sty.map(
            lambda v: f"color:{_STATUS_COLOR.get(v, '#212121')};font-weight:600",
            subset=["Trạng thái hoàn"],
        )
    return sty


with tab_history:
    st.subheader("Tra cứu giao dịch")
    r1a, r1b, r1c = st.columns(3)
    f_ref = r1a.text_input("Mã tham chiếu", key="h_ref")
    f_order = r1b.text_input("OrderID", key="h_order")
    f_stk = r1c.text_input("STK (tài khoản hưởng)", key="h_stk")
    c3, c4 = st.columns(2)
    charge_rng = c3.date_input("Ngày thu phí (từ – đến)", value=(),
                               format="DD/MM/YYYY", key="h_charge")
    refund_rng = c4.date_input("Ngày hoàn (từ – đến)", value=(),
                               format="DD/MM/YYYY", key="h_refund")
    c5, c6 = st.columns(2)
    picked_kind = c5.multiselect("Phân loại", list(_KIND_LABELS.keys()),
                                 key="h_kind")
    picked_status = c6.multiselect("Trạng thái hoàn", list(_STATUS_LABELS.keys()),
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
    _PER = 1000  # rows per page

    # Only query when the user searches — never preload the whole ledger.
    if st.button("Tra cứu", type="primary", key="h_search"):
        st.session_state["hist_filters"] = dict(
            ref_no=f_ref.strip() or None,
            order_id=f_order.strip() or None,
            account=f_stk.strip() or None,
            charge_from=cf, charge_to=ct,
            refund_from=rf, refund_to=rt,
            kinds=[_KIND_LABELS[k] for k in picked_kind] or None,
            statuses=[_STATUS_LABELS[s] for s in picked_status] or None,
        )
        st.session_state["hist_page"] = 1
        st.session_state.pop("hist_csv", None)  # invalidate prepared export

    filters = st.session_state.get("hist_filters")
    if filters is not None:
        total = store.transaction_lookup_count(**filters)
        pages = max(1, (total + _PER - 1) // _PER)
        # Clamp a stale page from a previous (larger) result before the widget.
        if st.session_state.get("hist_page", 1) > pages:
            st.session_state["hist_page"] = pages
        if pages > 1:
            pcol, _ = st.columns([1, 3])
            page = pcol.number_input(f"Trang (1–{pages})", min_value=1,
                                     max_value=pages, step=1, key="hist_page")
        else:
            page = 1
        st.write(f"**{total:,}** giao dịch · trang **{page}/{pages}**")

        hist = store.transaction_lookup(**filters, limit=_PER,
                                        offset=(page - 1) * _PER)
        # "Số tiền" is a fee amount — show it as a whole number (nullable Int64).
        if "Số tiền" in hist.columns:
            hist["Số tiền"] = hist["Số tiền"].round().astype("Int64")
        _col_cfg = {"Số tiền": st.column_config.NumberColumn("Số tiền", format="localized")}

        # A page is at most 1000 rows -> always under the Styler cell cap.
        st.dataframe(_style_lookup(hist), use_container_width=True,
                     hide_index=True, column_config=_col_cfg)
        if total:
            # Encode the active filters into the download name so exports are
            # self-describing, e.g. tra_cuu_giao_dich_order-ORD1_thuphi-
            # 20260101-20260131_trung_cho.csv. No filters -> the plain base name.
            _KIND_SLUG = {"Gốc": "goc", "Trùng": "trung"}
            _STATUS_SLUG = {"Chờ": "cho", "Hoàn thành": "hoanthanh", "Thất bại": "thatbai"}

            def _slug(s: str) -> str:
                return re.sub(r"[^0-9A-Za-z]+", "", str(s))

            def _daterange(a, b) -> str:
                return (a.strftime("%Y%m%d") if a else "") + "-" + (b.strftime("%Y%m%d") if b else "")

            parts = ["tra_cuu_giao_dich"]
            if f_ref.strip():
                parts.append("ref-" + _slug(f_ref))
            if f_order.strip():
                parts.append("order-" + _slug(f_order))
            if f_stk.strip():
                parts.append("stk-" + _slug(f_stk))
            if cf or ct:
                parts.append("thuphi-" + _daterange(cf, ct))
            if rf or rt:
                parts.append("hoan-" + _daterange(rf, rt))
            if picked_kind:
                parts.append("-".join(_KIND_SLUG[k] for k in picked_kind))
            if picked_status:
                parts.append("-".join(_STATUS_SLUG[s] for s in picked_status))
            fname = "_".join(parts) + ".csv"

            # Export ALL matching rows (not just the current page). Build once on
            # demand — querying every row can be heavy for a broad search.
            if st.button(f"Chuẩn bị CSV (tất cả {total:,} dòng)", key="hist_prep"):
                allrows = store.transaction_lookup(**filters, limit=10_000_000,
                                                   offset=0)
                st.session_state["hist_csv"] = (
                    fname, allrows.to_csv(index=False).encode("utf-8-sig"))
            prep = st.session_state.get("hist_csv")
            if prep:
                st.download_button(
                    "⬇ Tải CSV (tất cả)", prep[1], file_name=prep[0],
                    mime="text/csv", key="hist_csv_dl",
                )
