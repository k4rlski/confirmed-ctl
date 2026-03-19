# confirmed-ctl — CRM Schema Reference

## Database
- **Host:** `permtrak.com:3306`
- **Database:** `permtrak2_crm`
- **Table:** `t_e_s_t_p_e_r_m` (PERM cases)
- **Join:** `news` (newspaper vendors) via `p.news_id = n.id`

**Access note:** Direct MySQL access from claw.auto-ctl.io is whitelisted. No SSH hop needed.

---

## Primary Query (confirmed-ctl trigger)

```sql
SELECT
  p.id,
  p.name                  AS company,
  p.adnumbernews          AS ad_number,
  p.pricenewsreal         AS invoice_amount,
  p.dateinvoicednews      AS date_invoiced,
  p.datenewsend           AS ad_end_date,
  p.statacctgcreditnews   AS payment_status,
  p.trxstring             AS bank_transaction,
  p.urlgmailadconfirm     AS gmail_url,
  p.datepaidnews          AS date_paid,
  n.name                  AS newspaper_name,
  n.shortname             AS newspaper_short
FROM t_e_s_t_p_e_r_m p
JOIN news n ON p.news_id = n.id
WHERE p.statacctgcreditnews IN ('Confirmed', 'PaymentConfirmed')
  AND p.trxstring IS NULL
  AND p.deleted = 0
ORDER BY p.dateinvoicednews DESC
```

---

## Key Fields

### Newspaper Ad Fields on `t_e_s_t_p_e_r_m`

| Field | Type | Purpose | confirmed-ctl role |
|-------|------|---------|-------------------|
| `adnumbernews` | mediumtext | Ad number (e.g. `IPR00160880`) | Gmail search key |
| `statacctgcreditnews` | varchar(35) | Payment lifecycle status | **Trigger** (read) + written on completion |
| `statclearancenews` | mediumtext | Bank clearance | Read (set by plaid-ctl) |
| `pricenewsreal` | double | Invoice amount | Plaid amount verification |
| `dateinvoicednews` | date | Date newspaper invoiced | Date window |
| `datenewsstart` | date | Ad run start date | Dropbox filename |
| `datenewsend` | date | Ad run end date | Transaction date window |
| `urlgmailadconfirm` | mediumtext | Gmail thread URL | **Written** by confirmed-ctl |
| `trxstring` | mediumtext | Bank transaction string | **Written** by confirmed-ctl |
| `datepaidnews` | date | Date payment settled | **Written** by confirmed-ctl |
| `news_id` | varchar(17) | FK → news.id | Newspaper folder name |

### `news` Table (newspaper vendors)

| Field | Type | Purpose |
|-------|------|---------|
| `id` | varchar(17) | PK — referenced as `news_id` in cases |
| `name` | varchar(100) | Full name (e.g. `Miami Herald`) |
| `shortname` | varchar(50) | Short name for file paths (e.g. `Miami-Herald`) |

---

## Status Values (`statacctgcreditnews`)

| Value | Count (approx) | Meaning | Tool action |
|-------|---------------|---------|-------------|
| `NA` | ~12,297 | No newspaper ad | Skip |
| `Invoiced` / `["Invoiced"]` | variable | Ad billed, awaiting bank match | plaid-ctl trigger |
| `PaymentProcessed` | ~60 | Payment sent, not cleared | plaid-ctl verifies |
| `Confirmed` | variable | Ad confirmed (human or semi-auto) | **confirmed-ctl trigger** |
| `PaymentConfirmed` | ~593 | Bank-matched by plaid-ctl | **confirmed-ctl trigger** |
| `Done` | — | Fully reconciled | confirmed-ctl writes this |

---

## CRM Write Rules

confirmed-ctl is **read-only by default**. The `--write` flag enables these exact fields:

| Field | Written Value | Condition |
|-------|--------------|-----------|
| `statacctgcreditnews` | `'Done'` | Gmail + Plaid both confirmed |
| `urlgmailadconfirm` | Gmail thread URL | Gmail found |
| `trxstring` | `"{date} \| {txn_name} \| ${amount}"` | Plaid verified |
| `datepaidnews` | Plaid settlement date (`YYYY-MM-DD`) | Plaid verified |

**NEVER write to:** case data fields, ad text, pricing fields, status fields outside this list, or any field not listed here.

**NEVER add schema fields** without Karl creating them manually in EspoCRM Entity Manager first.

---

## Active Pending Cases (as of 2026-03-04)

Representative cases for testing:

| Case # | Company | Ad Number | Newspaper | Amount | Invoice Date |
|--------|---------|-----------|-----------|--------|-------------|
| 10349 | Eduexplora International | IPR00160880 | Miami Herald | $1,368 | 2026-03-02 |
| 10481 | JBM Data System LLC | IPR00160000 | Miami Herald | $1,940 | 2026-02-25 |
| 10551 | Data Cloud Tek LLC | IPR00159980 | Charlotte Observer | $928 | 2026-02-25 |
| 10455 | Solar Sculpture Inc. | IPR00159700 | Macon Telegraph | $457 | 2026-02-24 |
| 10531 | Martorell's Office Group | IPR0015496 | Miami Herald | $1,588 | 2026-02-19 |
| 10338 | Dash Dream Plant | IPR00156910 | Merced Sun Star | $464 | 2026-02-05 |
| 10416 | CG Tax Inc | IPR00156900 | Miami Herald | $1,280 | 2026-02-05 |
| 10419 | LA Nails & Spa LLC | IPR00156890 | Bradenton Herald | $799 | 2026-02-05 |
