# confirmed-ctl — Workflow Reference

## Pipeline Position

confirmed-ctl is the **second stage** of the newspaper ad reconciliation pipeline.

```
plaid-ctl           → matches bank transaction to CRM invoice
                       writes: statacctgcreditnews='PaymentConfirmed', trxstring

confirmed-ctl       → collects Gmail receipt PDF, stores to Dropbox, finalizes CRM
                       writes: statacctgcreditnews='Done', urlgmailadconfirm, datepaidnews
```

---

## Step-by-Step: Normal Case

**Case:** Eduexplora International — Ad IPR00160880 — Miami Herald — $1,368

### 1. CRM state entering confirmed-ctl
```
statacctgcreditnews: 'PaymentConfirmed'
adnumbernews:        'IPR00160880'
pricenewsreal:       1368.00
news_id:             → news.shortname = 'Miami-Herald'
trxstring:           NULL  (not yet written — triggers confirmed-ctl)
urlgmailadconfirm:   NULL
datepaidnews:        NULL
```

### 2. Gmail search
```bash
# gmail-ctl searches: auto-ctl@perm-ads.com + info@perm-ads.com
# Query: "IPR00160880"
# Returns: thread URL + PDF attachment
```

### 3. Plaid verify
```bash
# plaid-ctl verifies transaction still exists
# Confirms amount: $1,368 ±$1
# Gets settlement date: 2026-03-10
```

### 4. Dropbox storage
```
rclone copy IPR00160880.pdf "dropbox:Receipts/Newspapers/2026/2026-03/Miami-Herald/Case-10349_Eduexplora_IPR00160880_2026-03-02.pdf"
```

### 5. CRM write
```sql
UPDATE t_e_s_t_p_e_r_m SET
  statacctgcreditnews = 'Done',
  urlgmailadconfirm   = 'https://mail.google.com/mail/u/0/#inbox/thread-id',
  trxstring           = '2026-03-10 | MIAMI HERALD MEDIA CO | $1368.00',
  datepaidnews        = '2026-03-10'
WHERE id = '...'  -- case 10349
```

### 6. Final CRM state
```
statacctgcreditnews: 'Done'               ✅
urlgmailadconfirm:   'https://mail...'    ✅
trxstring:           '2026-03-10 | ...'   ✅
datepaidnews:        '2026-03-10'         ✅
```

---

## Status Transitions

```
Invoiced
  │ (plaid-ctl — bank match found)
  ▼
PaymentConfirmed  ← confirmed-ctl picks up here
  │ (gmail found + plaid verified + dropbox stored)
  ▼
Done  ← fully reconciled
```

---

## Commands in Daily Use

```bash
# Morning run — check pending and process
confirmed-ctl status                      # how many cases in PaymentConfirmed?
confirmed-ctl process-confirmed --dry-run # preview before writing
confirmed-ctl process-confirmed --write   # execute

# Single case (troubleshooting)
confirmed-ctl process-confirmed --case 10349 --dry-run
confirmed-ctl process-confirmed --case 10349 --write

# Just the Gmail/receipt step
confirmed-ctl fetch-receipt --case 10349

# Check a case's Plaid transaction
confirmed-ctl verify-payment --case 10349
```

---

## Cron Schedule

```cron
# confirmed-ctl — run 30 min after plaid-ctl (which runs at :00)
30 */4 * * * cd /opt/auto-cmd/confirmed-ctl && venv/bin/python3 -m confirmed_ctl.main process-confirmed --write >> /var/log/confirmed-ctl.log 2>&1
```

---

## Edge Cases

### Gmail not found
- Log: `WARN: IPR00160880 — no Gmail match in auto-ctl@perm-ads.com or info@perm-ads.com`
- Do not write `Done` to CRM
- If Plaid verified: write `trxstring` + `datepaidnews` only
- Retry on next cron run
- After 3 failed runs: Slack alert for manual search

### No PDF attachment (Gmail URL only)
- Log: `WARN: IPR00160880 — Gmail found, no PDF attachment`
- Store Gmail thread URL
- Mark as partial: write `urlgmailadconfirm` but add note (flag field TBD)
- Some newspapers don't attach PDFs — Karl to confirm which ones

### Plaid verification fails
- Case was marked `PaymentConfirmed` by plaid-ctl earlier but transaction now missing
- Log: `WARN: IPR00160880 — Plaid transaction no longer found`
- Do not write `Done`
- Alert Karl — may be a reconciliation error

### Amount mismatch on re-verify
- Plaid transaction amount differs from `pricenewsreal`
- Flag for manual review — do not auto-write
- Possible newspaper adjustment/credit

---

## ABCF-X Report Connection

The ABCF-X report at `reports.permtrak.com/abcf-x/` shows the human-facing view.
It reads:
- `statacctgcreditnews` — payment lifecycle status
- `urlgmailadconfirm` — links to Gmail confirmation
- `trxstring` — bank transaction reference

After confirmed-ctl writes `Done`, the case will move from the "Pending" section to "Reconciled" on the report.
