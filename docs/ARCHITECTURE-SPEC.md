# confirmed-ctl + receipt-ctl — Full Architecture Specification
## Host: `prx.auto-ops.net` | Stack: Python / Flask / PostgreSQL / ChromaDB
**Prepared:** June 18, 2026 | Handoff target: Cursor + confirmed-ctl repo

---

## 1. System Overview

Two cooperating tools that together close the financial loop on PERM newspaper
advertisement purchases — from bank charge through to saved receipt.

```
┌─────────────────────────────────────────────────────────────────┐
│                        prx.auto-ops.net                         │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  confirmed-ctl  (/opt/confirmed-ctl)                    │   │
│  │                                                         │   │
│  │  • Daemon / cron — syncs QBO → local DB                 │   │
│  │  • CLI:  confirmed-ctl sync                             │   │
│  │          confirmed-ctl status                           │   │
│  │          confirmed-ctl match --ad-id XXXX               │   │
│  │  • RAG layer (ChromaDB) — vendor + amount patterns      │   │
│  │  • Writes to: bank_transactions table                   │   │
│  │               ad_confirmations table                    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  receipt-ctl  (/opt/receipt-ctl  OR  confirmed-ctl/     │   │
│  │                receipts/ submodule)                     │   │
│  │                                                         │   │
│  │  • Given Gmail thread ID → fetch attachment             │   │
│  │  • Save to /mnt/receipts/{year}/{month}/{ad_number}/    │   │
│  │  • Update CRM record with file path / object store URL  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
          ↕ internal DB                    ↕ Gmail API
┌─────────────────────┐         ┌──────────────────────────┐
│   Central Postgres  │         │   Gmail (existing OAuth) │
│   (your CRM DB)     │         │   Thread search by       │
│                     │         │   ad number string       │
└─────────────────────┘         └──────────────────────────┘
          ↕
┌─────────────────────────────────────────────────────────────────┐
│            Flask UI  (existing app — new report page)           │
│                                                                 │
│  Report:  "Advise Cash Flow" OR new tab "Confirmed-CTL"         │
│                                                                 │
│  ┌── Unconfirmed Ads Table ──────────────────────────────────┐  │
│  │  Ad#  | Client | Newspaper | Run Date | Amount | Status   │  │
│  │  [click ad number] → popup                                │  │
│  └────────────────────────────────────────────────────────── ┘  │
│                                                                 │
│  ┌── Popup: Confirmed-CTL ──────────────────────────────────┐   │
│  │  [Sync Now] button  ← triggers confirmed-ctl sync CLI    │   │
│  │                                                          │   │
│  │  BANK TRANSACTIONS (last 5 days, ranked by match score)  │   │
│  │   ● 2026-06-17  LA Times  $425.00  [RELATE] ← best match │   │
│  │   ○ 2026-06-16  LA Times  $210.00  [RELATE]              │   │
│  │                                                          │   │
│  │  GMAIL THREADS (search: "{ad_number}")                   │   │
│  │   ● Thread: "Ad Confirmation #20240617-1234" Jun 17      │   │
│  │   ○ Thread: "Receipt – LA Times PERM" Jun 16             │   │
│  │                                [RELATE EMAIL]            │   │
│  │                                                          │   │
│  │  [CONFIRM & CLOSE]  saves: txn_id + gmail_thread_id      │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Repo Structure: `confirmed-ctl`

```
/opt/confirmed-ctl/
├── confirmed_ctl/
│   ├── __init__.py
│   ├── cli.py                  # Click CLI: sync, status, match, receipts
│   ├── daemon.py               # Daemon wrapper (runs sync on schedule)
│   ├── qbo/
│   │   ├── client.py           # OAuth token manager + qbo_get()
│   │   ├── sync.py             # Fetch Purchase/BillPayment from QBO → DB
│   │   └── tokens.json         # Encrypted token store (gitignored)
│   ├── gmail/
│   │   ├── client.py           # Gmail OAuth + thread search
│   │   └── receipts.py         # Attachment downloader (receipt-ctl logic)
│   ├── matching/
│   │   ├── scorer.py           # Candidate scoring: vendor + amount + date
│   │   └── rag.py              # ChromaDB embed + retrieve for pattern learning
│   ├── db/
│   │   ├── models.py           # SQLAlchemy models
│   │   └── migrations/         # Alembic migrations
│   └── api/
│       └── routes.py           # Lightweight internal Flask/FastAPI for UI calls
├── receipts/                   # receipt-ctl submodule or symlink
├── .env                        # Secrets (gitignored)
├── .env.example
├── requirements.txt
├── confirmed-ctl.service       # systemd unit file
└── README.md
```

---

## 3. Database Schema

Add to your existing Postgres database (or confirmed-ctl creates its own schema):

### 3.1 `bank_transactions` — Synced from QBO

```sql
CREATE TABLE IF NOT EXISTS bank_transactions (
    id                  SERIAL PRIMARY KEY,
    qbo_id              VARCHAR(50)     NOT NULL UNIQUE,   -- QBO Purchase.Id — dedup key
    sync_token          VARCHAR(20),                       -- QBO SyncToken (for updates)
    txn_date            DATE            NOT NULL,
    created_time        TIMESTAMPTZ,                       -- MetaData.CreateTime from QBO
    updated_time        TIMESTAMPTZ,                       -- MetaData.LastUpdatedTime
    total_amount        NUMERIC(10,2)   NOT NULL,
    payment_type        VARCHAR(20),                       -- Check, CreditCard, Cash
    payment_ref_num     VARCHAR(100),                      -- Check # or bank ref
    private_note        TEXT,                              -- QBO Memo field
    doc_number          VARCHAR(100),
    vendor_id           VARCHAR(50),
    vendor_name         VARCHAR(255),
    account_id          VARCHAR(50),
    account_name        VARCHAR(255),                      -- e.g. "Business Checking (BofA)"
    line_descriptions   TEXT[],                            -- Array of per-line descriptions
    raw_json            JSONB,                             -- Full QBO response for reference
    confirmed_ad_id     INTEGER REFERENCES ad_purchases(id) ON DELETE SET NULL,
    confirmed_at        TIMESTAMPTZ,
    created_in_db       TIMESTAMPTZ     DEFAULT NOW()
);

CREATE INDEX idx_bank_txn_date       ON bank_transactions(txn_date DESC);
CREATE INDEX idx_bank_txn_vendor     ON bank_transactions(vendor_name);
CREATE INDEX idx_bank_txn_amount     ON bank_transactions(total_amount);
CREATE INDEX idx_bank_txn_confirmed  ON bank_transactions(confirmed_ad_id);
```

### 3.2 `ad_confirmations` — The Relationship Record

```sql
CREATE TABLE IF NOT EXISTS ad_confirmations (
    id                  SERIAL PRIMARY KEY,
    ad_id               INTEGER         NOT NULL REFERENCES ad_purchases(id),
    bank_txn_id         INTEGER         REFERENCES bank_transactions(id),
    gmail_thread_id     VARCHAR(255),                      -- Gmail thread ID string
    gmail_message_id    VARCHAR(255),                      -- Specific message if needed
    gmail_subject       TEXT,                              -- Stored for display
    receipt_file_path   TEXT,                              -- Local path after download
    receipt_url         TEXT,                              -- Object store URL if applicable
    confirmed_by        VARCHAR(100),                      -- User who confirmed (e.g. "daughter")
    confirmed_at        TIMESTAMPTZ     DEFAULT NOW(),
    match_confidence    VARCHAR(10),                       -- 'auto_high', 'auto_med', 'manual'
    match_method        VARCHAR(50),                       -- 'vendor+amount+date', 'manual', etc.
    notes               TEXT,
    UNIQUE(ad_id)                                          -- One confirmation per ad
);
```

### 3.3 `confirmed_ctl_sync_log` — Audit Trail

```sql
CREATE TABLE IF NOT EXISTS confirmed_ctl_sync_log (
    id              SERIAL PRIMARY KEY,
    synced_at       TIMESTAMPTZ     DEFAULT NOW(),
    lookback_days   INTEGER,
    txns_fetched    INTEGER,
    txns_new        INTEGER,
    txns_updated    INTEGER,
    auto_matched    INTEGER,
    errors          TEXT,
    duration_ms     INTEGER
);
```

---

## 4. QBO Sync Layer — `confirmed_ctl/qbo/sync.py`

```python
"""
confirmed_ctl/qbo/sync.py

Pulls Purchase transactions from QBO and upserts into bank_transactions table.
Designed to run as a cron job or triggered via CLI / Flask sync button.
"""
import urllib.parse
from datetime import date, timedelta, datetime, timezone
from .client import qbo_get
from ..db.models import BankTransaction, SyncLog
from sqlalchemy.orm import Session


def sync_recent_transactions(
    db: Session,
    lookback_days: int = 2,
    use_cdc: bool = True,
) -> dict:
    """
    Main sync entry point. Called by daemon, cron, or Flask sync button.
    Returns summary dict for logging and UI display.
    """
    started_at = datetime.now(timezone.utc)
    summary = {"fetched": 0, "new": 0, "updated": 0, "errors": []}

    try:
        if use_cdc:
            raw_txns = _fetch_via_cdc(db, lookback_days)
        else:
            raw_txns = _fetch_via_date_query(lookback_days)

        summary["fetched"] = len(raw_txns)

        for raw in raw_txns:
            try:
                result = _upsert_transaction(db, raw)
                if result == "new":
                    summary["new"] += 1
                elif result == "updated":
                    summary["updated"] += 1
            except Exception as e:
                summary["errors"].append(f"QBO ID {raw.get('Id')}: {str(e)}")

        db.commit()

    except Exception as e:
        db.rollback()
        summary["errors"].append(f"Sync failed: {str(e)}")

    # Write sync log
    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    log = SyncLog(
        lookback_days=lookback_days,
        txns_fetched=summary["fetched"],
        txns_new=summary["new"],
        txns_updated=summary["updated"],
        errors="\n".join(summary["errors"]) if summary["errors"] else None,
        duration_ms=duration_ms,
    )
    db.add(log)
    db.commit()

    return summary


def _fetch_via_date_query(lookback_days: int) -> list[dict]:
    since_date = (date.today() - timedelta(days=lookback_days)).isoformat()
    query = (
        f"SELECT * FROM Purchase "
        f"WHERE TxnDate >= '{since_date}' "
        f"ORDERBY TxnDate DESC MAXRESULTS 200"
    )
    result = qbo_get("query", {"query": query})
    return result.get("QueryResponse", {}).get("Purchase", [])


def _fetch_via_cdc(db: Session, fallback_days: int) -> list[dict]:
    """
    Use CDC for incremental sync. Falls back to date query on first run.
    Reads last successful sync time from sync log table.
    """
    last_sync = db.query(SyncLog)\
                  .filter(SyncLog.errors == None)\
                  .order_by(SyncLog.synced_at.desc())\
                  .first()

    if last_sync:
        since = last_sync.synced_at.isoformat()
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=fallback_days)).isoformat()

    result = qbo_get("cdc", {
        "entities": "Purchase,BillPayment",
        "changedSince": since,
    })
    cdc = result.get("CDCResponse", [{}])[0].get("QueryResponse", [])
    purchases = []
    for group in cdc:
        for entity_type in ("Purchase", "BillPayment"):
            if entity_type in group:
                purchases.extend(group[entity_type])
    return purchases


def _upsert_transaction(db: Session, raw: dict) -> str:
    """Insert or update a single transaction. Returns 'new' or 'updated'."""
    qbo_id = raw.get("Id")
    lines = raw.get("Line", [])
    line_descs = [l.get("Description", "") for l in lines if l.get("Description")]

    existing = db.query(BankTransaction).filter_by(qbo_id=qbo_id).first()

    data = dict(
        qbo_id          = qbo_id,
        sync_token      = raw.get("SyncToken"),
        txn_date        = raw.get("TxnDate"),
        created_time    = raw.get("MetaData", {}).get("CreateTime"),
        updated_time    = raw.get("MetaData", {}).get("LastUpdatedTime"),
        total_amount    = float(raw.get("TotalAmt", 0)),
        payment_type    = raw.get("PaymentType"),
        payment_ref_num = raw.get("PaymentRefNum"),
        private_note    = raw.get("PrivateNote"),
        doc_number      = raw.get("DocNumber"),
        vendor_id       = raw.get("EntityRef", {}).get("value"),
        vendor_name     = raw.get("EntityRef", {}).get("name"),
        account_id      = raw.get("AccountRef", {}).get("value"),
        account_name    = raw.get("AccountRef", {}).get("name"),
        line_descriptions = line_descs,
        raw_json        = raw,
    )

    if existing:
        for k, v in data.items():
            setattr(existing, k, v)
        return "updated"
    else:
        db.add(BankTransaction(**data))
        return "new"
```

---

## 5. Matching / Scoring Layer — `confirmed_ctl/matching/scorer.py`

This is what powers the ranked candidate list in the popup.
No memo text matching needed — pure signal-based scoring.

```python
"""
confirmed_ctl/matching/scorer.py

Given an unconfirmed ad record, return ranked bank transaction candidates.
Scoring uses: vendor name similarity, amount match, date proximity.
"""
from datetime import date, timedelta
from difflib import SequenceMatcher
from ..db.models import BankTransaction, AdPurchase
from sqlalchemy.orm import Session


# Configurable weights
WEIGHT_AMOUNT   = 0.50   # Exact or near-exact amount is strongest signal
WEIGHT_VENDOR   = 0.30   # Vendor name substring match
WEIGHT_DATE     = 0.20   # Date proximity


def get_candidate_transactions(
    db: Session,
    ad: AdPurchase,
    lookback_days: int = 5,
    top_n: int = 8,
) -> list[dict]:
    """
    Return top_n ranked bank transactions as candidates for confirming this ad.

    ad must have: expected_amount, newspaper_name, expected_charge_date (or run_date)
    """
    window_start = ad.expected_charge_date - timedelta(days=lookback_days)
    window_end   = ad.expected_charge_date + timedelta(days=2)  # charges can post slightly late

    # Pull candidate transactions from DB — pre-filter by date window and unmatched only
    candidates = db.query(BankTransaction).filter(
        BankTransaction.txn_date >= window_start,
        BankTransaction.txn_date <= window_end,
        BankTransaction.confirmed_ad_id == None,      # Not already matched
    ).all()

    scored = []
    for txn in candidates:
        score = _score_candidate(txn, ad)
        if score > 0.10:   # minimum threshold — filters obviously irrelevant transactions
            scored.append({"transaction": txn, "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]


def _score_candidate(txn: BankTransaction, ad: AdPurchase) -> float:
    amount_score  = _score_amount(txn.total_amount, float(ad.expected_amount))
    vendor_score  = _score_vendor(txn.vendor_name, ad.newspaper_name)
    date_score    = _score_date(txn.txn_date, ad.expected_charge_date)

    return (
        WEIGHT_AMOUNT * amount_score +
        WEIGHT_VENDOR * vendor_score +
        WEIGHT_DATE   * date_score
    )


def _score_amount(actual: float, expected: float) -> float:
    if expected == 0:
        return 0.0
    diff_pct = abs(actual - expected) / expected
    if diff_pct == 0:
        return 1.0
    elif diff_pct <= 0.01:   # within 1%
        return 0.90
    elif diff_pct <= 0.05:   # within 5%
        return 0.60
    elif diff_pct <= 0.15:   # within 15%
        return 0.30
    return 0.0


def _score_vendor(txn_vendor: str, ad_newspaper: str) -> float:
    if not txn_vendor or not ad_newspaper:
        return 0.0
    # Normalize
    v1 = txn_vendor.lower().strip()
    v2 = ad_newspaper.lower().strip()

    # Direct substring: "LA TIMES" in "LOS ANGELES TIMES ACH"
    if v2 in v1 or v1 in v2:
        return 1.0

    # Known abbreviation map — extend this as you encounter real BofA strings
    KNOWN_MAPPINGS = {
        "los angeles times": ["la times", "latimes", "l.a. times"],
        "miami herald":      ["herald", "miami herald"],
        "sun sentinel":      ["sentinel", "sun-sentinel"],
        "chicago tribune":   ["tribune", "chi tribune"],
        "new york times":    ["nyt", "ny times"],
        "houston chronicle": ["chronicle", "houston chron"],
        # Add more as you discover BofA's truncated vendor strings
    }
    for canonical, aliases in KNOWN_MAPPINGS.items():
        if canonical in v2 or v2 in canonical:
            for alias in aliases:
                if alias in v1:
                    return 0.90

    # Fuzzy fallback
    ratio = SequenceMatcher(None, v1, v2).ratio()
    return ratio if ratio > 0.5 else 0.0


def _score_date(txn_date: date, expected_date: date) -> float:
    if not txn_date or not expected_date:
        return 0.0
    diff = abs((txn_date - expected_date).days)
    if diff == 0:   return 1.0
    if diff == 1:   return 0.85
    if diff == 2:   return 0.65
    if diff == 3:   return 0.40
    if diff <= 5:   return 0.20
    return 0.0
```

---

## 6. RAG Layer — `confirmed_ctl/matching/rag.py`

ChromaDB collection stores historical confirmation patterns.
After enough confirmed matches accumulate, the system can retrieve
similar past matches to improve candidate ranking.

```python
"""
confirmed_ctl/matching/rag.py

ChromaDB-backed pattern memory for confirmed matches.
Each confirmed match is embedded and stored. At query time,
similar ads retrieve similar past matches for ranking context.
"""
import chromadb
from chromadb.utils import embedding_functions
import json

CHROMA_PATH = "/opt/confirmed-ctl/chroma_db"
COLLECTION  = "confirmed_matches"


def get_collection():
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    ef = embedding_functions.DefaultEmbeddingFunction()
    return client.get_or_create_collection(
        name=COLLECTION,
        embedding_function=ef,
    )


def store_confirmed_match(
    ad_id: int,
    ad_number: str,
    newspaper_name: str,
    expected_amount: float,
    txn_amount: float,
    txn_date: str,
    txn_vendor: str,
    match_method: str,
):
    """
    Called after a human confirms a match. Embeds the pattern for future retrieval.
    """
    col = get_collection()
    doc_text = (
        f"Newspaper: {newspaper_name}. "
        f"Expected amount: {expected_amount}. "
        f"Actual charge: {txn_amount} on {txn_date} from vendor '{txn_vendor}'. "
        f"Matched via: {match_method}."
    )
    col.add(
        documents=[doc_text],
        ids=[f"ad_{ad_id}"],
        metadatas=[{
            "ad_id":          ad_id,
            "ad_number":      ad_number,
            "newspaper_name": newspaper_name,
            "txn_vendor":     txn_vendor,
            "match_method":   match_method,
        }],
    )


def retrieve_similar_patterns(newspaper_name: str, expected_amount: float, n: int = 5):
    """
    Retrieve past confirmed matches similar to this ad.
    Used to boost scoring for vendors/amounts we've seen before.
    """
    col = get_collection()
    query_text = f"Newspaper: {newspaper_name}. Expected amount: {expected_amount}."
    results = col.query(query_texts=[query_text], n_results=n)
    return results.get("metadatas", [[]])[0]
```

---

## 7. Gmail Integration — `confirmed_ctl/gmail/client.py`

```python
"""
confirmed_ctl/gmail/client.py

Search Gmail for threads containing a specific ad number string.
Uses existing Gmail OAuth credentials from your other machine/project.
Scope required: https://www.googleapis.com/auth/gmail.readonly
"""
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import os, json


GMAIL_TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", "/opt/confirmed-ctl/gmail_token.json")
GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_gmail_service():
    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def search_threads_by_ad_number(ad_number: str, max_results: int = 5) -> list[dict]:
    """
    Search Gmail for threads containing the ad number string.
    Returns list of thread summary dicts for display in popup.
    """
    service = get_gmail_service()
    query   = f'"{ad_number}"'     # Exact string match

    result  = service.users().threads().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()

    threads = result.get("threads", [])
    summaries = []

    for thread in threads:
        thread_detail = service.users().threads().get(
            userId="me",
            id=thread["id"],
            format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()

        messages = thread_detail.get("messages", [])
        if not messages:
            continue

        headers = {
            h["name"]: h["value"]
            for h in messages[0].get("payload", {}).get("headers", [])
        }
        summaries.append({
            "thread_id": thread["id"],
            "subject":   headers.get("Subject", "(no subject)"),
            "from":      headers.get("From", ""),
            "date":      headers.get("Date", ""),
            "snippet":   thread_detail.get("snippet", ""),
            "message_count": len(messages),
        })

    return summaries
```

---

## 8. Flask UI Routes — `confirmed_ctl/api/routes.py`

Internal API routes that your existing Flask app calls. Mount these
under `/confirmed-ctl/` or integrate into your existing blueprint structure.

```python
"""
confirmed_ctl/api/routes.py

Flask blueprint exposing endpoints for the Confirmed-CTL popup UI.
Mount in your main Flask app:
    from confirmed_ctl.api.routes import confirmed_ctl_bp
    app.register_blueprint(confirmed_ctl_bp)
"""
from flask import Blueprint, jsonify, request
from datetime import datetime, timezone
from ..qbo.sync import sync_recent_transactions
from ..matching.scorer import get_candidate_transactions
from ..matching.rag import store_confirmed_match, retrieve_similar_patterns
from ..gmail.client import search_threads_by_ad_number
from ..db.models import AdPurchase, BankTransaction, AdConfirmation
from ..db.session import get_db

confirmed_ctl_bp = Blueprint("confirmed_ctl", __name__, url_prefix="/confirmed-ctl")


@confirmed_ctl_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """
    Called by the [Sync Now] button in the Flask UI.
    Mirrors your Stripe sync button pattern.
    """
    body        = request.get_json(silent=True) or {}
    lookback    = int(body.get("lookback_days", 2))

    with get_db() as db:
        summary = sync_recent_transactions(db, lookback_days=lookback)

    return jsonify({"status": "ok", "summary": summary})


@confirmed_ctl_bp.route("/sync/status", methods=["GET"])
def sync_status():
    """Returns last sync run info for UI status display."""
    with get_db() as db:
        from ..db.models import SyncLog
        last = db.query(SyncLog).order_by(SyncLog.synced_at.desc()).first()
        if not last:
            return jsonify({"last_sync": None})
        return jsonify({
            "last_sync":     last.synced_at.isoformat(),
            "txns_fetched":  last.txns_fetched,
            "txns_new":      last.txns_new,
            "auto_matched":  last.auto_matched,
            "errors":        last.errors,
        })


@confirmed_ctl_bp.route("/candidates/<int:ad_id>", methods=["GET"])
def get_candidates(ad_id: int):
    """
    Called when user clicks an ad number in the UI.
    Returns ranked bank transaction candidates + Gmail thread results.
    """
    with get_db() as db:
        ad = db.query(AdPurchase).get(ad_id)
        if not ad:
            return jsonify({"error": "Ad not found"}), 404

        # Bank transaction candidates (ranked)
        candidates = get_candidate_transactions(db, ad)

        # Gmail threads
        gmail_threads = []
        if ad.ad_number:
            try:
                gmail_threads = search_threads_by_ad_number(ad.ad_number)
            except Exception as e:
                gmail_threads = []   # Don't break the popup if Gmail fails

        return jsonify({
            "ad": {
                "id":              ad.id,
                "ad_number":       ad.ad_number,
                "newspaper_name":  ad.newspaper_name,
                "expected_amount": float(ad.expected_amount),
                "run_date":        str(ad.run_date),
                "client_name":     ad.client_name,
            },
            "bank_candidates": [
                {
                    "txn_id":       c["transaction"].id,
                    "qbo_id":       c["transaction"].qbo_id,
                    "txn_date":     str(c["transaction"].txn_date),
                    "amount":       float(c["transaction"].total_amount),
                    "vendor_name":  c["transaction"].vendor_name,
                    "account_name": c["transaction"].account_name,
                    "payment_ref":  c["transaction"].payment_ref_num,
                    "memo":         c["transaction"].private_note,
                    "score":        round(c["score"], 3),
                    "score_pct":    int(c["score"] * 100),
                }
                for c in candidates
            ],
            "gmail_threads": gmail_threads,
        })


@confirmed_ctl_bp.route("/confirm", methods=["POST"])
def confirm_ad():
    """
    Called when user clicks [CONFIRM & CLOSE] in the popup.
    Saves the relationship: ad ↔ bank transaction ↔ Gmail thread.
    """
    body = request.get_json()
    ad_id       = body.get("ad_id")
    txn_id      = body.get("bank_txn_id")       # internal DB id
    thread_id   = body.get("gmail_thread_id")
    thread_subj = body.get("gmail_subject", "")
    confirmed_by = body.get("confirmed_by", "user")

    with get_db() as db:
        ad  = db.query(AdPurchase).get(ad_id)
        txn = db.query(BankTransaction).get(txn_id) if txn_id else None

        if not ad:
            return jsonify({"error": "Ad not found"}), 404

        # Check for existing confirmation (idempotency)
        existing = db.query(AdConfirmation).filter_by(ad_id=ad_id).first()
        if existing:
            return jsonify({"error": "Ad already confirmed", "confirmation_id": existing.id}), 409

        # Create confirmation record
        conf = AdConfirmation(
            ad_id           = ad_id,
            bank_txn_id     = txn.id if txn else None,
            gmail_thread_id = thread_id,
            gmail_subject   = thread_subj,
            confirmed_by    = confirmed_by,
            confirmed_at    = datetime.now(timezone.utc),
            match_method    = "manual",
            match_confidence = "manual",
        )
        db.add(conf)

        # Link transaction back to ad
        if txn:
            txn.confirmed_ad_id = ad_id
            txn.confirmed_at    = conf.confirmed_at

        # Store in RAG for future pattern learning
        if txn:
            store_confirmed_match(
                ad_id          = ad_id,
                ad_number      = ad.ad_number,
                newspaper_name = ad.newspaper_name,
                expected_amount = float(ad.expected_amount),
                txn_amount     = float(txn.total_amount),
                txn_date       = str(txn.txn_date),
                txn_vendor     = txn.vendor_name or "",
                match_method   = "manual",
            )

        db.commit()

        return jsonify({
            "status":          "confirmed",
            "confirmation_id": conf.id,
            "ad_number":       ad.ad_number,
            "txn_qbo_id":      txn.qbo_id if txn else None,
            "gmail_thread_id": thread_id,
        })


@confirmed_ctl_bp.route("/unconfirmed", methods=["GET"])
def list_unconfirmed():
    """
    Returns unconfirmed ads for the report table.
    Replaces or supplements the existing Advise Cash Flow report.
    """
    with get_db() as db:
        # Ads not yet in ad_confirmations
        confirmed_ad_ids = db.query(AdConfirmation.ad_id).subquery()
        ads = db.query(AdPurchase)\
                .filter(AdPurchase.id.notin_(confirmed_ad_ids))\
                .order_by(AdPurchase.run_date.desc())\
                .limit(100)\
                .all()

        return jsonify([{
            "id":              a.id,
            "ad_number":       a.ad_number,
            "client_name":     a.client_name,
            "newspaper_name":  a.newspaper_name,
            "run_date":        str(a.run_date),
            "expected_amount": float(a.expected_amount),
            "status":          "unconfirmed",
        } for a in ads])
```

---

## 9. receipt-ctl — `confirmed_ctl/gmail/receipts.py`

```python
"""
confirmed_ctl/gmail/receipts.py  (receipt-ctl core logic)

Given a Gmail thread ID, download all attachments (PDFs, images)
and save them to the local receipts directory.
Update the ad_confirmations record with the file path.
"""
import os
import base64
from pathlib import Path
from .client import get_gmail_service

RECEIPTS_BASE = os.environ.get("RECEIPTS_BASE_PATH", "/mnt/receipts")


def download_receipt(
    thread_id: str,
    ad_number: str,
    year: str,
    month: str,
) -> list[str]:
    """
    Download all attachments from a Gmail thread.
    Returns list of saved file paths.
    """
    service  = get_gmail_service()
    thread   = service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()

    save_dir = Path(RECEIPTS_BASE) / year / month / ad_number
    save_dir.mkdir(parents=True, exist_ok=True)

    saved_paths = []

    for message in thread.get("messages", []):
        parts = message.get("payload", {}).get("parts", [])
        for part in parts:
            if part.get("filename") and part.get("body", {}).get("attachmentId"):
                attachment = service.users().messages().attachments().get(
                    userId="me",
                    messageId=message["id"],
                    id=part["body"]["attachmentId"],
                ).execute()

                file_data = base64.urlsafe_b64decode(attachment["data"])
                file_name = part["filename"] or f"receipt_{message['id']}.pdf"
                file_path = save_dir / file_name

                with open(file_path, "wb") as f:
                    f.write(file_data)

                saved_paths.append(str(file_path))

    return saved_paths


def process_pending_receipts(db_session) -> dict:
    """
    Batch job: find all confirmed ads with a gmail_thread_id but no receipt_file_path.
    Download receipts for each. Called by cron or CLI.
    """
    from ..db.models import AdConfirmation
    import calendar

    pending = db_session.query(AdConfirmation).filter(
        AdConfirmation.gmail_thread_id != None,
        AdConfirmation.receipt_file_path == None,
    ).all()

    results = {"processed": 0, "errors": []}

    for conf in pending:
        try:
            confirmed_date = conf.confirmed_at
            year  = str(confirmed_date.year)
            month = f"{confirmed_date.month:02d}"

            paths = download_receipt(
                thread_id  = conf.gmail_thread_id,
                ad_number  = conf.ad.ad_number if conf.ad else conf.ad_id,
                year       = year,
                month      = month,
            )

            if paths:
                conf.receipt_file_path = paths[0]           # Primary receipt
                conf.receipt_url       = ",".join(paths)    # All if multiple
                results["processed"] += 1

        except Exception as e:
            results["errors"].append(f"Confirmation {conf.id}: {str(e)}")

    db_session.commit()
    return results
```

---

## 10. CLI Entry Point — `confirmed_ctl/cli.py`

```python
"""
confirmed_ctl/cli.py

CLI for confirmed-ctl daemon.
Usage:
  confirmed-ctl sync [--lookback-days 2]
  confirmed-ctl status
  confirmed-ctl receipts
  confirmed-ctl match --ad-id 1234
"""
import click
from .db.session import get_db
from .qbo.sync import sync_recent_transactions
from .gmail.receipts import process_pending_receipts


@click.group()
def cli():
    """confirmed-ctl — QBO bank sync and ad confirmation tool."""
    pass


@cli.command()
@click.option("--lookback-days", default=2, help="Days to look back in QBO")
@click.option("--no-cdc", is_flag=True, help="Use date query instead of CDC")
def sync(lookback_days, no_cdc):
    """Sync recent QBO transactions to local database."""
    click.echo(f"Syncing last {lookback_days} days from QuickBooks...")
    with get_db() as db:
        summary = sync_recent_transactions(db, lookback_days=lookback_days, use_cdc=not no_cdc)
    click.echo(f"Done: {summary['new']} new, {summary['updated']} updated, "
               f"{summary['fetched']} total fetched.")
    if summary["errors"]:
        click.echo("Errors:")
        for e in summary["errors"]:
            click.echo(f"  {e}")


@cli.command()
def status():
    """Show last sync run info."""
    with get_db() as db:
        from .db.models import SyncLog
        last = db.query(SyncLog).order_by(SyncLog.synced_at.desc()).first()
        if last:
            click.echo(f"Last sync: {last.synced_at}")
            click.echo(f"  Fetched: {last.txns_fetched}, New: {last.txns_new}")
        else:
            click.echo("No sync runs found.")


@cli.command()
def receipts():
    """Download receipts for all confirmed ads that have a Gmail thread ID."""
    click.echo("Processing pending receipts...")
    with get_db() as db:
        result = process_pending_receipts(db)
    click.echo(f"Done: {result['processed']} receipts downloaded.")
    if result["errors"]:
        for e in result["errors"]:
            click.echo(f"  Error: {e}")


@cli.command()
@click.option("--ad-id", required=True, type=int, help="Ad database ID")
def match(ad_id):
    """Show ranked bank transaction candidates for a specific ad."""
    with get_db() as db:
        from .db.models import AdPurchase
        from .matching.scorer import get_candidate_transactions
        ad = db.query(AdPurchase).get(ad_id)
        if not ad:
            click.echo(f"Ad {ad_id} not found.")
            return
        candidates = get_candidate_transactions(db, ad)
        click.echo(f"\nTop candidates for Ad #{ad.ad_number} ({ad.newspaper_name}, "
                   f"${ad.expected_amount}):\n")
        for i, c in enumerate(candidates, 1):
            t = c["transaction"]
            click.echo(f"  {i}. Score {int(c['score']*100)}% | "
                       f"{t.txn_date} | ${t.total_amount} | {t.vendor_name}")


if __name__ == "__main__":
    cli()
```

---

## 11. systemd Unit — `confirmed-ctl.service`

```ini
[Unit]
Description=confirmed-ctl QBO sync daemon
After=network.target postgresql.service

[Service]
Type=simple
User=auto-ops
WorkingDirectory=/opt/confirmed-ctl
EnvironmentFile=/opt/confirmed-ctl/.env
ExecStart=/opt/confirmed-ctl/venv/bin/python -m confirmed_ctl.daemon
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Daemon loop (`confirmed_ctl/daemon.py`)

```python
import time
import logging
from .db.session import get_db
from .qbo.sync import sync_recent_transactions

SYNC_INTERVAL_SECONDS = 3600   # Run every hour; adjustable via env

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("confirmed-ctl.daemon")


def run():
    log.info("confirmed-ctl daemon starting.")
    while True:
        try:
            with get_db() as db:
                summary = sync_recent_transactions(db, lookback_days=2)
            log.info(f"Sync complete: {summary['new']} new, "
                     f"{summary['updated']} updated, "
                     f"{summary['fetched']} fetched.")
        except Exception as e:
            log.error(f"Sync error: {e}")
        time.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
```

---

## 12. `.env.example`

```bash
# QBO credentials (from Intuit Developer Portal)
QBO_CLIENT_ID=AB...
QBO_CLIENT_SECRET=XY...
QBO_REALM_ID=123456789012345
QBO_TOKEN_PATH=/opt/confirmed-ctl/qbo_tokens.json

# Database (your existing Postgres)
DATABASE_URL=postgresql://user:password@localhost:5432/your_db

# Gmail
GMAIL_TOKEN_PATH=/opt/confirmed-ctl/gmail_token.json

# Receipts storage
RECEIPTS_BASE_PATH=/mnt/receipts

# ChromaDB
CHROMA_PATH=/opt/confirmed-ctl/chroma_db

# Sync interval for daemon (seconds)
SYNC_INTERVAL_SECONDS=3600
```

---

## 13. `requirements.txt`

```
# QBO
requests>=2.31
python-quickbooks>=0.9     # Optional ORM layer — use if preferred over raw requests

# Google / Gmail
google-auth>=2.28
google-auth-oauthlib>=1.2
google-api-python-client>=2.120

# Database
sqlalchemy>=2.0
alembic>=1.13
psycopg2-binary>=2.9

# Flask (existing app)
flask>=3.0

# RAG / ChromaDB
chromadb>=0.5

# CLI
click>=8.1

# Utils
python-dotenv>=1.0
```

---

## 14. Cursor Implementation Checklist

Hand Cursor this list in order:

- [ ] Initialize repo at `/opt/confirmed-ctl/` with structure from Section 2
- [ ] Create `.env` from `.env.example` — populate from existing QBO tokens on `prx.auto-ops.net`
- [ ] `db/models.py` — define `BankTransaction`, `AdConfirmation`, `SyncLog` SQLAlchemy models
- [ ] `db/session.py` — context manager connecting to existing Postgres via `DATABASE_URL`
- [ ] Run Alembic migration to add tables to existing DB
- [ ] `qbo/client.py` — token manager with auto-refresh + `qbo_get()` (see previous spec for full code)
- [ ] `qbo/sync.py` — `sync_recent_transactions()` with CDC + fallback date query
- [ ] `matching/scorer.py` — candidate ranking; **calibrate `KNOWN_MAPPINGS` to actual BofA vendor strings seen in your QBO transactions**
- [ ] `matching/rag.py` — ChromaDB collection; test `store_confirmed_match()` + `retrieve_similar_patterns()`
- [ ] `gmail/client.py` — wire in existing Gmail OAuth token from other project
- [ ] `gmail/receipts.py` — `download_receipt()` + `process_pending_receipts()`
- [ ] `api/routes.py` — Flask blueprint; register in main app
- [ ] `cli.py` — Click CLI; install as `confirmed-ctl` via `pip install -e .` or `setup.py`
- [ ] `daemon.py` + systemd unit file — install and enable service
- [ ] **Flask UI popup** — React/Jinja2 component on ad number click: calls `/confirmed-ctl/candidates/{ad_id}`, renders ranked transactions + Gmail threads, posts to `/confirmed-ctl/confirm`
- [ ] Add [Sync Now] button to existing Advise Cash Flow report page calling `POST /confirmed-ctl/sync`
- [ ] Add sync status indicator (last run time, transaction count) to report header
- [ ] Cron fallback: `30 7 * * * auto-ops /opt/confirmed-ctl/venv/bin/confirmed-ctl sync`
- [ ] Receipt-ctl cron: `0 8 * * * auto-ops /opt/confirmed-ctl/venv/bin/confirmed-ctl receipts`
- [ ] Test end-to-end: sync → open popup for one unconfirmed ad → confirm against a real transaction → verify `ad_confirmations` row + `bank_transactions.confirmed_ad_id`

---

## 15. Known Limitations & Watchpoints

| Issue | Mitigation |
|---|---|
| BofA vendor string truncation | Build `KNOWN_MAPPINGS` incrementally; run `confirmed-ctl sync` once and inspect real `vendor_name` values in DB before tuning scorer |
| QBO bank feed lag (overnight) | 2-day lookback + CDC handles; daemon hourly refresh catches intraday posts |
| Refresh token rotation every 24h | `qbo/client.py` always saves new token on every refresh — do not share token file with other QBO modules |
| Gmail thread search latency | Run async in popup (non-blocking); show bank candidates immediately, Gmail results load in parallel |
| ChromaDB cold start | RAG improves with volume; for first weeks, scorer.py alone is sufficient — RAG layer can be enabled once 50+ confirmed matches are stored |
| Receipt attachments format | Some newspapers send PDF, some send HTML emails — `receipts.py` saves all attachment types; filter by MIME type if needed |

---

*Document version: 2.0 — confirmed-ctl full system architecture*
*Prepared for Cursor handoff | June 18, 2026 | `prx.auto-ops.net`*
