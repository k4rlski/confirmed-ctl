"""confirmed_ctl/gmail/client.py

Search Gmail for threads containing a specific ad number string.
Uses existing Gmail OAuth credentials from your other machine/project.
Scope required: https://www.googleapis.com/auth/gmail.readonly

The Google client libraries are imported lazily so the rest of the package
imports without them installed.
"""

from __future__ import annotations

from .. import settings

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = settings.GMAIL_TOKEN_PATH
    creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


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
