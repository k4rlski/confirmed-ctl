# confirmed-ctl — Design Document

## Purpose

confirmed-ctl is the **final stage** of the newspaper ad payment reconciliation pipeline. It handles cases that have already been matched by plaid-ctl (payment confirmed in bank) and completes the audit trail:

- Gmail receipt PDF download
- Dropbox archiving
- CRM final status write (`Done`)

It replaces the manual workflow where Karl's daughter would log into Gmail, find the newspaper confirmation email, download the PDF, and file it.

---

## Position in Pipeline

```
[Invoiced]
    │
    │ plaid-ctl — bank transaction match
    ▼
[PaymentConfirmed] + trxstring + statclearancenews=Cleared
    │
    │ confirmed-ctl — receipt collection + final write
    ▼
[Done] + urlgmailadconfirm + datepaidnews + Dropbox PDF
```

confirmed-ctl polls CRM for cases in `PaymentConfirmed` (or `Confirmed`) state where `trxstring IS NULL`, meaning plaid-ctl has matched the bank charge but receipt collection hasn't run yet.

---

## Architecture

```
claw.auto-ctl.io (or cron on rodan)
  confirmed-ctl process-confirmed
      │
      ├─ CRM query: statacctgcreditnews IN ('Confirmed', 'PaymentConfirmed')
      │              AND trxstring IS NULL
      │
      ├─ For each case:
      │   ├─ Gmail search (gmail-ctl)
      │   │     → Search auto-ctl@perm-ads.com + info@perm-ads.com
      │   │     → Query: adnumbernews (e.g. IPR00160880)
      │   │     → Download PDF attachment
      │   │
      │   ├─ Plaid verify (plaid-ctl)
      │   │     → Confirm transaction exists + amount matches
      │   │     → Get settlement date
      │   │
      │   ├─ Dropbox storage (rclone)
      │   │     → Receipts/Newspapers/{Year}/{YYYY-MM}/{NewspaperShortName}/
      │   │     → Case-{#}_{Company}_{AdNumber}_{Date}.pdf
      │   │     → Generate Dropbox shared link
      │   │
      │   └─ CRM write (--write only)
      │         → statacctgcreditnews = 'Done'
      │         → urlgmailadconfirm = Gmail thread URL
      │         → trxstring = Plaid transaction string
      │         → datepaidnews = Plaid settlement date
      │
      └─ Summary report
```

---

## Module Breakdown

| Module | Responsibility |
|--------|---------------|
| `main.py` | Click CLI — commands: `process-confirmed`, `fetch-receipt`, `verify-payment`, `status`, `watch` |
| `crm_client.py` | CRM MySQL — query confirmed cases, approved writes |
| `gmail_receipt.py` | gmail-ctl integration — search by ad number, download PDF attachment |
| `plaid_verifier.py` | plaid-ctl integration — re-verify transaction, get settlement date |
| `dropbox_store.py` | rclone wrapper — save PDF, generate shared link |
| `pipeline.py` | Orchestrate per-case: Gmail → Plaid verify → Dropbox → CRM |
| `config.py` | Load `confirmed-ctl.yml`, credential paths |

---

## Gmail Receipt Collection

For each confirmed case:
1. Search `auto-ctl@perm-ads.com` and `info@perm-ads.com` for `adnumbernews`
2. Ad confirmation emails come from newspaper billing departments
3. Subject patterns: `"Ad Confirmation"`, `"Invoice"`, `"Ad #IPR..."`, `"Your Ad Order"`
4. Download PDF attachment (if present) — e.g. `IPR00160880.pdf`
5. If no PDF attachment: store Gmail thread URL only (manual review flagged)

---

## Dropbox Storage

Uses rclone (same setup as receipt-ctl). Path structure:

```
Receipts/Newspapers/{Year}/{YYYY-MM}/{NewspaperShortName}/
  Case-{casenumber}_{CompanySlug}_{AdNumber}_{DateInvoiced}.pdf
```

- `NewspaperShortName` from `news.shortname` in CRM
- `CompanySlug` = company name, spaces→hyphens, max 30 chars
- `DateInvoiced` = `dateinvoicednews` formatted as `YYYY-MM-DD`

Examples:
```
Receipts/Newspapers/2026/2026-03/Miami-Herald/
  Case-10349_Eduexplora_IPR00160880_2026-03-02.pdf
  Case-10531_Martorolls-Office-Group_IPR0015496_2026-02-19.pdf

Receipts/Newspapers/2026/2026-02/Charlotte-Observer/
  Case-10551_DataCloudTek_IPR00159980_2026-02-25.pdf
```

After upload, generate a Dropbox shared link and store it (future use — not a CRM field yet).

---

## Partial Completion Handling

Not every run will have both Gmail + Plaid. The pipeline handles partial states:

| Gmail found | Plaid verified | Action |
|-------------|---------------|--------|
| ✅ PDF found | ✅ Amount matches | Full write: Done + all fields |
| ✅ URL only (no PDF) | ✅ Amount matches | Write Done + urlgmailadconfirm + trxstring (flag: no PDF) |
| ✅ Found | ❌ No Plaid match | Write urlgmailadconfirm only; leave in PaymentConfirmed; retry next run |
| ❌ Not found | ✅ Amount matches | Write trxstring + datepaidnews; flag for manual Gmail search |
| ❌ Not found | ❌ No Plaid match | Log + skip; do not write |

---

## CRM Write Policy

Read-only by default. Writes require `--write`.

| Field | Written Value | Condition |
|-------|--------------|-----------|
| `statacctgcreditnews` | `'Done'` | Both Gmail + Plaid confirmed |
| `urlgmailadconfirm` | Gmail thread URL | Gmail found |
| `trxstring` | `"{date} \| {txn_name} \| ${amount}"` | Plaid verified |
| `datepaidnews` | Plaid settlement date | Plaid verified |

No other fields. No schema changes without explicit CRM session with Karl.

---

## Deployment

- **Dev/initial:** `claw.auto-ctl.io` at `/opt/auto-cmd/confirmed-ctl/`
- **Production cron:** every 4 hours (after plaid-ctl cron)
- **Cron:**
  ```cron
  30 */4 * * * cd /opt/auto-cmd/confirmed-ctl && venv/bin/python3 -m confirmed_ctl.main process-confirmed --write >> /var/log/confirmed-ctl.log 2>&1
  ```
  (30 min after plaid-ctl at `:00` — gives Plaid time to match first)

---

## Dependencies

| Dependency | Purpose | Status |
|-----------|---------|--------|
| `gmail-ctl` | Gmail search + PDF download | Must be deployed at `/opt/auto-cmd/gmail-ctl/` |
| `plaid-ctl` | Transaction verification | Must be deployed + BoA linked |
| `rclone` | Dropbox upload | Already installed on claw |
| `mysql-connector-python` | CRM access | pip install |
| `click` | CLI framework | pip install |
