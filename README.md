# LiteX Refund Tracker

Detects **duplicate insurance-premium charges** in BIDV account statements and
manages **1:1 refunds** against them. Duplicates are tracked across every upload
in a persistent MotherDuck (DuckDB) ledger, so a refunded charge never shows up
in a future refund file.

## Why a persistent ledger

A single statement almost never contains a duplicate — every certificate
(`order_id`) appears once. A duplicate is when the **same certificate + period**
is charged **again in a later statement**. Detecting that requires remembering
every charge ever seen, which is what the ledger does.

## Duplicate rule (first-seen-wins)

Each premium line in column **L** is parsed into a dedup key:

```
Diễn giải (L):
REM Tfr Ac:6330421880 O@L_..._2594238187_357273116558295040_Phi Bao hiem Bao An Tai Khoan Ky 2 cua ma KH 6330421880

dedup_key = order_id | ky | cif | product
          = 357273116558295040 | ky 2 | 6330421880 | bao an tai khoan
```

| Part        | Value                | Meaning                          |
|-------------|----------------------|----------------------------------|
| `order_id`  | 357273116558295040   | Unique per certificate           |
| `ky`        | 2                    | Payment period                   |
| `cif`       | 6330421880           | Customer CIF (`cua ma KH`)       |
| `product`   | Bao An Tai Khoan     | Product name                     |

For each dedup key, the **earliest** charge (by transaction time, then SEQ, then
reference) is legitimate. Every later charge with the same key is a **refundable
duplicate**.

Idempotency: the bank **reference number** (column P) is unique per physical line
and is the primary key, so re-uploading an overlapping statement never
double-counts.

## Refund file mapping

Pending duplicates are written into `templates/form_dien_case_hoan.xlsx`:

| Form column                              | Source                                   |
|------------------------------------------|------------------------------------------|
| (2) Số tài khoản                         | `Tài khoản đối ứng` (col M)              |
| (3) Tên đơn vị thụ hưởng                 | `Tên đối ứng` (col N)                    |
| (4) Ngân hàng thụ hưởng                  | product rule → **BIDV** for Bao An Tài Khoản |
| (5) Số tiền                              | product rule → **5,000** for Bao An Tài Khoản |
| (6) Chi tiết thanh toán                  | `Hoan Phi Bao Hiem <product> Ky <ky> cua KH <cif>` |
| (Ref) *added column G*                   | transaction reference — for result round-trip |

Product rules live in `refund_tracker/config.py` (`PRODUCT_CONFIG`). A product
with no rule falls back to the statement's own bank/amount and is flagged
`needs_review`.

## Result import

Re-upload the generated file with the **Status** (Done/Failed) and **Reason**
columns (H, I) filled in. Rows are matched back by the **Ref** column:
`Done` closes the refund permanently; `Failed` returns it to the queue.

## Setup

```powershell
# From D:\LiteX  (reuses the existing .venv)
.\.venv\Scripts\pip.exe install -r Refund_Tracker\requirements.txt

# Configure the database
copy Refund_Tracker\.env.example Refund_Tracker\.env
# then edit .env and set MOTHERDUCK_TOKEN  (leave empty to use a local .duckdb file)
```

## Run

```powershell
.\.venv\Scripts\streamlit.exe run Refund_Tracker\app.py
```

Tabs: **1 Upload statement** → **2 Refund queue** (generate file) →
**3 Import results** → **4 Dashboard**.

## Automatic import from Gmail

BIDV e-mails the daily statement from `insaoke@bidv.com.vn`, split across
several messages ("email 1/5", "2/5", ...), each with a password-protected
`.xlsx`. `refund_tracker/gmail_import.py` replaces the manual download/upload:
it reads those e-mails over IMAP, decrypts each attachment, and ingests it into
MotherDuck exactly like an upload in tab 1. Processed attachments are recorded
in the `gmail_imports` table and skipped on later runs (ingestion is also
idempotent on `ref_no`).

The e-mailed files use a compact layout without the *đối ứng* columns; the
parser finds columns by header text, and takes the refund account from
`Tfr Ac:<account>` in the description.

**Scheduled (GitHub Actions):** `.github/workflows/gmail_import.yml` runs daily
at 08:30 Vietnam time (and on demand from the Actions tab). Add these repo
secrets under *Settings → Secrets and variables → Actions*:

| Secret               | Value                                                  |
|----------------------|--------------------------------------------------------|
| `GMAIL_USER`         | Gmail address that receives the statements             |
| `GMAIL_APP_PASSWORD` | Google App Password (needs 2-Step Verification on)     |
| `STATEMENT_PASSWORD` | Password that opens the `.xlsx` (DDMMYYYY)             |
| `MOTHERDUCK_TOKEN`   | MotherDuck token                                       |

**Manual / local:** set the same values in `.env`, then

```powershell
.\.venv\Scripts\python.exe -m refund_tracker.gmail_import --dry-run   # list only
.\.venv\Scripts\python.exe -m refund_tracker.gmail_import --days 7
```

## Deploy to Streamlit Cloud

1. Push this folder to a GitHub repo (its contents at the repo **root**, so
   `app.py`, `refund_tracker/`, `templates/`, and `requirements.txt` sit at the
   top level).
2. On https://share.streamlit.io → **New app**, pick the repo/branch and set the
   **Main file path** to `app.py`.
3. Open **App → Settings → Secrets** and paste (see
   [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example)):
   ```toml
   MOTHERDUCK_TOKEN = "your-motherduck-token"
   MD_DATABASE = "refund_tracker"
   ```

> **Use MotherDuck in the cloud.** Streamlit Cloud's filesystem is ephemeral —
> without a token the app falls back to a local `.duckdb` that is wiped on every
> reboot/redeploy, losing the refund ledger. The token makes storage persistent.

## Layout

```
Refund_Tracker/
├── app.py                       Streamlit UI
├── refund_tracker/
│   ├── config.py                DB target, product rules, column maps
│   ├── gmail_import.py          Gmail (IMAP) → decrypt → ingest, run daily
│   ├── parser.py                statement .xlsx → Txn records + dedup key
│   ├── db.py                    DuckDB/MotherDuck: ingest, dedup, queue, results
│   ├── refund_file.py           fill the refund-form template
│   └── results.py               read a completed refund file
├── templates/form_dien_case_hoan.xlsx
├── requirements.txt · .env.example · .gitignore
```
