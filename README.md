# confirmed-ctl

Automated ad confirmation reconciliation for PERM-Ads.com LLC.

## Purpose

When a newspaper confirms a PERM ad placement, confirmed-ctl:
1. Detects newly confirmed ads from CRM (`statclearancenews = 'Confirmed'`)
2. Searches Gmail by ad number to locate the newspaper confirmation email + receipt
3. Extracts the confirmed charge amount
4. Matches against bank transactions via Plaid API (BofA) / Mercury API
5. Flags discrepancies (missing charges, double charges, wrong amounts)
6. Updates CRM status to `Receipted` or `Charged` when reconciled

## CRM Query
```sql
SELECT t_e_s_t_p_e_r_m.*, news.name AS newspapers_name
FROM t_e_s_t_p_e_r_m
JOIN news ON t_e_s_t_p_e_r_m.news_id = news.id
WHERE statnews = '["Active"]'
  AND statclearancenews = '["Confirmed"]'
  AND t_e_s_t_p_e_r_m.deleted = 0
  AND t_e_s_t_p_e_r_m.statpermcase = '["Active Case"]'
ORDER BY datebuynews DESC
```

## Related Tools
- `plaid-ctl` — Plaid API connector for BofA + Mercury transaction feeds
- `abcf-x` report — source of confirmed ad data at reports.permtrak.com/abcf-x/
