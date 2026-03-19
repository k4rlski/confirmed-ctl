# confirmed-ctl — Roadmap

## Current State: v0.1.0 (skeleton)

README + scaffold only. No implementation. Blocked on plaid-ctl being deployed first.

---

## Phase 0 — Prerequisites (plaid-ctl must be live first)

- [ ] plaid-ctl deployed on claw, Plaid Production access active, BoA linked
- [ ] gmail-ctl deployed at `/opt/auto-cmd/gmail-ctl/` and verified working
- [ ] rclone configured for Dropbox on claw (`rclone ls dropbox:` works)
- [ ] CRM MySQL accessible from claw (`permtrak2_crm.t_e_s_t_p_e_r_m`)
- [ ] At least 1 case in `statacctgcreditnews = 'PaymentConfirmed'` to test with

---

## Phase 1 — Core Pipeline (MVP)

**Goal:** Run `confirmed-ctl process-confirmed --write` on a real case and have it fully reconcile.

- [ ] `crm_client.py` — implement trigger query + approved writes
- [ ] `gmail_receipt.py` — search by ad number via gmail-ctl, download PDF attachment
- [ ] `plaid_verifier.py` — re-verify Plaid transaction, get settlement date
- [ ] `dropbox_store.py` — rclone upload + shared link
- [ ] `pipeline.py` — orchestrate per-case: Gmail → Plaid → Dropbox → CRM
- [ ] `main.py` — Click CLI: `process-confirmed`, `fetch-receipt`, `verify-payment`, `status`
- [ ] First live dry run: `confirmed-ctl process-confirmed --dry-run`
- [ ] Manual verification of 1–2 cases before enabling `--write`
- [ ] Deploy to claw: `/opt/auto-cmd/confirmed-ctl/`
- [ ] Wire up cron (30 min after plaid-ctl)

**Deliverable:** Cases move from `PaymentConfirmed` → `Done` automatically, PDFs filed in Dropbox.

---

## Phase 2 — Stability + Alerting

- [ ] MARS status reporting
- [ ] Slack notification: daily summary (`#reports-ctl` or `#auto-ctl`)
- [ ] Slack alert: case stuck >48h in PaymentConfirmed (no Gmail found after 3 runs)
- [ ] `confirmed-ctl status` — show all PaymentConfirmed cases + completion state
- [ ] Partial completion handling: Gmail URL only (no PDF) → flag gracefully
- [ ] Error log to `c_automation_logs` in `permtrak2_adm`

---

## Phase 3 — Production

- [ ] Move cron to rodan (or keep on claw — TBD)
- [ ] Collector script → MARS sub-page at `mars.auto-ctl.io/tool/confirmed-ctl`
- [ ] `confirmed-ctl watch --interval 30` daemon mode
- [ ] Handle `info@perm-ads.com` Gmail account (if confirmation emails go there)
- [ ] CRM field `urlreceiptnews` — propose to Karl, create via Entity Manager, then write Dropbox link

---

## Phase 4 — Pipeline Tightening

- [ ] Clarify trigger field: `statclearancenews = 'Confirmed'` vs `statacctgcreditnews = 'PaymentConfirmed'` — which is correct handoff from plaid-ctl?
- [ ] Decide: should plaid-ctl write to `trxstring` directly, or defer entirely to confirmed-ctl?
- [ ] Handle `PaymentProcessed` cases (60 records) — are these also confirmed-ctl's job?
- [ ] Build retry logic: failed cases accumulate in a retry queue with backoff

---

## Open GitHub Issues (to create)

| # | Title |
|---|-------|
| 1 | Implement `gmail_receipt.py` — search + PDF download via gmail-ctl |
| 2 | Implement `crm_client.py` — trigger query + writes |
| 3 | Implement `plaid_verifier.py` — transaction re-verification |
| 4 | Implement `dropbox_store.py` — rclone upload |
| 5 | Implement `pipeline.py` — per-case orchestration |
| 6 | CLI: `process-confirmed`, `status`, `watch` |
| 7 | MARS reporting + Slack alerts |
| 8 | Retry queue for stuck cases |
| 9 | Propose `urlreceiptnews` CRM field to Karl |
