"""Pull BIDV statement e-mails from Gmail and ingest them into the ledger.

Replaces the manual "download attachment → upload in the UI" step. BIDV sends
the daily statement (split into several e-mails, "email 1/5", "2/5", ...) from
``insaoke@bidv.com.vn`` with a password-protected .xlsx attached. This module:

  1. connects to Gmail over IMAP (Gmail address + an App Password),
  2. finds BIDV statement e-mails from the last N days,
  3. decrypts each .xlsx attachment with the statement password,
  4. ingests it via :func:`refund_tracker.batch.process_one_file`.

Idempotency: each processed Gmail Message-ID + attachment name is recorded in
the ``gmail_imports`` table and skipped next run. Ingestion itself is also
idempotent on ``ref_no``, so a re-run can never double-count.

Environment variables:
    GMAIL_USER           Gmail address that receives the statements.
    GMAIL_APP_PASSWORD   Google App Password (Account → Security → App passwords).
    STATEMENT_PASSWORD   Password that opens the BIDV .xlsx (DDMMYYYY).
    STATEMENT_SENDER     Sender filter (default: insaoke@bidv.com.vn).
    GMAIL_LOOKBACK_DAYS  How many days back to search (default: 3).
    MOTHERDUCK_TOKEN / MD_DATABASE   see config.py.

Run:  python -m refund_tracker.gmail_import [--days N] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import email
import email.header
import imaplib
import io
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config  # noqa: F401 — loads .env
from .batch import FileResult, aggregate, process_one_file

GMAIL_IMAP_HOST = "imap.gmail.com"
DEFAULT_SENDER = "insaoke@bidv.com.vn"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_imports (
    message_id    VARCHAR,
    attachment    VARCHAR,
    subject       VARCHAR,
    received_at   VARCHAR,
    status        VARCHAR,   -- ok | error
    error         VARCHAR,
    imported_at   TIMESTAMP,
    PRIMARY KEY (message_id, attachment)
);
"""


@dataclass
class Attachment:
    message_id: str
    subject: str
    received_at: str
    filename: str
    data: bytes


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    return "".join(
        p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p
        for p, enc in parts
    )


def decrypt_xlsx(data: bytes, password: str) -> bytes:
    """Return plain .xlsx bytes. Unencrypted files pass through unchanged."""
    import msoffcrypto

    office = msoffcrypto.OfficeFile(io.BytesIO(data))
    if not office.is_encrypted():
        return data
    if not password:
        raise ValueError("attachment is encrypted but STATEMENT_PASSWORD is empty")
    office.load_key(password=password)
    out = io.BytesIO()
    office.decrypt(out)
    return out.getvalue()


def fetch_attachments(user: str, app_password: str, sender: str,
                      days: int) -> list[Attachment]:
    """All .xlsx attachments from ``sender`` received in the last ``days``."""
    since = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%d-%b-%Y")
    found: list[Attachment] = []
    imap = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST)
    try:
        imap.login(user, app_password)
        # "[Gmail]/All Mail" also finds statements that filters moved out of INBOX.
        status, _ = imap.select('"[Gmail]/All Mail"', readonly=True)
        if status != "OK":
            imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", since)
        for num in data[0].split():
            _, msg_data = imap.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            message_id = (msg.get("Message-ID") or f"imap-{num.decode()}").strip()
            subject = _decode(msg.get("Subject"))
            received = msg.get("Date", "")
            for part in msg.walk():
                name = _decode(part.get_filename())
                if not name.lower().endswith((".xlsx", ".xls")):
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    found.append(Attachment(message_id, subject, received, name, payload))
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return found


def run(days: int | None = None, dry_run: bool = False, store=None,
        reimport: bool = False) -> list[FileResult]:
    user = os.environ.get("GMAIL_USER", "").strip()
    app_pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    stmt_pw = os.environ.get("STATEMENT_PASSWORD", "").strip()
    sender = os.environ.get("STATEMENT_SENDER", DEFAULT_SENDER).strip()
    days = days if days is not None else int(os.environ.get("GMAIL_LOOKBACK_DAYS", "3"))
    if not user or not app_pw:
        raise SystemExit("GMAIL_USER and GMAIL_APP_PASSWORD must be set")

    attachments = fetch_attachments(user, app_pw, sender, days)
    print(f"Found {len(attachments)} attachment(s) from {sender} in the last {days} day(s)")
    if dry_run:
        for a in attachments:
            print(f"  - {a.filename}  [{a.subject}]")
        return []

    if store is None:
        from .db import Store
        store = Store()
    store.con.execute(_SCHEMA)
    print(f"Target: {store.label}")

    results: list[FileResult] = []
    with tempfile.TemporaryDirectory() as tmp:
        for a in sorted(attachments, key=lambda x: x.filename):
            done = store.con.execute(
                "SELECT 1 FROM gmail_imports WHERE message_id=? AND attachment=? "
                "AND status='ok'", [a.message_id, a.filename],
            ).fetchone()
            if done and not reimport:
                print(f"  skip (already imported): {a.filename}")
                continue
            try:
                plain = decrypt_xlsx(a.data, stmt_pw)
                path = Path(tmp) / Path(a.filename).name
                path.write_bytes(plain)
                res = process_one_file(store, str(path), a.filename)
            except Exception as exc:  # noqa: BLE001 — one bad file never aborts the run
                res = FileResult(filename=a.filename, error=f"{type(exc).__name__}: {exc}")
            results.append(res)
            store.con.execute(
                "INSERT OR REPLACE INTO gmail_imports VALUES (?,?,?,?,?,?,?)",
                [a.message_id, a.filename, a.subject, a.received_at,
                 "ok" if res.ok else "error", res.error, _dt.datetime.now()],
            )
            print(f"  {'OK ' if res.ok else 'ERR'} {a.filename}: new_rows={res.new_rows} "
                  f"new_dups={res.new_duplicates} skipped={res.skipped_existing} "
                  f"flagged={res.flagged_rows} {res.error}")

    print("Summary:", aggregate(results))
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=None, help="lookback window in days")
    ap.add_argument("--dry-run", action="store_true", help="list attachments only")
    ap.add_argument("--reimport", action="store_true",
                    help="re-read already-imported attachments (e.g. after a parser fix); "
                         "rows already in the ledger are still skipped by ref_no")
    args = ap.parse_args(argv)
    results = run(days=args.days, dry_run=args.dry_run, reimport=args.reimport)
    return 1 if any(not r.ok for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
