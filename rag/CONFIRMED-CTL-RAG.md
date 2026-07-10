# confirmed-ctl RAG Knowledge Base

> **Repo:** https://github.com/k4rlski/confirmed-ctl (PRIVATE)
> **Last updated:** 2026-07-10
> **Status:** 🟢 Deployed service — Postgres-backed reconciliation API on FANG, proxied by MARS (`/adm/confirmed-ctl-adm`). Email-scan ingest + read-only CRM adapter + gated CRM write-back are live; OFX/CSV import adapters are a later gen.
> **Version:** 0.2.0

> **NOTE ON HISTORY:** An earlier RAG (dated 2026-03-19) described a claw + Dropbox + Plaid + MySQL-CRM-write design. That design was superseded. This document reflects the ACTUAL implemented system, read from the code. The historical Plaid/QBO/Dropbox docs (`docs/DESIGN.md`, `docs/CRM-SCHEMA.md`, `docs/DROPBOX.md`, `docs/WORKFLOW.md`, `docs/ARCHITECTURE-SPEC.md`, `docs/OPEN-QUESTIONS.md`, `docs/ROADMAP.md`) are retained in-repo for history only and are NOT authoritative.

---

## 1. Purpose

Close the financial loop on newspaper-advertisement purchases for PERM-Ads.com LLC:
ingest **Bank of America transactions**, rank candidate transactions against
unconfirmed CRM ad-buys, and record the human-confirmed relationship between an
**ad ↔ its bank charge ↔ its Gmail confirmation thread**. On confirmation the tool
(optionally, gated) writes the clearance-done marker + audit fields back to the CRM.

Reconciliation is **operator-driven** through the MARS admin page: the tool surfaces
ranked candidates and Gmail threads; a human clicks CONFIRM. There is no fully
automatic "mark Done" pipeline.

---

## 2. Architecture

```
                         FANG  /opt/confirmed-ctl
   ┌───────────────────────────────────────────────────────────────┐
   │  confirmed_ctl.wsgi:app  (Flask)  ── gunicorn ──┐              │
   │    bearer-token guard (before_request)          │              │
   │    blueprint: /confirmed-ctl/*  +  /healthz      │  systemd:    │
   │                                                  │  confirmed-  │
   │  confirmed_ctl.daemon  (email-scan loop)  ───────┤  ctl-api     │
   │                                                  │  .service    │
   │  confirmed-ctl CLI  (sync / status / ignore …)   │  confirmed-  │
   │                                                  │  ctl.service │
   │  ┌─────────────── Postgres (confirmed_ctl) ────┐ │              │
   │  │  bank_transactions / ad_confirmations /     │ │              │
   │  │  confirmed_ctl_sync_log / ignore_memo_...   │ │              │
   │  └─────────────────────────────────────────────┘ │              │
   │  ChromaDB (PersistentClient, CHROMA_PATH)        │              │
   └───────────────────────────────────────────────────────────────┘
        ▲ bind 127.0.0.1:8787 (localhost only)          ▲ read-only        ▲ read-only
        │ SSH tunnel                                     │ MariaDB CRM       │ Gmail API
   ┌────┴───────────────────────────┐    ┌──────────────┴───────┐   ┌───────┴──────────┐
   │  claw / MARS (mars.auto-ctl.io)│    │  MariaDB permtrak2_crm │   │  Google Gmail    │
   │  /adm/confirmed-ctl-adm page   │    │  t_e_s_t_p_e_r_m + news│   │  service acct +  │
   │  routes/confirmed_ctl_adm_bp.py│    │  READ-ONLY (SELECT) +  │   │  domain-wide     │
   │  proxies /api/confirmed-ctl/*  │    │  1 gated 3-field UPDATE│   │  delegation      │
   └────────────────────────────────┘    └────────────────────────┘   └──────────────────┘
```

**Key architectural facts (from code):**

- The service is a **Flask app** (`confirmed_ctl/wsgi.py`, `create_app()`), served by
  **gunicorn** (`confirmed-ctl-api.service`, `-w 2 --timeout 60`), binding
  **`127.0.0.1:8787`** (localhost only, `CONFIRMED_CTL_API_BIND`). It is never exposed
  publicly — **claw/MARS reaches it over an SSH tunnel**.
- **Auth:** every request carries a bearer token (`CONFIRMED_CTL_API_TOKEN`), checked in
  a `before_request` guard using `hmac.compare_digest`. `/healthz` is always exempt.
  Fail-open when the token is unset (dev/test, with a loud warning) unless
  `CONFIRMED_CTL_REQUIRE_AUTH=1` forces fail-closed (503).
- **MARS proxy:** the operator UI lives on claw/MARS as `static/confirmed-ctl-adm.html`
  with a Flask blueprint `routes/confirmed_ctl_adm_bp.py` that proxies
  `/api/confirmed-ctl/*` through the tunnel to the fang API. The admin page is at
  MARS `/adm/confirmed-ctl-adm`.
- **Cross-DB reality (locked data architecture):** BofA transaction data lives in the
  **standalone confirmed_ctl Postgres on FANG** because the MariaDB CRM schema
  (`t_e_s_t_p_e_r_m`) **cannot be altered**. A CRM ad is therefore referenced *logically*
  from Postgres via plain indexed columns (`ad_crm_id` = EspoCRM record id,
  `ad_number` = `adnumbernews`) with **NO foreign key**. There is no `ad_purchases`
  Postgres table; ad/case data is read live from the CRM.

---

## 3. Data model

### 3.1 Postgres (owned by confirmed-ctl, on FANG)

Alembic head: **`0003_ignore_memo_patterns`** (0001_initial → 0002_add_synclog_source
→ 0003_ignore_memo_patterns).

**`bank_transactions`** — one ingested BofA transaction.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `source` | varchar(50) NOT NULL | ingestion adapter; CHECK IN (`email-scan`, `export-ofx`, `export-csv`) |
| `source_txn_id` | varchar(100) NOT NULL | source's stable id; `UNIQUE(source, source_txn_id)` → idempotent re-ingest |
| `txn_date` | date NOT NULL | posting date |
| `created_time` / `updated_time` | timestamptz | (source-provided, largely unused by email-scan) |
| `total_amount` | numeric(10,2) NOT NULL | **SIGNED** — debits/withdrawals stored NEGATIVE |
| `payment_type` | varchar(20) | e.g. `PURCH W/O PIN`, `ELEC DRAFT (ACH)` |
| `payment_ref_num` | varchar(100) | card/account last4 |
| `private_note` | text | e.g. `BofA alert (SCHEMA-CARD)` |
| `doc_number` | varchar(100) | |
| `vendor_id` / `vendor_name` | varchar | merchant string (vendor_name = cleaned merchant) |
| `account_id` / `account_name` | varchar | |
| `line_descriptions` | text[] | |
| `raw_json` | JSONB | full parse blob (`message_id`, `schema`, `merchant`, `last4`, `posted_date`, `merchant_raw`, …) |
| `confirmed_ad_crm_id` | varchar(50) indexed | **logical** pointer at the confirmed CRM ad; **NO FK**. NULL == unmatched |
| `confirmed_at` | timestamptz | set at /confirm |
| `created_in_db` | timestamptz default now() | |
| `ignored` | bool NOT NULL default false | SAAS/vendor-charge flag (never surfaces as a candidate) |
| `ignore_reason` | text | `ignore_pattern:<pattern>` |

Indexes: `idx_bank_txn_date`, `idx_bank_txn_vendor`, `idx_bank_txn_amount`,
`idx_bank_txn_confirmed`, and partial `idx_bank_txn_unmatched_date` on `txn_date`
`WHERE confirmed_ad_crm_id IS NULL` (accelerates the unmatched-candidate query).

**`ad_confirmations`** — the ad ↔ bank-txn ↔ Gmail-thread relationship record.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `ad_crm_id` | varchar(50) NOT NULL | logical CRM ad id (EspoCRM record id); `UNIQUE` → one confirmation per ad |
| `ad_number` | varchar(100) | CRM `adnumbernews`; indexed |
| `bank_txn_id` | int FK → `bank_transactions.id` | genuine same-DB FK (kept) |
| `gmail_thread_id` | varchar(255) | |
| `gmail_message_id` | varchar(255) | |
| `gmail_subject` | text | |
| `receipt_file_path` | text | primary saved receipt path (in-suite downloader) |
| `receipt_url` | text | comma-joined all saved paths |
| `confirmed_by` | varchar(100) | |
| `confirmed_at` | timestamptz default now() | |
| `match_confidence` | varchar(10) | `manual` (from /confirm) |
| `match_method` | varchar(50) | `manual` |
| `notes` | text | |

**`confirmed_ctl_sync_log`** — audit row per ingestion run: `source`, `synced_at`,
`lookback_days`, `txns_fetched`, `txns_new`, `txns_updated`, `auto_matched`, `errors`,
`duration_ms`.

**`ignore_memo_patterns`** — DB-tracked SAAS/vendor ignore-strings: `pattern` (short
stable substring), `label`, `active`, `created_at`. Case-insensitive functional index
on `lower(pattern)`. Seeded defaults: `THEAGOGE`, `FIREWORKS.AI`, `INTUIT *QBOOKS`
(plain literal substring match — the asterisk is matched verbatim).

### 3.2 CRM read model — `CrmAd` (NOT a Postgres table)

Ad/case data is read live from MariaDB into an in-process `CrmAd` dataclass (see
`db/models.py`). Fields hydrated from `t_e_s_t_p_e_r_m` + `news` (the ABCF-X column set):

`crm_id` (`t_e_s_t_p_e_r_m.id`), `ad_number` (`adnumbernews`, trailing space stripped),
`client_name` (`name`), `newspaper_name` (`news.name`), `run_date` (`datenewsstart`),
`run_end` (`datenewsend`), `expected_charge_date` (`datebuynews` → falls back to
`datenewsstart`), `buy_date` (`datebuynews`), `expected_amount` (`pricenewsreal`),
`case_number` (`casenumber`), `state` (`jobsitestate`), `attorney` (`attyname`),
`entity` (`entity`), `job_title` (`jobtitle`), `beneficiary_last` (`beneficiarylast`),
`approved_date` (`adsapproveddate`), `owner` (`news.owner`), `status_news` (raw
`statnews` enum string, e.g. `'["Active"]'`), `clearance_status` (raw
`statclearancenews` enum string, e.g. `'["Confirmed"]'`). EspoCRM enums are stored as
JSON arrays; `status_news`/`clearance_status` are passed through as-is (helper
`parse_enum` extracts the first element where needed).

---

## 4. CRM adapter (`crm/client.py`) — READ-ONLY MySQL + one gated write

Adapter into the MariaDB CRM `permtrak2_crm` (`t_e_s_t_p_e_r_m` PERM cases JOIN `news`).
`pymysql` with `DictCursor`, `connect_timeout=10`, `read_timeout=60`. Host/creds are
env-driven (`CRM_DB_HOST`/`_USER`/`_PASS`/`_NAME`/`_PORT`, default port 3306). When
`CRM_DB_HOST` is blank the adapter is "not configured" and CRM-dependent endpoints
return 503.

**CRM host:** env-configured. Repo docs reference `permtrak.com:3306`; ops has reported
the fang deployment reaching the CRM on `ra.akhet.org` (the cPanel Remote-MySQL host
that must allowlist fang's IP and grant a least-privilege read-only user). Treat the
exact production host as whatever `CRM_DB_HOST` is set to on fang — **[TBD: confirm the
live `CRM_DB_HOST` value on fang]**.

**Read queries (verbatim ABCF-X SELECT/JOIN, only WHERE differs):**

- `list_clearances()` — the candidate ad queue. WHERE `statnews='["Active"]'` AND
  `(entity='JKT' OR entity='PA')` AND `statclearancenews='["Confirmed"]'` AND
  `deleted=0` AND `statpermcase='["Active Case"]'`, ORDER BY `datebuynews DESC`. (This
  mirrors the canonical mars-status ABCF-X clearances query.)
- `list_reconciled()` — identical WHERE except `statclearancenews='["Done"]'` (ads this
  tool has already marked Done).
- `get_ad(ad_crm_id)` — same SELECT, `WHERE id=%s AND deleted=0` (parameterized).

**Write-back — the ONLY non-SELECT the package issues (`update_ad_clearance`):**

- **Feature-gated:** raises `CrmWriteDisabled` unless `CONFIRMED_CTL_CRM_WRITE=true`
  (default false; set true ONLY on fang). Dev/test never touch the live CRM.
- **Strict 3-field allowlist, hardcoded:** a single parameterized
  `UPDATE t_e_s_t_p_e_r_m SET statclearancenews=%s, trxstring=%s, urlgmailadconfirm=%s
  WHERE id=%s`. `statclearancenews` is bound to the literal `'["Done"]'`. No dynamic /
  caller-supplied column names; every value bound via `%s`. **The staff-owned
  `datepaidnews` is intentionally NEVER written** (this corrects the legacy 4-field
  design).
- **Verified match:** connection opened with `CLIENT.FOUND_ROWS` so `rowcount` reflects
  MATCHED (not CHANGED) rows → a re-write of identical values still counts as 1 (safe,
  idempotent); `rowcount==0` unambiguously means "no such id" and raises `CrmWriteError`
  so the caller does not persist a confirmation for a write that never landed.

---

## 5. API endpoints (`api/routes.py`, blueprint prefix `/confirmed-ctl`)

All require the bearer token (via wsgi guard) except `/healthz` (defined in wsgi).

| Method / path | Purpose | Notable response keys |
|---------------|---------|-----------------------|
| `GET /healthz` | liveness (wsgi, auth-exempt) | `{status:"ok"}` |
| `POST /sync` | Sync-Now button | **501 stub** `{status:"not_implemented", detail, lookback_days}` (OFX/CSV adapters not wired; QBO removed) |
| `GET /sync/status` | last sync run | `last_sync`, `txns_fetched`, `txns_new`, `auto_matched`, `errors` |
| `GET /candidates/<ad_crm_id>` | ranked candidates + Gmail threads for one ad | `ad{…}`, `bank_candidates[]`, `gmail_threads[]`, `gmail_error`, `gmail_note`, `excluded[]`. 503 crm_not_configured / 502 crm_unavailable / 404 not_found |
| `POST /confirm` | save the confirmation (+ gated CRM write-back) | `status:"confirmed"`, `confirmation_id`, `ad_crm_id`, `ad_number`, `txn_source_txn_id`, `gmail_thread_id`, `crm_write` (`written`/`disabled`/`skipped_no_txn`), `crm_values{statclearancenews,trxstring,urlgmailadconfirm}`. 409 already confirmed; 400 missing ad_crm_id; 502 crm_write_failed; 500 postgres_commit_failed_after_crm_write |
| `GET /unconfirmed` | candidate ads not yet confirmed | `count`, `ads[]` (full ABCF-X CrmAd fields) — CRM `list_clearances` minus ids already in `ad_confirmations` |
| `GET /reconciled` | ads reconciled by THIS tool (Done in CRM AND have a local confirmation) | `count`, `ads[]` each with CrmAd fields + `bank_txn_id`, `bank_amount`, `bank_txn_date`, `gmail_thread_id`, `gmail_url`, `confirmed_at` (DESC) |
| `GET /bank-transaction/<txn_id>` | read-only Bank-Trx modal detail | `txn_id`, `amount` (raw signed), `vendor_name`, `merchant_raw`/`merchant` (from raw_json), `line_descriptions`, `txn_date`, `posted_date`, `source`, `source_txn_id`, `ignored`, `ignore_reason`, `confirmed_at`, `created_at`, `confirmed_ad_crm_id`, `related{crm_id,case_number,client_name,ad_number,newspaper_name}` or `related=null` (+ `related_error:"crm_unavailable"` if CRM lookup fails — modal always renders). 404 not_found |

**`bank_candidates[]`** items: `txn_id`, `source`, `source_txn_id`, `txn_date`,
`amount`, `vendor_name`, `account_name`, `payment_ref`, `memo`, `score`, `score_pct`.

**CONSUMED-EXCLUSION INVARIANT:** candidates only include UNCONSUMED
(`confirmed_ad_crm_id IS NULL`) and non-`ignored` rows. Once a txn is mapped to an ad
(or flagged SAAS/vendor) it can never resurface as a candidate for another ad.

**`/confirm` write ordering:** when gated on and a bank txn is matched, the CRM UPDATE
is issued BEFORE the Postgres commit, so a CRM failure leaves nothing persisted and the
confirm is cleanly retryable. If the CRM write succeeds but the Postgres commit then
fails, it logs CRITICAL and returns 500 (retry is idempotent/self-healing).

---

## 6. Ingest pipeline — BofA transaction-alert email scan (`ingest/email_scan.py`)

Reads (read-only) the impersonated Gmail mailbox and parses **Bank of America
transaction alerts** (all from `onlinebanking@ealerts.bankofamerica.com`) into
`bank_transactions` rows (`source='email-scan'`).

- **Query:** one broad, date-bounded SENDER query
  `from:onlinebanking@ealerts.bankofamerica.com after:<epoch>` (epoch seconds), NOT
  brittle `subject:"…"` phrase queries. Each returned message is then classified by a
  case-insensitive **substring of its raw Subject** against a routing table.
- **Schemas** (each has a current + older-backfill subject; per-schema label→value map):
  - `SCHEMA-CARD` — debit/ATM card over-limit (Amount, card ending, Merchant,
    Transaction type, Date). Highest value for ad matching. (Real .eml fixture.)
  - `SCHEMA-ACH-WITHDRAWAL` — ACH withdrawal over-limit (Amount, Type, Account
    nickname+last4, Merchant, Transaction date). (Real .eml fixture.)
  - `SCHEMA-TRANSFER` — online transfer over-limit (Account, Amount, date; no
    merchant/type). `# REFINE` — no raw HTML fixture yet.
  - `SCHEMA-DEBITCARD-USED` — debit card used online/phone/mail; **single OR batched**
    (an Account/Amount/Made-at/On block repeats per txn). `# REFINE` — no raw HTML
    fixture yet.
- **Parsing:** alerts are **HTML-only**; parsed with BeautifulSoup by **pairing the two
  adjacent table cells** (label `<td>` → value `<td>`), NOT a flattened text render.
  Whitespace collapsed. Amounts, last4 (`ending in …`), and dates extracted by tolerant
  regexes. **Amount stored NEGATIVE** (debit/withdrawal). Fail-closed: a row with no
  amount or no resolvable date is skipped, never fabricated.
- **Idempotency:** `source_txn_id` = the Gmail `message_id` for a single-transaction
  alert; `f"{message_id}:{block_index}"` per line item of a batched alert (see
  `ingest/dedup.py::email_scan_source_txn_id`). Re-scans collapse on
  `uq_bank_transactions_source_txn` (insert-conflict → SKIP; per-row SAVEPOINT catches
  races). **Note: this is message-id based for email-scan — NOT the natural-key SHA-256
  hash, which is used ONLY for `export-csv`.**
- **Ignore flags:** active `ignore_memo_patterns` are loaded once per run and applied per
  row at ingest — a match sets `ignored=true` + `ignore_reason` (flag, never delete).
- **SyncLog:** every run records counts + duration in `confirmed_ctl_sync_log`.
- **Mailbox / where alerts live:** default `GMAIL_IMPERSONATE=karl@perm-ads.com` — it
  keeps every BofA alert in its DURABLE INBOX and also receives Paul's `info@` vendor
  ad-confirmation emails (searched by `adnumbernews`). `info@perm-ads.com` is only the
  delivery address (a filter auto-trashes alerts there; Gmail purges Trash ~30 days, so
  scanning `info@` would need a DAILY run). The Gmail client lists with
  `includeSpamTrash=True` defensively regardless of mailbox. Gmail settings are never
  modified.

**`source_txn_id` conventions (`ingest/dedup.py`):** OFX → statement `<FITID>`;
email-scan → message-id (± `:block_index`); CSV → hex SHA-256 of normalized natural key
`(source, posted_date, amount, description, last4)` + optional per-row disambiguator
(`line_index`/`balance`) so distinct same-day/same-amount CSV rows don't collapse.

---

## 7. Gmail integration (`gmail/client.py`, `gmail/receipts.py`)

**Auth:** Google **service account** with domain-wide delegation. Loads the
service-account JSON key at `GMAIL_TOKEN_PATH`
(default `/opt/confirmed-ctl/secrets/google-service-account.json`) and impersonates
`GMAIL_IMPERSONATE` via `.with_subject()`. Only scope requested is
`gmail.readonly` — the client NEVER modifies/trashes/deletes. Google libs imported
lazily so the package imports without them.

**Thread search — `search_threads_by_ad_number(ad_number, newspaper_name, charge_date)`**
(used by `/candidates`):
- Primary clause is the exact-string ad number (`"<ad_number>"`).
- If both `newspaper_name` AND a parseable `charge_date` are present, a **date-windowed
  paper-name fallback** is also searched (`after: charge-14d`, `before: charge+7d`) so
  receipts that omit the ad number still surface; without a charge date the unbounded
  paper-name clause is skipped (would flood the popup).
- Results merged/de-duped by `thread_id`, ranked ad#-matched-first then paper-name-only,
  capped at `max_results` (default 8). Each summary: `thread_id`, `subject`, `from`,
  `date`, `snippet`, `message_count`, `gmail_url`, `matched_by` (`ad_number`/`paper_name`).
- Raises `ValueError` on a blank ad number (distinguishes "no ad number on record" from
  "searched, found nothing"). `includeSpamTrash=True` throughout.

**`_build_gmail_url(ad_number, gmail_thread_id)`** (in `api/routes.py`) — the CRM
`urlgmailadconfirm` deep link:
`https://mail.google.com/mail/?authuser={GMAIL_IMPERSONATE}#all/{gmail_thread_id}`.
The `?authuser=<email>` + `#all/<thread_id>` form opens the exact thread regardless of
which Google account slot (`/u/0`, `/u/1`, …) the viewer is signed into — the old
`/u/1/#search/{adnum}` form broke when the account index differed. `ad_number` is kept
in the signature for backward compatibility but is no longer part of the URL. Returns
`""` when no thread id was selected.

**Receipt/attachment download (`gmail/receipts.py`) — receipt-ctl / ad-buy half, built
IN-SUITE (confirmed-ctl#27).** Pulls receipt PDFs from the AD-CONFIRMATION Gmail thread of
CONFIRMED ads only. Read-only against Gmail; the BofA alert thread is NEVER touched. The
vendor-portal invoice-scraper half stays in the standalone `receipt-ctl` repo
(k4rlski/receipt-ctl#2), to graduate this module later.

- **Classifier `classify_attachment(filename, mime, subject, body, *, require_keyword)`**
  returns `(accepted, reason)` with strict ordering: (1) non-PDF → `not_pdf`; (2) a
  denylisted FILENAME (invoice/proof/tearsheet/statement/order/campaign…) → `denylist`
  (filename always wins, so `invoice receipt.pdf` is still an invoice); (3) a
  receipt-keyword FILENAME (`receipt.pdf`) → `receipt_keyword` (accepted even when the
  subject mentions an invoice — common mixed threads); (4) else a denylisted
  subject/body → `denylist_context`; (5) loose mode → `pdf_not_denylisted`; (6) else a
  receipt keyword in subject/body → `receipt_keyword`, otherwise `no_receipt_keyword`.
  Receipt keywords use letter-boundary matching so `unpaid` does NOT satisfy `paid`.
- **`_walk_parts`** recurses multipart messages in document order; **SHA-256 dedup**
  against files already in the target dir; `_dedupe_name` avoids on-disk collisions.
- **`scan_thread` / `download_thread_receipts`** return `{saved, present, would_download,
  skipped, scanned}`. `present` = accepted receipts already on disk (recovers ads whose
  file was written by a prior crashed run before the DB commit); `would_download` = names
  accepted in `--dry-run` (nothing written).
- **`process_pending_receipts(db, *, ad_crm_id=None, require_keyword=True, dry_run=False)`**
  — scope = `ad_confirmations` with `gmail_thread_id` set AND `receipt_file_path` NULL
  (optionally one `ad_crm_id`). Writes `receipt_file_path` (first PDF) and always mirrors
  the on-disk path(s) into `receipt_url` (comma-joined when a thread yields multiple).
- **Storage:** `RECEIPTS_BASE_PATH/{year}/{month}/{ad_number}/` — on FANG this is
  `/var/lib/confirmed-ctl/receipts` (owned `auto-ops:auto-ops`); `/mnt/receipts` was a
  never-provisioned placeholder and is NOT used.
- **CLI:** `confirmed-ctl receipt gmail-scan` (dry-run classify), `download-receipts`
  (`--ad-crm-id`, `--loose`, `--dry-run`), `xfer-receipts` (Dropbox transfer — stub,
  confirmed-ctl#31). Legacy `confirmed-ctl receipts` remains a back-compat alias.
- **API/UI:** `/reconciled` rows carry `has_receipt` + `receipt_file_path`; MARS
  `/adm/confirmed-ctl-adm` shows a **Receipt** column (PDF-present badge) and there is a
  `/adm/receipt-ctl-adm` scaffold. A web-served download endpoint is a follow-up
  (confirmed-ctl#30). First prod run: 12 PDFs across 10 reconciled ads (2026-07-10).

---

## 8. Matching / scoring (`matching/scorer.py`, `matching/rag.py`)

`get_candidate_transactions(db, ad, …)` ranks UNMATCHED, non-`ignored`
`bank_transactions` inside a date window `[charge_date - lookback, charge_date +
lookahead]` (charge_date = `expected_charge_date` or `run_date`). Window defaults 10/10
days (`CONFIRMED_CTL_MATCH_LOOKBACK_DAYS` / `_LOOKAHEAD_DAYS`). Score = 0.50·amount +
0.30·vendor + 0.20·date; min threshold 0.10; top 8.

- **Amount (CC-fee aware):** newspapers billed to a card post the invoice grossed up by
  a **3.99% processing fee** (`CC_FEE_MULTIPLIER = 1.0399`), so the txn magnitude is
  matched against BOTH `expected` and `expected × 1.0399`; within $1 or 0.5% is a strong
  match (0.95–1.0), else graduated buckets (1% / 5% / 15%). Debits stored negative match
  by magnitude. `expected` may be NULL (contributes 0).
- **Vendor:** substring both ways → 1.0; `KNOWN_MAPPINGS` alias table (LA Times, Miami
  Herald, Sun Sentinel, Chicago Tribune, NYT, Houston Chronicle) → 0.90; else difflib
  ratio (>0.5).
- **Date:** 0-day 1.0, 1-day 0.85, 2-day 0.65, 3-day 0.40, ≤5-day 0.20.

`get_excluded_transactions(db, ad, …)` surfaces up to 10 near-miss txns (plausible by
CC-fee amount) EXCLUDED from candidates as `out_of_window` or `already_matched`, scanning
a bounded outer window (candidate window + 60 days). `ignored` rows never appear.

`matching/rag.py` — ChromaDB `PersistentClient(path=CHROMA_PATH)`, collection
`confirmed_matches`. `store_confirmed_match(...)` embeds each confirmed match (best-effort,
never blocks /confirm); `retrieve_similar_patterns(...)` fetches similar prior matches.
Chroma imported lazily.

---

## 9. CLI reference (`confirmed_ctl/cli.py`, entrypoint `confirmed-ctl`)

```bash
confirmed-ctl sync [--lookback-days 2]   # run the BofA email-scan ingest (run_email_scan);
                                         #   inserts idempotently + writes a SyncLog
confirmed-ctl status                     # last sync run (synced_at, fetched, new)
confirmed-ctl receipt gmail-scan         # DRY-RUN: classify ad-confirm-thread attachments
                                         #   for confirmed ads missing a receipt (no writes)
confirmed-ctl receipt download-receipts  # download accepted receipt PDFs -> RECEIPTS_BASE_PATH;
    [--ad-crm-id ID] [--loose] [--dry-run]  #   sets receipt_file_path/receipt_url
confirmed-ctl receipt xfer-receipts      # STUB: Dropbox case-tree transfer (confirmed-ctl#31)
confirmed-ctl receipts                   # back-compat alias for process_pending_receipts
confirmed-ctl match --ad-crm-id <id>     # STUB: exits non-zero — live CRM ad hydration in CLI
                                         #   is a later gen (the API path uses the CRM adapter)

# Ignore-string management (flag SAAS/vendor charges; flagged, never deleted):
confirmed-ctl ignore seed                # seed default patterns (idempotent)
confirmed-ctl ignore add <PATTERN> [--label ...]
confirmed-ctl ignore list
confirmed-ctl ignore backfill            # flag existing rows matching an active pattern
```

> NOTE: the README's "sync is a stub" line is stale — the **CLI `sync` command actually
> runs the email-scan adapter** (`run_email_scan`). What IS still a stub: the HTTP
> `POST /confirmed-ctl/sync` endpoint (501) and the `match` CLI subcommand. The daemon
> (`confirmed_ctl/daemon.py`, `confirmed-ctl.service`) runs the email-scan pass each
> `SYNC_INTERVAL_SECONDS` (default 3600), lookback `EMAIL_SCAN_LOOKBACK_DAYS` (default 2).

---

## 10. Deployment (FANG `/opt/confirmed-ctl`)

> **This CORRECTS the stale claw + Dropbox + cron/`/opt/auto-cmd/confirmed-ctl` section
> of the old RAG.** confirmed-ctl is NOT on claw and does NOT use Dropbox/rclone/Plaid.

- **Host / path:** FANG, `/opt/confirmed-ctl` (venv at `/opt/confirmed-ctl/venv`, user
  `auto-ops`, `EnvironmentFile=/opt/confirmed-ctl/.env`).
- **systemd units (in repo):**
  - `confirmed-ctl-api.service` — HTTP API:
    `gunicorn -b ${CONFIRMED_CTL_API_BIND} -w 2 --timeout 60 --access-logfile - confirmed_ctl.wsgi:app`
    (default bind `127.0.0.1:8787`). After `postgresql.service`.
  - `confirmed-ctl.service` — ingestion daemon: `python -m confirmed_ctl.daemon`.
- **Networking:** localhost-only bind; **claw/MARS reaches the API over an SSH tunnel**.
  MARS admin page `/adm/confirmed-ctl-adm` (claw `static/confirmed-ctl-adm.html` +
  `routes/confirmed_ctl_adm_bp.py`) proxies `/api/confirmed-ctl/*` to fang.
- **Postgres:** the standalone `confirmed_ctl` database on fang. Apply migrations with
  `alembic upgrade head` (head `0004_bank_txn_bofa_thread`, adds
  `bank_transactions.bofa_gmail_thread_id`).
- **Config (`.env` / `settings.py`):** `DATABASE_URL`; CRM `CRM_DB_HOST/USER/PASS/NAME/PORT`;
  `CONFIRMED_CTL_CRM_WRITE` (true only on fang); `GMAIL_TOKEN_PATH`, `GMAIL_IMPERSONATE`;
  `EMAIL_SCAN_LOOKBACK_DAYS`, `MATCH_LOOKBACK_DAYS`/`_LOOKAHEAD_DAYS`;
  `RECEIPTS_BASE_PATH` (fang `/var/lib/confirmed-ctl/receipts`; code default
  `/var/lib/confirmed-ctl/receipts`); `CHROMA_PATH`; `SYNC_INTERVAL_SECONDS`;
  `CONFIRMED_CTL_API_TOKEN`, `CONFIRMED_CTL_REQUIRE_AUTH`, `CONFIRMED_CTL_API_BIND`.
- **Packaging:** `pyproject.toml` v0.2.0. Core deps: click, python-dotenv, requests,
  SQLAlchemy, alembic. `[live]` extras: psycopg2-binary, google-auth(+oauthlib),
  google-api-python-client, flask, gunicorn, chromadb. `[dev]`: pytest, ruff.
- **[TBD: confirm the exact live `CRM_DB_HOST` on fang (docs say `permtrak.com`; ops
  reports `ra.akhet.org`), and whether the confirmed_ctl Postgres is provisioned on
  fang or elsewhere — the README's "Postgres not yet provisioned" note is likely stale.]**

---

## 11. Status transitions (CRM, ACTUAL)

The implemented tool keys on **`statclearancenews`** (the ABCF-X clearance field), not
the legacy `statacctgcreditnews`:

```
[ ... upstream sets statclearancenews = ["Confirmed"] on active JKT/PA cases ... ]
        │
        ▼
statclearancenews = ["Confirmed"]        ← confirmed-ctl "unconfirmed/candidate" queue
   (statnews=["Active"], entity JKT/PA, deleted=0, statpermcase=["Active Case"])
        │  operator confirms in MARS  →  /confirm  (gated CRM write-back)
        ▼
statclearancenews = ["Done"]  + trxstring + urlgmailadconfirm   ← reconciled by this tool
```

Legacy note (corrected): earlier docs described writing `statacctgcreditnews='Done'` +
`datepaidnews` and a Plaid verification step. The live tool writes **only**
`statclearancenews='["Done"]'`, `trxstring`, `urlgmailadconfirm`; **`datepaidnews` is
never written** and there is **no Plaid integration**. `trxstring` is built from the
matched bank txn (`{payment_type} {vendor_name} ON MM/DD {Debit|Credit}\t{signed amount}`).

---

## 12. Related tools

- **plaid-ctl** — the earlier design had plaid-ctl feed "PaymentConfirmed" cases; the
  implemented confirmed-ctl does **not** depend on Plaid (ingestion is BofA email-scan;
  OFX/CSV import is a later gen). Historical dependency only.
- **receipt-ctl** — standalone Gmail-receipt downloader/archiver (dedup, cloud storage,
  `receipt_files`/`receipt_ctl_log` tables). Reads `ad_confirmations.gmail_thread_id`
  written by confirmed-ctl; the two speak only through the shared DB. confirmed-ctl also
  ships a lightweight in-suite downloader (`confirmed-ctl receipts`).
- **vendor-ctl** — vendor-invoice downloader for a different domain (vendors, not
  newspaper ads); complementary, not in this data path.
- **mars-status (MARS)** — hosts the operator UI (`/adm/confirmed-ctl-adm`) and proxies
  the fang API; the ABCF-X clearances query in `crm/client.py` mirrors the canonical
  mars-status reports query. See `MARS-CTL-RAG.md`.
- **gmail-ctl** — the former separate Gmail search/receipt concern is now implemented
  **in-place** here (`gmail/client.py` + `gmail/receipts.py`, service-account read-only).
- **abcf-x report** — human-facing view at `reports.permtrak.com` reading
  `statclearancenews` / `urlgmailadconfirm` / `trxstring`.

---

## 13. Facts marked TBD

- Exact live `CRM_DB_HOST` on fang (docs say `permtrak.com:3306`; ops reports
  `ra.akhet.org`).
- Whether the confirmed_ctl Postgres is provisioned/live on fang (README's
  "not yet provisioned" note is likely stale given the deployed API + daemon).
- OFX/CSV import adapters (`export-ofx` / `export-csv` sources) exist in the schema/dedup
  contract but the ingestion adapters themselves are not yet implemented (later gen); the
  HTTP `POST /sync` endpoint and the `match` CLI subcommand remain stubs.
- `SCHEMA-TRANSFER` and `SCHEMA-DEBITCARD-USED` email parsers are `# REFINE` — no raw
  HTML fixture captured yet (structure assumed from older text layout).
