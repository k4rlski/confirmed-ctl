> **ARCHIVED / HISTORICAL — ABANDONED PATH.** The QuickBooks Online (QBO) API
> integration was dropped in Phase 1 when confirmed-ctl pivoted to BofA
> email-scan + OFX/CSV export ingestion. This runbook is retained only for
> reference (Intuit app/realm details, onboarding steps); it does **not**
> describe the current architecture. See `../ARCHITECTURE-SPEC.md` and the
> project README for the live design.

# QBO Production Onboarding Runbook — confirmed-ctl

Purpose: connect the **confirmed-ctl** project to a **production QuickBooks Online (QBO) company** via the QBO Accounting API (read access to transactions).

Every step below is based on the current (2026) Intuit Developer documentation. Each section cites the exact page it came from. Where a page could not be read as static text, that is stated explicitly rather than guessed.

> Scope of this document: **documentation only.** No credentials are used, no server or Intuit account is touched. You (the human operator) perform the clicks in the Intuit Developer Portal; this runbook tells you exactly where to click and why.

---

## TL;DR checklist

- [ ] **1. App:** An app already exists (dashboard app id `9341454666542286`, used by `python-qb-invoice`, currently sandbox). **Recommendation: create a NEW dedicated app** for confirmed-ctl / QBO-CTL for clean separation and its own keys/scopes. (Reusing the existing app is possible but couples the two projects.)
- [ ] **2. Scope:** Request **`com.intuit.quickbooks.accounting`** only. To *read* transactions you do **not** need `com.intuit.quickbooks.payment` or `openid profile email`.
- [ ] **3. Sandbox:** A sandbox company is auto-provisioned per developer account. Test with **Development keys** against the sandbox — no approval needed.
- [ ] **4. Production:** App Dashboard → your app → **Keys & credentials → Production** → fill **App Details** (EULA URL, privacy-policy URL, host domain, launch/disconnect/connect URLs, redirect URIs) → **Compliance → App Assessment Questionnaire** → submit → await approval → **Show Credentials** reveals Production Client ID/Secret.
- [ ] **5. realmId:** `realmId` = QBO **company id**. Shown in the company picker, returned as `realmId` on the OAuth redirect, and visible in the OAuth 2.0 Playground. Your companies: **PERM-Ads.com LLC = `9130352109291776`**, **JKT PARTNERS LLC = `9130346764667536`**.
- [ ] **6. Status check:** Open **Keys & credentials → Production**. If it shows Client ID/Secret + "Show Credentials", production is already approved. If it shows a "complete App Assessment / Start Questionnaire" prompt, production is **not** yet approved.
- [ ] **7. confirmed-ctl target:** likely realm **PERM-Ads.com LLC `9130352109291776`** (matches the BofA export account) — **confirm before wiring**. Scope = accounting read. Redirect URI points at the confirmed-ctl host.

---

## 1. "Do I need to make an app first?"

**Short answer:** Yes — an app is mandatory to talk to QBO, and you already have one. But for confirmed-ctl the recommendation is to create a **new, dedicated** app.

Intuit is explicit that an app is required: *"The first step is to create an app on the Intuit Developer Platform. This generates credentials you'll use to connect to the Intuit OAuth 2.0 server, get access tokens, and make API calls."* — [Authorization FAQ](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/faq). The OAuth setup guide likewise starts with *"Step 1: Create your app on the Intuit Developer Portal … When you create your app, select the QuickBooks Online Accounting scope."* — [Set up OAuth 2.0](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0).

**Current situation on your account:** an app already exists — dashboard app id **`9341454666542286`** — used by `python-qb-invoice`, currently operating in **sandbox**.

**Trade-off: reuse the existing app vs. create a new dedicated app**

| Option | Pros | Cons |
| --- | --- | --- |
| **Reuse app `9341454666542286`** | No new app to assess; keys already exist | Couples confirmed-ctl to python-qb-invoice; shared client id/secret, shared scopes, shared redirect URIs and shared App Assessment; a change for one project can break the other; harder to audit which project holds which token |
| **New dedicated app (recommended)** | Clean separation; its own Client ID/Secret; its own scopes (accounting-read only); its own redirect URIs pointing at the confirmed-ctl host; independent App Assessment and revocation | You must complete a fresh App Assessment for production (one-time, ~40 min) |

**Recommendation: create a new dedicated app** (e.g. named `confirmed-ctl` / `QBO-CTL`) so its keys, scopes and redirect URIs are isolated from python-qb-invoice.

**Steps to create an app** (per [Get started](https://developer.intuit.com/app/developer/qbo/docs/get-started) and [Set up OAuth 2.0 — Step 1](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0)):

1. Sign in at [developer.intuit.com](https://developer.intuit.com) with the developer account that owns the QBO companies.
2. Go to **My Hub → App dashboard** (upper-right of the toolbar). (Click-path per [Get the Client ID and Client Secret](https://developer.intuit.com/app/developer/qbo/docs/get-started/get-client-id-and-client-secret).)
3. Create a new app and, when prompted for scopes, **select the QuickBooks Online Accounting scope** (Intuit's Step 1 instruction: *"select the QuickBooks Online Accounting scope"*).
4. Name it clearly (e.g. `confirmed-ctl`).

> Note: The dedicated **create-an-app** page ([get-started/create-an-app](https://developer.intuit.com/app/developer/qbo/docs/get-started/create-an-app)) is a JavaScript-rendered page and did not return static text when fetched; the click-path above is taken from the Get started and OAuth 2.0 pages, which are authoritative and did render.

---

## 2. "What scope for the API keys?"

**Answer: `com.intuit.quickbooks.accounting`** (this grants Accounting API access, which is what querying transactions uses).

From [Learn about scopes](https://developer.intuit.com/app/developer/qbo/docs/learn/scopes):

| Scope | Description (verbatim from Intuit) |
| --- | --- |
| `com.intuit.quickbooks.accounting` | "Grants access to the QuickBooks Online Accounting API, which focuses on accounting data." |
| `com.intuit.quickbooks.payment` | "Grants access to the QuickBooks Payments API, which focuses on payments processing." |
| `openid` (+ `profile`, `email`, `phone`, `address`) | OpenID Connect / user-profile info (given & family name, email, phone, address). |

To **read transactions** (query `Purchase`, `Bill`, `Invoice`, `Deposit`, etc. via the Accounting API) you need only `com.intuit.quickbooks.accounting`. You do **NOT** need:

- `com.intuit.quickbooks.payment` — that is the **Payments** processing API, a different product, not required to read accounting transactions.
- `openid profile email` — those are **OpenID Connect / sign-in** scopes for retrieving the end-user's identity profile. Reading company transactions does not require them.

Intuit's own accounting example confirms accounting-only is a valid, complete request — the "Connect to QuickBooks, using the accounting scope" example uses just `scope=com.intuit.quickbooks.accounting` ([Authorization FAQ](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/faq)). Intuit also recommends **requesting scopes incrementally** rather than all up front ([Learn about scopes](https://developer.intuit.com/app/developer/qbo/docs/learn/scopes)) — so add nothing beyond accounting-read for confirmed-ctl.

> Each time you change scopes you must re-run the authorization flow and users must re-authorize ([Learn about scopes](https://developer.intuit.com/app/developer/qbo/docs/learn/scopes)).

---

## 3. "Make a new sandbox first?"

**Answer:** You don't have to *manually* make one for basic testing — **a sandbox company is auto-provisioned when you create your developer profile.** You can also add more (up to 10). You test the sandbox with **Development keys**, and **no approval is required** to use it.

From [Create and test with a sandbox company](https://developer.intuit.com/app/developer/qbo/docs/develop/sandboxes/manage-your-sandboxes): *"When you create your developer profile, you automatically get a sandbox company."* They are QBO companies pre-loaded with sample data, for development/testing only.

**To add an additional sandbox company (optional):**

1. Sign in to your developer account.
2. **My Hub → Sandboxes** (upper-right of toolbar).
3. Select **Add** (right side).
4. Choose **QuickBooks Online Plus** or **QuickBooks Online Advanced**.
5. If Plus, pick a **Country** (region is fixed and can't be changed later).
6. Select **Create**. (Up to 10 sandboxes; each valid ~2 years.)

**To test against the sandbox (Development keys — no approval needed):**

1. Sign in → select your app.
2. **Keys and credentials** (left nav).
3. Select **Development** and turn on **Show Credentials**.
4. Copy the **Client ID** and **Client Secret** (these are the *Development* keys; they work only for sandbox companies).
5. Use the sandbox API base URL: **`https://sandbox-quickbooks.api.intuit.com/v3`**.

(Development vs. Production keys are distinct: *"Each app has two sets of credentials: one for live production code and another for sandbox and testing environments … Those for Development are only for your sandbox companies."* — [Get the Client ID and Client Secret](https://developer.intuit.com/app/developer/qbo/docs/get-started/get-client-id-and-client-secret).)

> confirmed-ctl mapping: for sandbox testing set `QBO_API_BASE_URL=https://sandbox-quickbooks.api.intuit.com` in `.env` (the code already documents this in `.env.example`), and use the Development Client ID/Secret.

---

## 4. "Then get Prod set up / approved?"

Production credentials are gated behind the **App Assessment Questionnaire**. Until it is submitted and approved, the Production Client ID/Secret are hidden.

Intuit is explicit: *"The client ID and client secret will be accessible only after completing the Production Key questionnaire and its approval."* ([Get the Client ID and Client Secret](https://developer.intuit.com/app/developer/qbo/docs/get-started/get-client-id-and-client-secret)). And: *"If you have a new or development-mode app, you will not be able to access your production credentials until your questionnaire is submitted and your app is approved."* ([App assessment and compliance FAQ](https://help.developer.intuit.com/s/article/New-app-assessment-process-FAQ)).

**Important:** even a **private** app that connects to a production company must complete this — *"If your app has any connections to production QuickBooks Online companies, you will need to submit this questionnaire even if your app is not listed on the QuickBooks App store."* ([App assessment and compliance FAQ](https://help.developer.intuit.com/s/article/New-app-assessment-process-FAQ)).

**Exact click-path** (per [Publish your app](https://developer.intuit.com/app/developer/qbo/docs/go-live/publish-app)):

1. **Fill out App Details for Production**
   - Sign in → **My Hub → App dashboard** → open your app.
   - **Keys and credentials** (left nav) → select **Production**.
   - Select **App Details** and fill every section:
     - contact **email address** and other details;
     - **End User License Agreement (EULA) URL** and **Privacy Policy URL**;
     - app **host domain**, **launch URL**, **disconnect URL**, and **connect/reconnect URL**;
     - up to **four categories**;
     - any **regulated industries** the app was built for;
     - **regions** where the app is hosted.
   - (Intuit estimates ~30 minutes.) These URLs are **required before you can get production credentials.**
2. **Review OAuth 2.0** — confirm OAuth is fully set up; test **connect / disconnect / reconnect** from a sandbox company first to catch errors.
3. **Update your Intuit Developer Account profile** — **My Hub → Account Profile**; verify contact/account info.
4. **Complete the App Assessment Questionnaire**
   - Sign in → **My Hub → Account Profile** → open the app → **Keys and credentials** (left nav) → **Production** → **Compliance** → **Start Questionnaire** → complete it.
   - (Alternate access per the [App assessment FAQ](https://help.developer.intuit.com/s/article/New-app-assessment-process-FAQ): app dashboard → select app → **Production Settings** tab → **App assessment questionnaire** in the left-side nav.)
5. **Review name/icon/settings** — **Settings** (left nav) → **Basic app info**; confirm name and upload the icon; check the other tabs.
6. **Get production credentials** — **Keys and credentials → Production → Show Credentials** (the **Show Credentials** switch appears **only after the questionnaire is approved**) → copy the **Client ID** and **Client Secret**.

**What the questionnaire requires / asks** (per [App assessment and compliance FAQ](https://help.developer.intuit.com/s/article/New-app-assessment-process-FAQ)):

- It will **ask you to attest** that you meet Intuit's platform requirements, **gather information about how your app uses the QuickBooks platform**, and **ask for additional info if your app operates in certain (regulated) industries.**
- Have handy: your app's data-security/compliance posture against Intuit's platform requirements, EULA + privacy-policy URLs, and details of how the app stores/uses QBO data.
- It takes **~40 minutes** once you have the data; you can **Save** and resume (unsaved answers are lost on navigation).
- **After submitting:** you get a confirmation email; status typically appears on the app dashboard **within ~5 minutes** (**Production Settings → App assessment questionnaire**). If approved → done. If not approved → the portal shows the reason and you can update & resubmit; hard rejection emails the primary contact and blocks new connections until resolved.

---

## 5. "Where is the realm/company ID?"

**`realmId` = the QuickBooks Online company id.** Intuit defines it as *"The unique ID of the connected user's QuickBooks Online company. It's also sometimes called the 'company ID.'"* ([Set up OAuth 2.0 — Step 11](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0)).

You can obtain the realmId three ways:

1. **Company picker in QuickBooks** — the company you sign into is the realm. Your account has:
   - **PERM-Ads.com LLC → `9130352109291776`**
   - **JKT PARTNERS LLC → `9130346764667536`**
2. **On the OAuth redirect** — after the user authorizes, Intuit appends `realmId` to your redirect URI, e.g.:

```
https://www.mydemoapp.com/oauth-redirect?
    code=4/P7q7W91a-oMsCeLvIaQm6bTrgtp7&
    state=...&
    realmId=1231434565226279
```

   (Example from [Set up OAuth 2.0 — Step 11](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0).) You then use this `realmId` in every API URL: `/v3/company/{realmId}/...`.
3. **OAuth 2.0 Playground** — running the auth flow in the Playground surfaces the `realmId` (company id) alongside the returned tokens.

> Note: The interactive [OAuth 2.0 Playground](https://developer.intuit.com/app/developer/playground) is a JavaScript app and did not return static text when fetched, so its live UI could not be captured verbatim here. Intuit's OAuth guide references it directly — *"Check out the OAuth Playground to preview each step of the authorization flow"* ([Set up OAuth 2.0 — Step 2](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0)) — and because the Playground performs the same authorization-code flow, it returns/echoes the same `realmId` value shown on the redirect above.

> confirmed-ctl mapping: set `QBO_REALM_ID` in `.env` to the chosen company id (the code builds every request as `/v3/company/{QBO_REALM_ID}/...` — see `confirmed_ctl/qbo/client.py`).

---

## 6. Current status check — does the app already have production keys?

Because your **production companies already appear in the company picker**, production access may already be partially or fully set up. To verify definitively:

1. Sign in → **My Hub → App dashboard** → open the app (existing app id `9341454666542286`, or the new dedicated app).
2. **Keys and credentials** (left nav) → select the **Production** tab.
3. Interpret what you see:
   - **If it shows a Client ID / Client Secret with a "Show Credentials" switch** → the App Assessment Questionnaire has been **approved** and **production keys exist**. The **Show Credentials** switch only appears after approval ([Publish your app — Step 6](https://developer.intuit.com/app/developer/qbo/docs/go-live/publish-app)).
   - **If it shows the App Assessment / "Start Questionnaire" prompt instead of credentials** → production is **not** approved yet; the questionnaire is still required ([Get the Client ID and Client Secret](https://developer.intuit.com/app/developer/qbo/docs/get-started/get-client-id-and-client-secret): *"For apps without a completed questionnaire, the questionnaire will be visible when you select Production"*).
4. Cross-check via **Production Settings → App assessment questionnaire** (left-side nav) — it shows the assessment **result/status** ([App assessment and compliance FAQ](https://help.developer.intuit.com/s/article/New-app-assessment-process-FAQ)).

> Caveat: If you create a **new dedicated app** (recommendation in §1), that new app starts with **no production keys** regardless of the existing app's status — you'll run §4 for it. The existing app `9341454666542286` may already be approved for production; that approval does **not** transfer to a new app.

---

## 7. confirmed-ctl specifics

- **Target realm (to confirm):** likely **PERM-Ads.com LLC → `9130352109291776`**, because that matches the Bank of America export account confirmed-ctl reconciles against. **The operator must confirm** this is the correct company before wiring `QBO_REALM_ID`. (The alternative on the account is JKT PARTNERS LLC `9130346764667536`.)
- **Scope:** `com.intuit.quickbooks.accounting` (read). Nothing else needed for reading transactions (§2).
- **Redirect URI:** must be **HTTPS** (Intuit rejects non-TLS and IP-address URIs — [Authorization FAQ](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/faq)) and must point at the **confirmed-ctl host**, matching exactly (scheme, casing, trailing slash) what is registered in the app's Keys & credentials.
- **Environment / base URL:** production = `https://quickbooks.api.intuit.com` (the code default in `confirmed_ctl/settings.py`); sandbox = `https://sandbox-quickbooks.api.intuit.com`. Set via `QBO_API_BASE_URL`.
- **Credentials wiring:** put the app's **Production** Client ID/Secret into `QBO_CLIENT_ID` / `QBO_CLIENT_SECRET`, and the company id into `QBO_REALM_ID` (see `.env.example`). The token file at `QBO_TOKEN_PATH` holds the access/refresh tokens produced by the OAuth flow; the client auto-refreshes (QBO rotates the refresh token ~every 24h).
- **Token lifetimes to design around** (from [Authorization FAQ](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/faq)): access token = **1 hour**; refresh token = **100 days rolling** (expires if unused for 100 days), **hard limit 5 years**; always persist the **latest** refresh token from each response. The existing `client.py` already implements refresh-on-expiry and refresh-on-401.

---

## Source pages (all read on 2026-07-08)

- Get started: <https://developer.intuit.com/app/developer/qbo/docs/get-started>
- Get the Client ID and Client Secret (Dev vs Prod keys): <https://developer.intuit.com/app/developer/qbo/docs/get-started/get-client-id-and-client-secret>
- Learn about scopes: <https://developer.intuit.com/app/developer/qbo/docs/learn/scopes>
- Set up OAuth 2.0 (authorization-code flow): <https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0>
- Authorization FAQ (realmId / tokens / redirect URIs): <https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/faq>
- Create and test with a sandbox company: <https://developer.intuit.com/app/developer/qbo/docs/develop/sandboxes/manage-your-sandboxes>
- Publish your app (go-live / production): <https://developer.intuit.com/app/developer/qbo/docs/go-live/publish-app>
- App assessment and compliance FAQ: <https://help.developer.intuit.com/s/article/New-app-assessment-process-FAQ>
- OAuth 2.0 Playground: <https://developer.intuit.com/app/developer/playground> (JS app — not readable as static text; described via the OAuth 2.0 guide above)
- Create an app (get-started/create-an-app): <https://developer.intuit.com/app/developer/qbo/docs/get-started/create-an-app> (JS app — not readable as static text; click-path taken from Get started + OAuth 2.0 pages)
