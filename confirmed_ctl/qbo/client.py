"""QBO OAuth token manager + thin REST helper.

Tokens are stored as JSON at ``QBO_TOKEN_PATH``::

    {
      "access_token":  "...",
      "refresh_token": "...",
      "expires_at":    "2026-06-18T12:00:00+00:00"   # ISO8601, optional
    }

The access token is refreshed automatically when expired (or on a 401). Every
refresh rewrites the token file — QBO rotates the refresh token roughly every
24h, so this file must not be shared with other QBO modules.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .. import settings

logger = logging.getLogger("confirmed-ctl.qbo.client")

_REFRESH_SKEW = timedelta(seconds=120)


class QboError(RuntimeError):
    """Raised for unrecoverable QBO API/auth failures."""


def _token_path() -> Path:
    return Path(settings.QBO_TOKEN_PATH)


def _load_tokens() -> dict[str, Any]:
    path = _token_path()
    if not path.is_file():
        raise QboError(
            f"QBO token file not found at {path}. Complete the Intuit OAuth flow "
            "and write access_token/refresh_token there."
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save_tokens(tokens: dict[str, Any]) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(tokens, fh, indent=2)
    tmp.replace(path)


def _is_expired(tokens: dict[str, Any]) -> bool:
    expires_at = tokens.get("expires_at")
    if not expires_at:
        return True  # unknown expiry — refresh to be safe
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= (exp - _REFRESH_SKEW)


def _refresh(tokens: dict[str, Any]) -> dict[str, Any]:
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise QboError("No refresh_token in QBO token file; re-authorize the app.")
    resp = requests.post(
        settings.QBO_TOKEN_ENDPOINT,
        auth=(settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    if resp.status_code != 200:
        raise QboError(f"QBO token refresh failed ({resp.status_code}): {resp.text}")
    payload = resp.json()
    expires_in = int(payload.get("expires_in", 3600))
    new_tokens = {
        "access_token": payload["access_token"],
        # QBO returns a rotated refresh token; fall back to the old one if absent.
        "refresh_token": payload.get("refresh_token", refresh_token),
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat(),
    }
    _save_tokens(new_tokens)
    logger.info("QBO access token refreshed.")
    return new_tokens


def _access_token() -> str:
    tokens = _load_tokens()
    if _is_expired(tokens):
        tokens = _refresh(tokens)
    return tokens["access_token"]


def _company_url(endpoint: str) -> str:
    realm = settings.QBO_REALM_ID
    if not realm:
        raise QboError("QBO_REALM_ID is not configured.")
    base = settings.QBO_API_BASE_URL.rstrip("/")
    return f"{base}/v3/company/{realm}/{endpoint.lstrip('/')}"


def qbo_get(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET a QBO endpoint (e.g. ``query`` or ``cdc``) and return parsed JSON.

    Automatically refreshes the token once and retries on a 401.
    """
    params = dict(params or {})
    params.setdefault("minorversion", settings.QBO_MINOR_VERSION)
    url = _company_url(endpoint)

    for attempt in (1, 2):
        token = _access_token()
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            timeout=60,
        )
        if resp.status_code == 401 and attempt == 1:
            logger.info("QBO returned 401; forcing token refresh and retrying.")
            _refresh(_load_tokens())
            continue
        if resp.status_code != 200:
            raise QboError(f"QBO GET {endpoint} failed ({resp.status_code}): {resp.text}")
        return resp.json()
    raise QboError(f"QBO GET {endpoint} failed after token refresh.")
