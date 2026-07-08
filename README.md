# confirmed-ctl

> QBO bank-transaction sync and newspaper-ad payment confirmation for PERM-Ads.com LLC. Part of the **auto-ctl.io** suite.

confirmed-ctl closes the financial loop on newspaper advertisement purchases. It
syncs bank/expense transactions from **QuickBooks Online (QBO)** into a local
PostgreSQL database, ranks candidate transactions against unconfirmed ads, and
records the human-confirmed relationship between an ad, its bank charge, and its
Gmail confirmation thread.

> **Design of record:** [`docs/ARCHITECTURE-SPEC.md`](docs/ARCHITECTURE-SPEC.md).
> This replaces the earlier Plaid + MySQL/EspoCRM design (Plaid is no longer
> viable); the `docs/DESIGN.md`, `CRM-SCHEMA.md`, and `DROPBOX.md` documents
> describe that superseded approach and are retained for history only.

---

## What it does

```
QBO (Purchase / BillPayment)
    │  confirmed-ctl sync   (CDC + date-query fallback)
    ▼
bank_transactions  ──►  matching/scorer  ──►  ranked candidates in the Flask popup
                                                     │  human clicks CONFIRM
                                                     ▼
                                              ad_confirmations  (ad ↔ txn ↔ gmail thread)
                                                     │
                                                     ▼
                                    receipt-ctl (separate tool) downloads the PDF
```

- **QBO sync** — `qbo/` pulls recent transactions and upserts them into
  `bank_transactions` (deduped by `qbo_id`), logging every run to
  `confirmed_ctl_sync_log`.
- **Matching** — `matching/scorer.py` ranks candidate transactions by amount,
  vendor-name similarity, and date proximity. `matching/rag.py` (ChromaDB) stores
  confirmed matches as pattern memory for future ranking.
- **Gmail** — `gmail/client.py` searches Gmail threads by ad number for the popup.
- **Flask API** — `api/routes.py` exposes the endpoints the existing app's
  "Confirmed-CTL" popup calls (`/sync`, `/candidates/<ad_id>`, `/confirm`, …).

Receipt download/archiving is handled by the separate **receipt-ctl** tool (see
[`docs/RECEIPT-CTL.md`](docs/RECEIPT-CTL.md)); a lightweight in-suite downloader
is also available via `confirmed-ctl receipts`.

---

## CLI

```bash
# Sync recent QBO transactions into the local DB
confirmed-ctl sync --lookback-days 2
confirmed-ctl sync --no-cdc            # use a date query instead of CDC

# Last sync run info
confirmed-ctl status

# Ranked bank-transaction candidates for one ad
confirmed-ctl match --ad-id 1234

# Download receipts for confirmed ads that have a Gmail thread id
confirmed-ctl receipts
```

Daemon mode (hourly sync) runs via `python -m confirmed_ctl.daemon` — see
`confirmed-ctl.service`.

---

## Install & configure

```bash
python -m venv venv && . venv/bin/activate
pip install -e ".[live]"        # runtime; use ".[dev]" for tests/lint
cp .env.example .env            # then fill in QBO + DATABASE_URL + Gmail values

# Create confirmed-ctl's tables in the shared Postgres database
alembic upgrade head
```

Configuration is environment-driven (see `.env.example`): `DATABASE_URL`,
`QBO_CLIENT_ID`/`QBO_CLIENT_SECRET`/`QBO_REALM_ID`/`QBO_TOKEN_PATH`,
`GMAIL_TOKEN_PATH`, `CHROMA_PATH`, `RECEIPTS_BASE_PATH`, `SYNC_INTERVAL_SECONDS`.

confirmed-ctl **owns** `bank_transactions`, `ad_confirmations`, and
`confirmed_ctl_sync_log`. It references the existing CRM table `ad_purchases`
via foreign keys but never creates or migrates it.

---

## Development

```bash
pip install -e ".[dev]"
pytest            # unit tests (scoring logic, QBO client helpers, import smoke)
ruff check confirmed_ctl tests
```

Heavy integrations (Google API, ChromaDB, Flask, psycopg2) are imported lazily,
so the core package, CLI, and test suite import without them installed.

### Project layout

```
confirmed_ctl/
├── cli.py            # Click CLI: sync, status, match, receipts
├── daemon.py         # hourly sync loop (systemd)
├── settings.py       # env-driven configuration
├── qbo/              # OAuth token manager + transaction sync
├── gmail/            # thread search + receipt download
├── matching/         # scorer + ChromaDB RAG
├── db/               # SQLAlchemy models, session, Alembic migrations
└── api/routes.py     # Flask blueprint for the Confirmed-CTL popup
```

---

## Related tools (auto-ctl.io suite)

- [`receipt-ctl`](docs/RECEIPT-CTL.md) — downloads Gmail receipt attachments and
  archives them (reads `ad_confirmations.gmail_thread_id`).
- `qbo-ctl` / `ledger-ctl` (future) — writes accounting categorization back to QBO.

---

## Docs

- [ARCHITECTURE-SPEC.md](docs/ARCHITECTURE-SPEC.md) — **current** full system architecture
- [RECEIPT-CTL.md](docs/RECEIPT-CTL.md) — receipt-ctl standalone specification
- [ROADMAP.md](docs/ROADMAP.md) — phased build plan
- Historical (superseded Plaid design): [DESIGN.md](docs/DESIGN.md), [CRM-SCHEMA.md](docs/CRM-SCHEMA.md), [DROPBOX.md](docs/DROPBOX.md), [WORKFLOW.md](docs/WORKFLOW.md), [OPEN-QUESTIONS.md](docs/OPEN-QUESTIONS.md)
