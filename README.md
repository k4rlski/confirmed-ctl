# confirmed-ctl

> Bank-transaction ingest and newspaper-ad payment confirmation for PERM-Ads.com LLC. Part of the **auto-ctl.io** suite.

confirmed-ctl closes the financial loop on newspaper advertisement purchases. It
ingests Bank of America transactions — by **scanning BofA alert emails in Gmail**
and by **importing BofA account exports (OFX / CSV)** — into a Postgres database,
ranks candidate transactions against unconfirmed CRM ad-buys, and records the
human-confirmed relationship between an ad, its bank charge, and its Gmail
confirmation thread. Receipt capture for confirmed ads is available in-suite and
via the separate receipt-ctl tool.

> **Design of record:** [`docs/ARCHITECTURE-SPEC.md`](docs/ARCHITECTURE-SPEC.md).
> This replaces the earlier Plaid + MySQL/EspoCRM design (Plaid is no longer
> viable); the `docs/DESIGN.md`, `CRM-SCHEMA.md`, and `DROPBOX.md` documents
> describe that superseded approach and are retained for history only.
>
> **Pivot note:** the QuickBooks Online (QBO) API sync backend was removed in
> Phase 1. Ingestion is now BofA email-scan + BofA export (OFX/CSV). The
> replacement ingestion adapters land in a later generation; the `sync` command
> and daemon are honest stubs until then (see [CLI](#cli)).

---

## What it does

```
BofA alert emails (Gmail)  +  BofA exports (OFX / CSV)
    │  confirmed-ctl sync   (ingestion adapters — land in a later gen)
    ▼
bank_transactions  ──►  matching/scorer  ──►  ranked candidates in the Flask popup
                                                     │  human clicks CONFIRM
                                                     ▼
                                              ad_confirmations  (ad ↔ txn ↔ gmail thread)
                                                     │
                                                     ▼
                                    receipt-ctl (separate tool) downloads the PDF
```

- **Ingestion** — `ingest/` will host the BofA email-scan and OFX/CSV export
  adapters that upsert rows into `bank_transactions`, logging every run to
  `confirmed_ctl_sync_log`. Idempotency is enforced by a composite
  `UNIQUE(source, source_txn_id)`; `ingest/dedup.py` derives a deterministic
  `source_txn_id` for every row (see [Schema](#schema--configuration)).
- **Matching** — `matching/scorer.py` ranks candidate transactions by amount,
  vendor-name similarity, and date proximity. `matching/rag.py` (ChromaDB) stores
  confirmed matches as pattern memory for future ranking.
- **Gmail** — `gmail/client.py` searches Gmail threads by ad number for the popup;
  `gmail/receipts.py` downloads receipt attachments for confirmed ads.
- **Flask API** — `api/routes.py` exposes the endpoints the existing app's
  "Confirmed-CTL" popup calls (`/sync`, `/candidates/<ad_id>`, `/confirm`,
  `/unconfirmed`, `/sync/status`).

Receipt download/archiving is also handled by the separate **receipt-ctl** tool
(see [`docs/RECEIPT-CTL.md`](docs/RECEIPT-CTL.md)); a lightweight in-suite
downloader is available via `confirmed-ctl receipts`.

### BofA email-scan: which mailbox, and where the alerts live

The scan impersonates **`karl@perm-ads.com`** by default (`GMAIL_IMPERSONATE`).
`karl@` is the primary mailbox because it holds every BofA transaction alert in
its **durable INBOX** (not Trash), and it also receives Paul's `info@` vendor
**ad-confirmation emails** — which confirmed-ctl searches by the CRM ad-number
field `adnumbernews`. `info@perm-ads.com` is the delivery address: a Gmail filter
there auto-sends BofA alerts to **Trash**, which Gmail purges after ~30 days, so
using `info@` would require a **daily** scan to stay complete.

The mailbox is configurable via `GMAIL_IMPERSONATE` (default
`karl@perm-ads.com`; set `info@perm-ads.com` or the non-forwarded admin mailbox
as alternatives). Regardless of mailbox, the Gmail client lists with
`includeSpamTrash=True` **defensively** (`gmail/client.py::search_messages`) so
any trashed alert is still found. Gmail settings/filters are never modified.

The scan uses a broad, date-bounded **sender** query
(`from:onlinebanking@ealerts.bankofamerica.com after:<epoch>`) — not brittle
`subject:"…"` phrase queries, which under-match — then classifies each message
by a case-insensitive substring of its raw `Subject` header and parses the
HTML-only body by **pairing the two-column data-table cells** (label `<td>` →
value `<td>`) with BeautifulSoup.

---

## CLI

```bash
# Ingest recent bank transactions into the local DB.
# NOTE: ingestion adapters are not wired yet — this is currently a stub that
# exits NON-ZERO (no transactions ingested, no SyncLog written) so cron does
# not treat it as a successful sync. Adapters land in a later gen.
confirmed-ctl sync --lookback-days 2

# Last sync run info
confirmed-ctl status

# Ranked bank-transaction candidates for one ad
confirmed-ctl match --ad-id 1234

# Download receipts for confirmed ads that have a Gmail thread id
confirmed-ctl receipts
```

Daemon mode runs via `python -m confirmed_ctl.daemon` (see
`confirmed-ctl.service`). It wakes on `SYNC_INTERVAL_SECONDS` and currently only
logs an idle heartbeat at INFO — no ingestion runs until the adapters land.

---

## Install & configure

```bash
python -m venv venv && . venv/bin/activate
pip install -e ".[live]"        # runtime; use ".[dev]" for tests/lint
cp .env.example .env            # then fill in DATABASE_URL + Gmail values

# Create confirmed-ctl's tables in the Postgres database
alembic upgrade head
```

Configuration is environment-driven (see `.env.example`): `DATABASE_URL`,
`GMAIL_TOKEN_PATH`, `CHROMA_PATH`, `RECEIPTS_BASE_PATH`, `SYNC_INTERVAL_SECONDS`.
There are no QBO settings — the QBO backend was removed in Phase 1.

---

## Schema & configuration

confirmed-ctl **owns** `bank_transactions`, `ad_confirmations`, and
`confirmed_ctl_sync_log` in the fang Postgres store. It references the existing
CRM table `ad_purchases` via foreign keys but never creates or migrates it.

`bank_transactions` idempotency / provenance columns:

- `source` — the ingestion adapter that produced the row. `NOT NULL` and
  restricted by a CHECK constraint to `{'email-scan', 'export-ofx', 'export-csv'}`.
- `source_txn_id` — the source's stable per-transaction id. `NOT NULL` and
  ALWAYS populated. `UNIQUE(source, source_txn_id)` makes re-ingestion
  idempotent. The value is derived by
  `confirmed_ctl.ingest.dedup.deterministic_source_txn_id`:
  - **OFX** (`export-ofx`) → the statement `<FITID>`.
  - **email/CSV** (`email-scan` / `export-csv`) → a hex SHA-256 of the
    normalized natural key `(source, posted_date, amount, description, last4)`.

A partial index `idx_bank_txn_unmatched_date` on `txn_date WHERE confirmed_ad_id
IS NULL` accelerates the unmatched-queue candidate lookup.

> DB provisioning note: at time of writing the fang Postgres instance is not yet
> provisioned, so migrations have not been applied anywhere. The schema is
> greenfield — `0001_initial` is the single source of truth.

---

## Development

```bash
pip install -e ".[dev]"
pytest            # unit tests (scoring logic, dedup helper, import smoke)
ruff check confirmed_ctl tests
```

Heavy integrations (Google API, ChromaDB, Flask, psycopg2) are imported lazily,
so the core package, CLI, and test suite import without them installed and the
tests run without a database.

### Project layout

```
confirmed_ctl/
├── cli.py            # Click CLI: sync (stub), status, match, receipts
├── daemon.py         # idle heartbeat loop (systemd)
├── settings.py       # env-driven configuration
├── ingest/           # ingestion adapters (later gen) + dedup helper
├── gmail/            # thread search + receipt download
├── matching/         # scorer + ChromaDB RAG
├── db/               # SQLAlchemy models, session, Alembic migrations
└── api/routes.py     # Flask blueprint for the Confirmed-CTL popup
```

---

## Related tools (auto-ctl.io suite)

- [`receipt-ctl`](docs/RECEIPT-CTL.md) — downloads Gmail receipt attachments and
  archives them (reads `ad_confirmations.gmail_thread_id`).

---

## Docs

- [ARCHITECTURE-SPEC.md](docs/ARCHITECTURE-SPEC.md) — **current** full system architecture
- [RECEIPT-CTL.md](docs/RECEIPT-CTL.md) — receipt-ctl standalone specification
- [ROADMAP.md](docs/ROADMAP.md) — phased build plan
- Historical (superseded Plaid design): [DESIGN.md](docs/DESIGN.md), [CRM-SCHEMA.md](docs/CRM-SCHEMA.md), [DROPBOX.md](docs/DROPBOX.md), [WORKFLOW.md](docs/WORKFLOW.md), [OPEN-QUESTIONS.md](docs/OPEN-QUESTIONS.md)
```
