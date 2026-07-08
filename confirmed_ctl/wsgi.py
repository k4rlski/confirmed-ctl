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

from flask import Flask, jsonify, request

from . import settings
from .api.routes import confirmed_ctl_bp

# Endpoints exempt from the bearer-token check. ``/healthz`` must be reachable
# without the secret so the tunnel/service can be health-probed.
_AUTH_EXEMPT_PATHS = frozenset({"/healthz"})


def create_app() -> Flask:
    """Build the Flask app: blueprint + health check + bearer-token guard."""
    app = Flask(__name__)
    app.register_blueprint(confirmed_ctl_bp)

    @app.get("/healthz")
    def healthz():
        """Unauthenticated liveness probe (exempt from the token guard)."""
        return jsonify({"status": "ok"}), 200

    @app.before_request
    def _require_bearer_token():
        # Read the token at request time (not import time) so tests can
        # monkeypatch ``settings.API_TOKEN`` and the fang service picks up its
        # EnvironmentFile value.
        token = settings.API_TOKEN

        # Fail-open when unset: an empty token means dev/test/unconfigured, so
        # allow every request through. The fang service ALWAYS sets
        # CONFIRMED_CTL_API_TOKEN, so production is authenticated.
        if not token:
            return None

        # Health probe never requires the secret.
        if request.path in _AUTH_EXEMPT_PATHS:
            return None

        auth_header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        provided = auth_header[len(prefix):] if auth_header.startswith(prefix) else ""

        # Constant-time comparison to avoid leaking the token via timing.
        if not (provided and hmac.compare_digest(provided, token)):
            return jsonify({"error": "unauthorized"}), 401

        return None

    return app


# Module-level app so ``gunicorn confirmed_ctl.wsgi:app`` works.
app = create_app()
