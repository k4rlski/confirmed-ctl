"""confirmed_ctl/gmail/client.py

Read-only Gmail access for confirmed-ctl.

Authentication uses a Google **service account** with domain-wide delegation
(modelled on the ``gmail-ctl`` tool): the service-account JSON key at
``settings.GMAIL_TOKEN_PATH`` is loaded and ``.with_subject()`` impersonates
``settings.GMAIL_IMPERSONATE`` (default ``karl@perm-ads.com``). The only scope
requested is ``gmail.readonly`` — this client NEVER calls modify/trash/delete.

The Google client libraries are imported lazily so the rest of the package
imports without them installed (tests never touch live Gmail).
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta

from .. import settings

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_gmail_service():
    """Build a read-only Gmail API service via service-account delegation.

    Loads the service-account key from ``settings.GMAIL_TOKEN_PATH`` and
    impersonates ``settings.GMAIL_IMPERSONATE``. Secrets live on disk (the key
    file), never in source.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        settings.GMAIL_TOKEN_PATH, scopes=GMAIL_SCOPES
    )
    delegated = creds.with_subject(settings.GMAIL_IMPERSONATE)
    return build("gmail", "v1", credentials=delegated)


def search_messages(
    service, query: str, max_results: int = 2000
) -> Iterator[dict]:
    """Yield message stubs (``{id, threadId}``) matching ``query``.

    Paginates automatically through ``users().messages().list``. Read-only.

    ``includeSpamTrash=True`` is set **defensively**: the default mailbox
    ``karl@perm-ads.com`` keeps BofA alerts in its INBOX, but at ``info@perm-ads.com``
    (an override) a filter auto-trashes them, and ``users().messages().list``
    excludes Trash/Spam by default — so without this flag that mailbox silently
    returns nothing. Read-only access to Trash does not modify or restore anything.
    """
    page_token = None
    fetched = 0
    while fetched < max_results:
        batch = min(500, max_results - fetched)
        params = {
            "userId": "me",
            "q": query,
            "maxResults": batch,
            "includeSpamTrash": True,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = service.users().messages().list(**params).execute()
        messages = resp.get("messages", [])
        for m in messages:
            yield m
            fetched += 1
        page_token = resp.get("nextPageToken")
        if not page_token or not messages:
            break
        time.sleep(0.1)  # rate-limit courtesy


def get_message(
    service,
    message_id: str,
    fmt: str = "full",
    metadata_headers: list[str] | None = None,
) -> dict:
    """Fetch a message (headers + body). Read-only.

    ``metadata_headers`` (only meaningful with ``fmt="metadata"``) restricts the
    returned headers to the named ones (e.g. ``["From"]``), which makes a
    header-only harvest markedly cheaper than pulling every header/body.
    """
    params = {"userId": "me", "id": message_id, "format": fmt}
    if metadata_headers:
        params["metadataHeaders"] = metadata_headers
    return service.users().messages().get(**params).execute()


def get_headers(message: dict) -> dict:
    """Return a lower-cased header-name -> value dict for a message."""
    headers = {}
    for h in message.get("payload", {}).get("headers", []):
        headers[h["name"].lower()] = h["value"]
    return headers


def get_html_body(message: dict) -> str:
    """Return the RAW ``text/html`` body of a (possibly multipart) message.

    BofA alerts are HTML-only; the email-scan parser wants the raw HTML (not a
    flattened text rendering) so it can pair the two-column data-table cells
    (label ``<td>`` -> value ``<td>``) with BeautifulSoup. Falls back to any
    ``text/plain`` part when no HTML part exists. Read-only.
    """
    html = _extract_part(message.get("payload", {}), "text/html")
    if html:
        return html
    return _extract_part(message.get("payload", {}), "text/plain")


def get_body_text(message: dict) -> str:
    """Extract a PLAIN-TEXT body from a (possibly multipart) message.

    BofA transaction alerts are **HTML-only** (no ``text/plain`` part), so when
    only ``text/html`` is present it is converted to plain text (tags/entities
    stripped, block elements turned into line breaks) BEFORE it is returned. This
    lets the email-scan parser do label-based extraction on the rendered text
    without caring about HTML structure. ``text/plain`` is still preferred when a
    message actually provides it. Recurses through multipart containers.
    """
    payload = message.get("payload", {})
    text = _extract_part(payload, "text/plain")
    if text:
        return text
    html = _extract_part(payload, "text/html")
    if html:
        return html_to_text(html)
    return ""


def html_to_text(html: str) -> str:
    """Convert an HTML fragment to normalized plain text.

    Strips ``<script>``/``<style>``, turns block-level elements into line breaks,
    removes remaining tags, and unescapes HTML entities. Prefers BeautifulSoup
    when available; falls back to a dependency-free regex pass otherwise. Blank
    lines are collapsed and each line is stripped so labels sit at line starts.
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text("\n")
    except Exception:  # pragma: no cover - bs4 missing: regex fallback
        import html as _htmllib

        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|tr|td|th|table|h[1-6]|li|ul|ol)>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = _htmllib.unescape(text)

    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _decode_body(body: dict) -> str:
    data = body.get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def _extract_part(payload: dict, want_mime: str) -> str:
    mime = payload.get("mimeType", "")
    if mime == want_mime:
        return _decode_body(payload.get("body", {}))
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _extract_part(part, want_mime)
            if text:
                return text
    return ""


def _coerce_charge_date(value: date | str | None) -> date | None:
    """Best-effort coerce a charge-date value into a ``date`` (or ``None``).

    Accepts a ``date``/``datetime`` (``datetime`` is narrowed to its ``.date()``)
    or a string in a few common CRM/ISO formats. Returns ``None`` when the value
    is missing, blank, or unparseable — the caller then simply omits the
    date-windowed paper-name fallback clause.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _list_thread_ids(service, query: str, max_results: int) -> list[str]:
    """Return thread ids matching ``query`` (read-only ``threads().list``).

    ``includeSpamTrash=True`` mirrors :func:`search_messages` — ad/receipt
    threads can be auto-filtered to Trash/Spam and would otherwise be invisible.
    """
    result = service.users().threads().list(
        userId="me",
        q=query,
        maxResults=max_results,
        includeSpamTrash=True,
    ).execute()
    return [t["id"] for t in result.get("threads", [])]


def _summarize_thread(service, thread_id: str, stripped_adnum: str) -> dict | None:
    """Build a display summary dict for one thread (read-only metadata fetch).

    ``matched_by`` is a best-effort classification: ``"ad_number"`` when the
    stripped ad number appears in the first message's subject or the thread
    snippet, else ``"paper_name"``. ``gmail_url`` is an account-index-agnostic
    deep link using ``settings.GMAIL_IMPERSONATE`` so it opens regardless of
    which Google account slot the viewer has signed in.
    """
    thread_detail = service.users().threads().get(
        userId="me",
        id=thread_id,
        format="metadata",
        metadataHeaders=["Subject", "From", "Date"],
    ).execute()

    messages = thread_detail.get("messages", [])
    if not messages:
        return None

    headers = {
        h["name"]: h["value"]
        for h in messages[0].get("payload", {}).get("headers", [])
    }
    subject = headers.get("Subject", "(no subject)")
    snippet = thread_detail.get("snippet", "")
    matched_by = (
        "ad_number"
        if stripped_adnum and (stripped_adnum in subject or stripped_adnum in snippet)
        else "paper_name"
    )
    return {
        "thread_id": thread_id,
        "subject": subject,
        "from": headers.get("From", ""),
        "date": headers.get("Date", ""),
        "snippet": snippet,
        "message_count": len(messages),
        "gmail_url": (
            f"https://mail.google.com/mail/?authuser={settings.GMAIL_IMPERSONATE}"
            f"#all/{thread_id}"
        ),
        "matched_by": matched_by,
    }


def search_threads_by_ad_number(
    ad_number: str,
    newspaper_name: str | None = None,
    charge_date: date | str | None = None,
    max_results: int = 8,
) -> list[dict]:
    """Search Gmail for threads relevant to a CRM ad, for the /candidates popup.

    Read-only (``threads().list`` + ``threads().get`` metadata only).

    Query construction:

    - Primary clause is the exact-string ad number (``.strip()``ed).
    - If ``newspaper_name`` AND a parseable ``charge_date`` are both provided, a
      DATE-WINDOWED paper-name fallback is also searched
      (``after:charge-14d before:charge+7d``) so automated receipts that omit the
      ad number still surface. Without a charge date the unbounded paper-name
      clause is deliberately skipped (it would flood the popup).

    Results from the two searches are merged and de-duplicated by ``thread_id``,
    ranked ad#-matched-first then paper-name-only, and capped at ``max_results``.
    Each summary carries the existing keys plus ``gmail_url`` and ``matched_by``.

    Raises ``ValueError`` when ``ad_number`` is blank/whitespace — the caller can
    distinguish this "no ad number on record" case from "searched, found nothing"
    (an empty list). ``includeSpamTrash=True`` is kept throughout.
    """
    stripped = (ad_number or "").strip()
    if not stripped:
        raise ValueError("blank ad_number: refusing to search Gmail")

    service = get_gmail_service()

    ordered_ids: list[str] = []
    seen: set[str] = set()

    for tid in _list_thread_ids(service, f'"{stripped}"', max_results):
        if tid not in seen:
            seen.add(tid)
            ordered_ids.append(tid)

    paper = (newspaper_name or "").strip()
    parsed_charge = _coerce_charge_date(charge_date)
    if paper and parsed_charge is not None:
        after = parsed_charge - timedelta(days=14)
        before = parsed_charge + timedelta(days=7)
        paper_query = (
            f'"{paper}" after:{after:%Y/%m/%d} before:{before:%Y/%m/%d}'
        )
        for tid in _list_thread_ids(service, paper_query, max_results):
            if tid not in seen:
                seen.add(tid)
                ordered_ids.append(tid)

    summaries: list[dict] = []
    for tid in ordered_ids[:max_results]:
        summary = _summarize_thread(service, tid, stripped)
        if summary:
            summaries.append(summary)

    return summaries
