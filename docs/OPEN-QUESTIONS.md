# confirmed-ctl — Open Questions

Resolve these before or during Phase 1 build.

---

## CRM Field Questions

**Q1: What is the exact trigger for confirmed-ctl?**
- The original README says: `statclearancenews = 'Confirmed'`
- The abcf-ctl RAG says: `statacctgcreditnews IN ('Confirmed', 'PaymentConfirmed')`
- Need to clarify: does plaid-ctl write `PaymentConfirmed` to `statacctgcreditnews`, and confirmed-ctl picks that up? Or does a human set `Confirmed` on `statclearancenews`?
- **Action:** Check live CRM records — look at a recently reconciled case and trace the field values

**Q2: Does `trxstring` need to be NULL as the trigger, or is there a cleaner state?**
- Current design: trigger = `PaymentConfirmed` AND `trxstring IS NULL`
- But plaid-ctl might write `trxstring` at the same time it writes `PaymentConfirmed`
- If plaid-ctl writes both simultaneously, `trxstring IS NULL` never matches
- **Resolution:** Decide who writes `trxstring` — plaid-ctl (before confirmed-ctl) or confirmed-ctl (as part of its final write)

**Q3: What happens to `PaymentProcessed` cases (60 records)?**
- These are "payment sent, not cleared" — a limbo state
- Should confirmed-ctl process these? Or only plaid-ctl?
- Are they manually managed?

**Q4: Is there a `urlreceiptnews` or similar field for the Dropbox PDF link?**
- Check EspoCRM Entity Manager for any existing receipt/Dropbox URL field
- If none: propose to Karl to add `urlreceiptnews` via Entity Manager before we write to it

---

## Gmail Questions

**Q5: Does `info@perm-ads.com` receive newspaper confirmation emails?**
- Some papers may CC or send directly to `info@` instead of `auto-ctl@`
- Need to check: is gmail-ctl configured for `info@perm-ads.com`?
- Action: Karl to check inbox at info@perm-ads.com for any ad confirmation emails

**Q6: Which newspapers send PDF attachments vs email-body-only confirmations?**
- Miami Herald: PDF attached? Or just email text?
- Charlotte Observer, Bradenton Herald, Macon Telegraph, Merced Sun Star?
- Action: Karl or daughter to check a few existing confirmation emails in Gmail

**Q7: What Gmail label/folder do confirmation emails land in?**
- Are ad confirmation emails labeled in Gmail? (e.g. "Newspaper Ads" label)
- Or do they land in inbox/all mail?
- Affects search scope in gmail-ctl

---

## Architecture Questions

**Q8: Should confirmed-ctl run on claw or rodan?**
- claw = dev machine, actively used, has Chrome/Playwright, gmail-ctl available
- rodan = frozen production cron server, no `apt upgrade`
- Recommendation: claw for both dev and production (plaid-ctl + confirmed-ctl both live on claw)

**Q9: Clear division of writes between plaid-ctl and confirmed-ctl?**
- Proposed:
  - plaid-ctl writes: `statclearancenews = ["Cleared"]`
  - confirmed-ctl writes: `statacctgcreditnews = 'Done'`, `urlgmailadconfirm`, `trxstring`, `datepaidnews`
- Or:
  - plaid-ctl writes: `statacctgcreditnews = 'PaymentConfirmed'`, `trxstring`
  - confirmed-ctl writes: `statacctgcreditnews = 'Done'`, `urlgmailadconfirm`, `datepaidnews`
- Need to decide before implementing both tools' write layers

**Q10: Is there a Slack channel for confirmed-ctl notifications?**
- Suggest: `#reports-ctl` for daily summary (already exists)
- Or create `#confirmed-ctl` — consistent with naming convention
- Karl to decide

---

## Dropbox Questions

**Q11: Which Dropbox account / rclone remote?**
- rclone remote name: `dropbox` (verify: `rclone listremotes`)
- Which account: `admin@perm-ads.com` or `karl@perm-ads.com`?
- Verify the `Receipts/Newspapers/` path exists or needs to be created

**Q12: Does any existing receipt filing convention conflict with proposed path?**
- Check if Karl or his daughter already files some receipts manually in Dropbox
- Path: `Receipts/Newspapers/{Year}/{YYYY-MM}/{NewspaperShortName}/` — does this match existing structure?
