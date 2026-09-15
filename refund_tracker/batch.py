"""Batch processing for multi-file statement uploads.

Factored out of the Streamlit UI so the per-file work is unit-testable without
a running Streamlit session. Each file is parsed and ingested independently;
one bad file never aborts the batch (errors are captured on the result row).

Duplicate detection is recomputed in the DB ordered by ``trans_ts`` on every
ingest, so the ORDER files are processed in does not change which charge is
flagged. Callers should still process files in a deterministic order (sorted by
filename) for predictable UX.
"""
from __future__ import annotations

from dataclasses import dataclass

from .parser import parse_statement, file_hash


@dataclass
class FileResult:
    """Outcome of parsing + ingesting a single statement file."""

    filename: str
    total_rows: int = 0
    insurance_rows: int = 0
    new_rows: int = 0
    new_duplicates: int = 0
    skipped_existing: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def as_row(self) -> dict:
        """Flat dict for a pandas results table (one row per file)."""
        return {
            "File": self.filename,
            "Tổng dòng": self.total_rows,
            "Dòng bảo hiểm": self.insurance_rows,
            "Dòng mới": self.new_rows,
            "Trùng mới": self.new_duplicates,
            "Bỏ qua (đã có)": self.skipped_existing,
            "Trạng thái": "OK" if self.ok else "LỖI",
            "Lỗi": self.error,
        }


def process_one_file(store, path: str, filename: str) -> FileResult:
    """Parse + ingest one statement file.

    Never raises: any parse/ingest failure is captured in ``FileResult.error``
    so the caller can keep processing the rest of the batch.

    Args:
        store: a ``refund_tracker.db.Store`` (anything with an ``ingest`` method).
        path: local filesystem path to the .xlsx to read.
        filename: the original/display name recorded as the ingest source_file.
    """
    try:
        txns, summary = parse_statement(path)
        report = store.ingest(txns, filename, summary, file_hash(path))
        return FileResult(
            filename=filename,
            total_rows=summary.get("total_numbered_rows", 0),
            insurance_rows=summary.get("insurance_rows", 0),
            new_rows=report["new_rows"],
            new_duplicates=report["new_duplicates"],
            skipped_existing=report["skipped_existing"],
        )
    except Exception as exc:  # noqa: BLE001 — surface any failure per-file
        return FileResult(filename=filename, error=f"{type(exc).__name__}: {exc}")


def aggregate(results) -> dict:
    """Totals across a list of :class:`FileResult`."""
    results = list(results)
    return {
        "files_processed": sum(1 for r in results if r.ok),
        "files_failed": sum(1 for r in results if not r.ok),
        "total_new_rows": sum(r.new_rows for r in results),
        "total_new_duplicates": sum(r.new_duplicates for r in results),
        "total_insurance_rows": sum(r.insurance_rows for r in results),
        "total_skipped_existing": sum(r.skipped_existing for r in results),
    }
