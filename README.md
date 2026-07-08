# confirmed-ctl

> Automated newspaper ad receipt collection and final payment reconciliation for PERM-Ads.com LLC.

Picks up where `plaid-ctl` leaves off. When a newspaper ad payment is confirmed in the bank, confirmed-ctl:
1. Finds the ad confirmation email in Gmail and downloads the PDF receipt
2. Stores the receipt in Dropbox under the correct folder structure
3. Writes the Gmail URL + Dropbox path back to CRM
4. Marks the case as `Done` (fully reconciled)

**Depends on:** `plaid-ctl` (bank matching), `gmail-ctl` (email search + PDF download)

---

## Commands

```bash
# Process all confirmed cases (full pipeline)
confirmed-ctl process-confirmed

# Dry run — no writes, no file storage
confirmed-ctl process-confirmed --dry-run

# Single case
confirmed-ctl process-confirmed --case 10349

# Expand bank transaction search window (default 48h)
confirmed-ctl process-confirmed --hours 72

# Just Gmail + receipt step (skip Plaid verification)
confirmed-ctl fetch-receipt --case 10349

# Just Plaid verification step
confirmed-ctl verify-payment --case 10349

# Status: all confirmed cases + Gmail/Plaid/Dropbox status
confirmed-ctl status

# Daemon mode
confirmed-ctl watch --interval 30
```

---

## CRM Trigger

```sql
SELECT p.id, p.name, p.adnumbernews, p.pricenewsreal,
       p.dateinvoicednews, p.news_id, n.shortname AS newspaper_short
FROM t_e_s_t_p_e_r_m p
JOIN news n ON p.news_id = n.id
WHERE p.statacctgcreditnews IN ('Confirmed', 'PaymentConfirmed')
  AND p.trxstring IS NULL
  AND p.deleted = 0
ORDER BY p.dateinvoicednews DESC
```

**Trigger:** `statacctgcreditnews` = `Confirmed` or `PaymentConfirmed`, and `trxstring` not yet populated.

---

## Dropbox Path Convention

```
Receipts/Newspapers/{Year}/{YYYY-MM}/{NewspaperShortName}/
  Case-{casenumber}_{Company}_{AdNumber}_{DateInvoiced}.pdf
```

Example:
```
Receipts/Newspapers/2026/2026-03/Miami-Herald/
  Case-10349_Eduexplora_IPR00160880_2026-03-02.pdf
```

---

## CRM Write Policy

Read-only by default. Writes require `--write` flag. Only these fields:

| Field | Written Value |
|-------|--------------|
| `statacctgcreditnews` | `'Done'` |
| `urlgmailadconfirm` | Gmail thread URL |
| `trxstring` | `"{date} \| {txn_name} \| ${amount}"` |
| `datepaidnews` | Plaid transaction settlement date |

No other fields. No schema changes.

---

## Related Tools

- [`plaid-ctl`](https://github.com/k4rlski/plaid-ctl) — upstream: writes `PaymentConfirmed` + `trxstring`
- [`gmail-ctl`](https://github.com/k4rlski/gmail-ctl) — Gmail search + PDF attachment download
- [`receipt-ctl`](https://github.com/k4rlski/receipt-ctl) — vendor invoice downloader (separate domain)
- [ABCF-X Report](https://reports.permtrak.com/abcf-x/) — human-facing view of reconciliation status

---

## Development

Requires Python 3.10+.

```bash
# Build the environment (venv + editable install + dev deps)
make install          # or: python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Configure
cp confirmed-ctl.yml.example confirmed-ctl.yml   # then fill in CRM creds
# (or export CONFIRMED_CTL_CRM_USER / CONFIRMED_CTL_CRM_PASSWORD, etc.)

# Run
.venv/bin/confirmed-ctl --help
.venv/bin/confirmed-ctl status

# Test + lint
make test             # pytest
make lint             # ruff
```

### Project layout

```
src/confirmed_ctl/
  main.py           # Click CLI entry point (confirmed-ctl)
  config.py         # confirmed-ctl.yml loader (+ env overrides)
  models.py         # Case / GmailResult / PlaidResult / CaseReport
  crm_client.py     # CRM MySQL — trigger query + allow-listed writes
  gmail_receipt.py  # gmail-ctl bridge (search + PDF download)
  plaid_verifier.py # plaid-ctl bridge (re-verify transaction)
  dropbox_store.py  # rclone upload + shared link
  pipeline.py       # per-case orchestration (partial-completion table)
tests/              # unit tests for the pure logic
```

> Status: v0.1.0 scaffold. The CLI, config, orchestration, Dropbox path logic,
> and CRM write allow-list are implemented and tested. The live `gmail-ctl`,
> `plaid-ctl`, and `rclone` bridges are stubbed (marked `TODO(phase1)`) pending
> those upstream tools being deployed — see [ROADMAP.md](docs/ROADMAP.md).

## Docs

- [DESIGN.md](docs/DESIGN.md) — architecture, pipeline, module breakdown
- [WORKFLOW.md](docs/WORKFLOW.md) — step-by-step workflow + status transitions
- [CRM-SCHEMA.md](docs/CRM-SCHEMA.md) — CRM fields reference
- [DROPBOX.md](docs/DROPBOX.md) — receipt storage structure
- [ROADMAP.md](docs/ROADMAP.md) — phased build plan + open issues
- [OPEN-QUESTIONS.md](docs/OPEN-QUESTIONS.md) — unresolved design questions
