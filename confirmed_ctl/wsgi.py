"""confirmed_ctl/wsgi.py

WSGI entrypoint for the confirmed-ctl HTTP API.

This wraps the (otherwise unmounted, auth-free) ``confirmed_ctl_bp`` blueprint in
a real Flask app and adds a bearer-token authentication layer so the service can
run on fang and be reached from claw/MARS over an SSH tunnel.

Serve with:
    gunicorn confirmed_ctl.wsgi:app

The blueprint itself stays auth-free (its unit tests mount a bare Flask app);
all authentication lives here in ``create_app`` via ``before_request``.
"""

from __future__ import annotations

import hmac
import logging

from flask import Flask, jsonify, request

from . import settings
from .api.routes import confirmed_ctl_bp

logger = logging.getLogger(__name__)

# Endpoints exempt from the bearer-token check. ``/healthz`` must be reachable
# without the secret so the tunnel/service can be health-probed — this holds in
# EVERY auth mode (fail-open, authenticated, and fail-closed).
_AUTH_EXEMPT_PATHS = frozenset({"/healthz"})

_UNAUTH_WARNING = (
    "CONFIRMED_CTL_API_TOKEN unset — serving UNAUTHENTICATED "
    "(fail-open). Set CONFIRMED_CTL_API_TOKEN, or CONFIRMED_CTL_REQUIRE_AUTH=1 "
    "to fail closed."
)


def create_app() -> Flask:
    """Build the Flask app: blueprint + health check + bearer-token guard."""
    app = Flask(__name__)
    app.register_blueprint(confirmed_ctl_bp)

    # Loud, once-at-startup warning when we are about to serve unauthenticated.
    startup_token = (settings.API_TOKEN or "").strip()
    if not startup_token and not settings.REQUIRE_AUTH:
        logger.warning(_UNAUTH_WARNING)

    @app.get("/healthz")
    def healthz():
        """Unauthenticated liveness probe (exempt from the token guard)."""
        return jsonify({"status": "ok"}), 200

    @app.before_request
    def _require_bearer_token():
        # Health probe never requires the secret, in any auth mode.
        if request.path in _AUTH_EXEMPT_PATHS:
            return None

        # Read the token at request time (not import time) so tests can
        # monkeypatch ``settings.API_TOKEN`` and the fang service picks up its
        # EnvironmentFile value. A whitespace-only token is treated as unset.
        token = (settings.API_TOKEN or "").strip()

        if not token:
            # Fail CLOSED: REQUIRE_AUTH demands a token; refuse to serve rather
            # than silently exposing the API. /healthz already returned above.
            if settings.REQUIRE_AUTH:
                return jsonify({"error": "auth_required_but_unset"}), 503
            # Fail OPEN (dev/test/unconfigured): allow through, but shout about it.
            logger.warning(_UNAUTH_WARNING)
            return None

        auth_header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        provided = auth_header[len(prefix):] if auth_header.startswith(prefix) else ""
        if not provided:
            return jsonify({"error": "unauthorized"}), 401

        # Constant-time comparison to avoid leaking the token via timing. Compare
        # as bytes so a non-ASCII Authorization value yields a clean 401 mismatch
        # rather than a 500; the try/except is a defensive backstop.
        try:
            authorized = hmac.compare_digest(
                provided.encode("utf-8"), token.encode("utf-8")
            )
        except TypeError:
            authorized = False
        if not authorized:
            return jsonify({"error": "unauthorized"}), 401

        return None

    return app


# Module-level app so ``gunicorn confirmed_ctl.wsgi:app`` works.
app = create_app()
