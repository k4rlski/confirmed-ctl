"""confirmed_ctl/gmail/client.py

Read-only Gmail access for confirmed-ctl.

Authentication uses a Google **service account** with domain-wide delegation
(modelled on the ``gmail-ctl`` tool): the service-account JSON key at
``settings.GMAIL_TOKEN_PATH`` is loaded and ``.with_subject()`` impersonates
``settings.GMAIL_IMPERSONATE`` (``info@perm-ads.com``). The only scope requested
is ``gmail.readonly`` — this client NEVER calls modify/trash/delete.

The Google client libraries are imported lazily so the rest of the package
imports without them installed (tests never touch live Gmail).
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Iterator

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

    ``includeSpamTrash=True`` is REQUIRED: BofA transaction alerts to
    ``info@perm-ads.com`` are auto-filtered into **Trash**, which
    ``users().messages().list`` excludes by default — so without this flag the
    scan silently returns nothing. Read-only access to Trash does not modify or
    restore anything.
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


def get_message(service, message_id: str, fmt: str = "full") -> dict:
    """Fetch a full message (headers + body). Read-only."""
    return (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format=fmt)
        .execute()
    )


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


def search_threads_by_ad_number(ad_number: str, max_results: int = 5) -> list[dict]:
    """
    Search Gmail for threads containing the ad number string.
    Returns list of thread summary dicts for display in popup.
    """
    service = get_gmail_service()
    query = f'"{ad_number}"'  # Exact string match

    result = service.users().threads().list(
        userId="me",
        q=query,
        maxResults=max_results,
        includeSpamTrash=True,  # BofA/ad threads can be auto-filtered to Trash
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
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "snippet": thread_detail.get("snippet", ""),
            "message_count": len(messages),
        })

    return summaries
