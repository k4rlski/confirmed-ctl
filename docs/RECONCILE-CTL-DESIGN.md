# reconcile-ctl — Design Doc (final-sweep reconciliation chain)

> **Status:** DESIGN ONLY (no build in this cycle). Captures the intended
> accounting-reconciliation chain and the decisions taken 2026-07-10. `reconcile-ctl`
> **does not exist yet**; this doc lives in `confirmed-ctl` until the repo is bootstrapped.

## 1. Decisions ratified (2026-07-10)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **`reconcile-ctl` = a new standalone repo** (`k4rlski/reconcile-ctl`) when built. Design + tracking issues live in `confirmed-ctl` for now. | Keeps the reconcile layer independent of the ingest tools it consumes. |
| D2 | **Bypass QuickBooks for ingest.** Keep the Gmail/bank pipeline as the source of truth. Optionally accept a **QBO export as ONE reconcile input**. **Defer any write-back.** | QBO API sync was already removed from confirmed-ctl; re-adding a live QBO dependency is out of scope. Import is low-risk and reversible. |
| D3 | **`trx-ctl` stays parallel for now**; `reconcile-ctl` supersedes/absorbs its BofA-CSV↔CRM role later (consolidation tracked as an issue, not done here). | Avoids disturbing a working mars-status module. |
| D4 | **pay-ctl / fin-ctl remain CRM-sourced today.** Feeding them reconciled actuals from confirmed-ctl Postgres is a **future** enhancement, not part of the first reconcile build. | They are live production modules; changing their data source is a separate, gated project. |

## 2. Purpose

A **final-sweep reconciliation** layer: take every bank debit for a period and match it
to a claimed source of truth — ad-buy confirmations (`confirmed-ctl`), vendor invoices
(`vendor-ctl`), and CRM `accounting` — producing a categorized, reconciled ledger with an
explicit **"unclaimed transactions"** exception list. The reconciled output later feeds
tax / payroll / finance views.

## 3. Existence map (current reality)

| Tool | State | Home |
|------|-------|------|
| `confirmed-ctl` | **Live** (FANG Postgres) — BofA email-scan, ad↔txn confirm, Gmail receipts | `core-v5/confirmed-ctl` |
| `vendor-ctl` (née receipt-ctl) | **Scaffold** — vendor-portal invoice scraper, never run E2E | `core-v5/receipt-ctl` |
| `reconcile-ctl` | **Does not exist** — conceptual | (future `k4rlski/reconcile-ctl`) |
| `tax-ctl` | **Stub page only** (`MARS-STATUS/static/tax-ctl.html`) | mars-status |
| `pay-ctl` | **Live** payroll module (DB `pay_ctl` via dbx tunnel) | mars-status |
| `fin-ctl` | **Live** ABCF-X cash cascade at `/ops/fin-ctl` | mars-status |
| `trx-ctl` | **Live** BofA-CSV↔CRM `accounting` triage (parallel) | mars-status |

## 4. Intended data flow

```
[confirmed-ctl]  ad-buy bank charges + Gmail ad receipts   ─┐
  bank_transactions + ad_confirmations + receipt_file_path  │
                                                            ├─► [reconcile-ctl]
[vendor-ctl]     vendor/SaaS invoices (PDF + amount)        │      final sweep:
  Dropbox + state.json (later: ledger row)                 ─┘   join vs ALL bank debits
                                                                 + CRM `accounting`
[optional inputs] BofA statement export | QBO export  ─────────►   │
                                                                   ├─► categorized ledger
                                                                   └─► "unclaimed" exceptions
                                                                        │
        ┌───────────────────────────────────────────────────────────┼───────────────┐
        ▼                                   ▼                          ▼
   tax-ctl (planned)                  pay-ctl (live)              fin-ctl (live)
   P&L / deductions / quarterly       reconciled actuals vs       true cash position;
   estimates; QBO export in           CRM compute_month_profit    feed reconciled
   (no write-back)                    (future wiring)             expenses into cascade (future)
```

## 5. Data model sketch (to be refined at build time)

Reconciliation joins on **amount + date window + vendor/payee**, reusing confirmed-ctl's
existing scorer semantics where possible:

- **Bank side:** `confirmed_ctl.bank_transactions` (already ingested BofA debits) and/or an
  imported BofA statement / QBO export row.
- **Ad-buy claim:** `confirmed_ctl.ad_confirmations` (ad ↔ txn ↔ receipt).
- **Vendor claim:** `vendor-ctl` invoice records (vendor, invoice id, amount, date, PDF ref).
- **CRM claim:** `permtrak2_crm` `accounting` rows (existing vendor-expense triage from trx-ctl).
- **Output:** a `reconciliation` table keyed by bank txn → {claim_type, claim_ref, category,
  status ∈ matched|partial|unclaimed}. Unclaimed rows are the actionable exception queue.

Storage: a shared Postgres schema (likely alongside confirmed-ctl on FANG) so both halves
speak through the DB, not imports. Elasticsearch is **not** required for structured txns
(deferred; revisit only if free-text search over a large receipt archive is needed).

## 6. QuickBooks interplay (per D2)

- **Ingest:** bypass QBO entirely (Gmail alerts + bank remain source of truth).
- **Reconcile input:** a QBO export (CSV/OFX) may be accepted as one more bank/expense source.
- **Write-back:** deferred. A future `qbo-ctl`/`ledger-ctl` could categorize in QBO, but is
  out of scope. `tax-ctl` may re-engage QBO read-only for books; tracked separately.

## 7. Non-goals (this cycle)

- No `reconcile-ctl` repo, code, or DB migration.
- No changes to pay-ctl / fin-ctl / trx-ctl.
- No QBO integration of any kind.
- No tax-ctl backend.

## 8. Open questions (tracked as issues)

Existing coverage: **confirmed-ctl#33** (reconcile pipeline → tax/pay/fin), **#21**
(final-sweep vs BofA statement/QBO), **#22** (Elasticsearch decision — leaning *no* for
structured txns). Open questions still to schedule: reconcile scope (ad-buy only vs all
debits; which entities/accounts); shared ledger schema + home (FANG Postgres?); trx-ctl
consolidation timing; whether pay-ctl/fin-ctl should read reconciled actuals; tax-ctl
bootstrap + canonical RAG; QBO export format. A new focused issue opened alongside this
doc records the **QBO-stance decision** (bypass ingest / import-only / defer write-back).
