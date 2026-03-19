# confirmed-ctl RAG Knowledge Base

> **Repo:** https://github.com/k4rlski/confirmed-ctl (PRIVATE)
> **Last updated:** 2026-03-19
> **Status:** 🟡 v0.1.0 skeleton — depends on plaid-ctl + gmail-ctl deployment

---

## 1. Purpose

Automated newspaper ad receipt collection and final reconciliation for PERM-Ads.com LLC.

Picks up where plaid-ctl leaves off. When the bank confirms a newspaper charge, confirmed-ctl:
1. Searches Gmail by ad number → downloads PDF receipt
2. Stores receipt in Dropbox under structured folder hierarchy
3. Writes Gmail URL + transaction string + paid date back to CRM
4. Marks the case `Done` (fully reconciled)

**Trigger:** `statacctgcreditnews IN ('Confirmed', 'PaymentConfirmed')` AND `trxstring IS NULL`

---

## 2. Pipeline

```
plaid-ctl → statacctgcreditnews='PaymentConfirmed'
                   │
confirmed-ctl processes:
  ├─ Gmail search: adnumbernews (e.g. IPR00160880)
  │     → download PDF attachment
  │     → store: Receipts/Newspapers/{Year}/{YYYY-MM}/{NewspaperShortName}/Case-{#}_{Co}_{Ad}_{Date}.pdf
  │     → CRM: urlgmailadconfirm = Gmail thread URL
  ├─ Plaid verify: re-confirm transaction amount + settlement date
  │     → CRM: trxstring = "{date} | {txn_name} | ${amount}"
  │     → CRM: datepaidnews = settlement date
  └─ CRM: statacctgcreditnews = 'Done'
```

---

## 3. CLI Reference

```bash
confirmed-ctl process-confirmed              # full pipeline, all confirmed cases
confirmed-ctl process-confirmed --dry-run   # no writes, no storage
confirmed-ctl process-confirmed --case 10349  # single case
confirmed-ctl process-confirmed --hours 72  # expand bank search window
confirmed-ctl fetch-receipt --case 10349    # just Gmail + receipt step
confirmed-ctl verify-payment --case 10349   # just Plaid verification step
confirmed-ctl status                        # show all confirmed cases + completion state
confirmed-ctl watch --interval 30           # daemon mode (poll every 30 min)
```

---

## 4. CRM Data Model

**DB:** `permtrak2_crm.t_e_s_t_p_e_r_m`  
**Join:** `news` via `p.news_id = n.id`

| Field | Role |
|-------|------|
| `statacctgcreditnews` | **Trigger:** `Confirmed`/`PaymentConfirmed`. Written: `Done` |
| `adnumbernews` | Gmail search key (e.g. `IPR00160880`) |
| `pricenewsreal` | Amount for Plaid re-verification |
| `urlgmailadconfirm` | **Written:** Gmail thread URL |
| `trxstring` | **Written:** Plaid transaction string |
| `datepaidnews` | **Written:** Plaid settlement date |
| `news_id` → `news.shortname` | Newspaper folder name in Dropbox |

**Approved writes only:** `statacctgcreditnews`, `trxstring`, `urlgmailadconfirm`, `datepaidnews`

---

## 5. Dropbox Path Convention

```
Receipts/Newspapers/{Year}/{YYYY-MM}/{NewspaperShortName}/
  Case-{casenumber}_{CompanySlug}_{AdNumber}_{DateInvoiced}.pdf
```

Examples:
```
Receipts/Newspapers/2026/2026-03/Miami-Herald/Case-10349_Eduexplora_IPR00160880_2026-03-02.pdf
Receipts/Newspapers/2026/2026-02/Charlotte-Observer/Case-10551_DataCloudTek_IPR00159980_2026-02-25.pdf
```

---

## 6. Dependencies

| Dependency | Purpose | Must be deployed first |
|-----------|---------|----------------------|
| `plaid-ctl` | Transaction verification | Yes — writes PaymentConfirmed cases |
| `gmail-ctl` | Gmail search + PDF download | Yes — at `/opt/auto-cmd/gmail-ctl/` |
| `rclone` | Dropbox upload | Already on claw (receipt-ctl) |
| `permtrak2_crm` | CRM read/write | Direct MySQL from claw |

---

## 7. Deployment

**Server:** `claw.auto-ctl.io`
**Path:** `/opt/auto-cmd/confirmed-ctl/`
**Cron (30 min after plaid-ctl):**
```cron
30 */4 * * * cd /opt/auto-cmd/confirmed-ctl && venv/bin/python3 -m confirmed_ctl.main process-confirmed --write >> /var/log/confirmed-ctl.log 2>&1
```

---

## 8. Status Transitions

```
[Invoiced]
    │ plaid-ctl
    ▼
[PaymentConfirmed] + statclearancenews=["Cleared"]
    │ confirmed-ctl
    ▼
[Done] + urlgmailadconfirm + trxstring + datepaidnews
```

---

## 9. Key Open Questions

See `docs/OPEN-QUESTIONS.md` for full list. Critical:
- Exact trigger field: `statclearancenews='Confirmed'` vs `statacctgcreditnews='PaymentConfirmed'`?
- Does plaid-ctl write `trxstring` before or after confirmed-ctl?
- Which Dropbox account + rclone remote name?
- Does `info@perm-ads.com` receive confirmation emails?

---

## 10. Related Tools

- **plaid-ctl** — upstream dependency (bank transaction matching)
- **gmail-ctl** — Gmail search + PDF download
- **abcf-x-report.php** — human-facing view at reports.permtrak.com
- **receipt-ctl** — vendor invoice downloader (different domain: vendors, not newspapers)
