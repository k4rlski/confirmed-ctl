# Incident RCA — Bank Map Trx freeze (2026-07) — daemon unit never installed

**Severity:** HIGH (bank-transaction candidates silently frozen ~5 days)
**Date detected:** 2026-07-08 (freeze) → diagnosed/fixed 2026-07-13
**Breadcrumb:** [confirmed-ctl#36](https://github.com/k4rlski/confirmed-ctl/issues/36) · RAG `rag/CONFIRMED-CTL-RAG.md`
**Status:** RESOLVED (ops fix on fang; no code change)

---

## Symptom

Map Trx (`/candidates`) on **delta** showed stale, non-matchable bank candidates. New
BofA charges (e.g. the 2026-07-10 Dallas Morning News ad payment) never appeared as
candidates, so recent ads could not be reconciled. Bank-transaction data was effectively
frozen from **2026-07-08 18:32 UTC** onward (~5 days).

## Wrong theories (ruled out)

The freeze *looked* like a front-end or transport problem, so time was initially lost on:

- **UI / proxy** — the `/candidates` page and the delta→fang tunnel were assumed broken.
- **ChromaDB / context** — suspected a retrieval or embedding staleness issue.

None of these were the cause. The delta→fang tunnel returned exactly what fang held; the
data itself was stale at the source.

## Root cause

confirmed-ctl runs on **fang**, and fang shares its Postgres with the other consumers
(so both claw and delta read the same `bank_transactions`). On fang, **only
`confirmed-ctl-api.service` was installed** under `/etc/systemd/system/`.

The ingestion daemon unit **`confirmed-ctl.service`** (`python -m confirmed_ctl.daemon`,
the hourly BofA email-scan) existed in the repo checkout
(`/opt/confirmed-ctl/confirmed-ctl.service`) but was **never installed or enabled**. There
was also **no systemd timer and no cron** invoking `confirmed-ctl sync`.

Result: `bank_transactions` was frozen at the one-off **2026-07-08 manual backfill (42
rows)** — nothing was ever pulling new bank alerts.

### Evidence (pre-fix)

- `confirmed-ctl status` → Last sync `2026-07-08 18:32 UTC`, Fetched 42 / New 0
- Postgres: `count=42`, `max(txn_date)=2026-07-08`, `max(created_in_db)=2026-07-08`
- `systemctl status confirmed-ctl` → *"Unit confirmed-ctl.service could not be found"*
- No confirmed-ctl timers; no confirmed-ctl cron (root / auto-ops / cron.d)

## Fix (ops only — fang; claw untouched)

1. **Catch-up sync:** `confirmed-ctl sync --lookback-days 7` → found=33, inserted=17.
2. **Install + enable the daemon:**
   ```bash
   cp /opt/confirmed-ctl/confirmed-ctl.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now confirmed-ctl.service   # multi-user.target.wants → survives reboot
   ```
   Cycles hourly (`SYNC_INTERVAL_SECONDS=3600`, lookback 2d).

Because fang's Postgres is shared, this single fix restored fresh candidates for **both
claw and delta** at once.

### Verification (post-fix)

- Postgres: `count=59`, `max(txn_date)=2026-07-10`, `max(created_in_db)=now`
- `/candidates/<dallas ad>` → 8 candidates, top = **DALLAS MORNING NEWS −$2270.60 on
  2026-07-10, score 97%** (ad expected $2271.00 / 2026-07-10). This row did not exist
  before the sync.
- delta→fang tunnel returns the same fresh candidates.

## Open follow-ups (tracked, NOT part of this incident)

- **`SCHEMA-DEBITCARD-USED`** ("Your debit card was used") still parse-misses — the RAG
  marks it `# REFINE` (no HTML fixture yet), so these debit-card purchase alerts are
  currently dropped on ingest.
- **Dual BofA alert rows:** BofA can emit two distinct alert emails (distinct Gmail
  `message_id`s) for a single charge → two identical candidate rows. This is not a dedup
  bug (different message_ids); consider a same-transaction multi-alert collapse.

## Lesson

A service that exists in the repo is not a service that is running. When "live" data goes
stale, verify the **producer** (daemon/timer/cron actually installed + enabled) before
suspecting the UI, proxy, or downstream stores. `systemctl status <unit>` returning
"could not be found" is the tell.
