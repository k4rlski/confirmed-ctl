# confirmed-ctl — Dropbox Receipt Storage

## Path Convention

```
Receipts/Newspapers/{Year}/{YYYY-MM}/{NewspaperShortName}/
  Case-{casenumber}_{CompanySlug}_{AdNumber}_{DateInvoiced}.pdf
```

### Field Sources

| Component | CRM Field | Notes |
|-----------|----------|-------|
| `{Year}` | `dateinvoicednews` year | e.g. `2026` |
| `{YYYY-MM}` | `dateinvoicednews` year+month | e.g. `2026-03` |
| `{NewspaperShortName}` | `news.shortname` | e.g. `Miami-Herald` |
| `{casenumber}` | case number (from case name or CRM ID) | e.g. `10349` |
| `{CompanySlug}` | company name — spaces→hyphens, max 30 chars | e.g. `Eduexplora` |
| `{AdNumber}` | `adnumbernews` | e.g. `IPR00160880` |
| `{DateInvoiced}` | `dateinvoicednews` as `YYYY-MM-DD` | e.g. `2026-03-02` |

---

## Examples

```
Receipts/Newspapers/2026/2026-03/Miami-Herald/
  Case-10349_Eduexplora_IPR00160880_2026-03-02.pdf
  Case-10481_JBM-Data-System_IPR00160000_2026-02-25.pdf
  Case-10531_Martorolls-Office-Group_IPR0015496_2026-02-19.pdf

Receipts/Newspapers/2026/2026-02/Charlotte-Observer/
  Case-10551_DataCloudTek_IPR00159980_2026-02-25.pdf

Receipts/Newspapers/2026/2026-02/Macon-Telegraph/
  Case-10455_Solar-Sculpture_IPR00159700_2026-02-24.pdf

Receipts/Newspapers/2026/2026-02/Bradenton-Herald/
  Case-10419_LA-Nails-Spa_IPR00156890_2026-02-05.pdf
```

---

## rclone Implementation

```python
import subprocess

def upload_receipt(local_path: str, newspaper_short: str, year: str, month: str, filename: str) -> bool:
    remote_path = f"dropbox:Receipts/Newspapers/{year}/{year}-{month}/{newspaper_short}/{filename}"
    result = subprocess.run(
        ["rclone", "copy", local_path, remote_path],
        capture_output=True
    )
    return result.returncode == 0

def generate_shared_link(remote_path: str) -> str:
    result = subprocess.run(
        ["rclone", "link", remote_path],
        capture_output=True, text=True
    )
    return result.stdout.strip()
```

rclone config on claw: `~/.config/rclone/rclone.conf`  
Remote name: `dropbox` (already configured for receipt-ctl)

---

## No PDF Attachment Case

Some newspapers may not attach a PDF to confirmation emails — they confirm via email body only.

In this case:
- Store the Gmail thread URL in `urlgmailadconfirm`
- Log: `WARN: no PDF attachment found for IPR00160880`
- Do not fail — partial record is acceptable
- Flag for manual PDF retrieval if needed

Newspapers known to not attach PDFs: (document here as discovered)

---

## Dropbox Account

- Account: `admin@perm-ads.com` (or `karl@perm-ads.com`)
- rclone remote: configured on claw under `dropbox`
- Verify: `rclone ls dropbox:Receipts/Newspapers/ --max-depth 2`

---

## Shared Link Policy

After upload, generate a Dropbox shared link via `rclone link`. This is stored for future use (not currently a CRM field — will be added when Karl creates it in Entity Manager).

Future CRM field: `urlreceiptnews` (proposed) — needs Karl to create in EspoCRM Entity Manager before we can write it.
