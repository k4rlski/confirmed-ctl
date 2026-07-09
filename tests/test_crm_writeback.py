"""Tests for the CRM write-back (confirmed_ctl.crm.client.update_ad_clearance)
and the /confirm wiring that calls it.

NO live CRM, NO live DB: the pymysql connection and the Postgres session are
replaced by in-memory fakes that record every statement / call. We assert:

- update_ad_clearance issues EXACTLY ONE UPDATE on t_e_s_t_p_e_r_m touching only
  the 4 allowlisted columns, param-bound (with ad_crm_id and '["Done"]').
- with the gate OFF it raises CrmWriteDisabled and NEVER connects/writes.
- /confirm (gate ON, write mocked) passes a correctly-formatted trxstring
  (TAB + signed amount), the /u/1/#search URL, and a YYYY-MM-DD date; and does
  NOT commit Postgres when the CRM write raises (rollback, no AdConfirmation).
- /confirm (gate OFF) reports crm_write: "disabled" and still writes Postgres.
- the trxstring formatter emits the signed amount ('-$2,000.00' style) + a TAB.
"""

import logging
import re
from contextlib import contextmanager
from datetime import date

import pytest

from confirmed_ctl.crm import client as crm
from confirmed_ctl.db.models import AdConfirmation, BankTransaction

flask = pytest.importorskip("flask")

from confirmed_ctl.api import routes  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeCursor:
    """Records executed (sql, params); usable as a context manager.

    ``rowcount`` mimics pymysql with ``CLIENT.FOUND_ROWS``: after the UPDATE it
    reports MATCHED rows (default 1 = the id matched). Set to 0 to simulate a
    bad/stale ad_crm_id that matches no CRM row.
    """

    def __init__(self, rowcount=1):
        self.executed = []
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class FakeConn:
    def __init__(self, rowcount=1):
        self.cur = FakeCursor(rowcount=rowcount)
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _txn():
    return BankTransaction(
        id=1,
        source="email-scan",
        source_txn_id="msg-123",
        txn_date=date(2026, 6, 26),
        total_amount=-2226.94,
        payment_type="PURCH W/O PIN",
        payment_ref_num="5723",
        vendor_name="LA TIMES MEDIA GR",
        private_note="BofA alert (SCHEMA-CARD)",
    )


class _ConfQuery:
    def __init__(self, existing):
        self._existing = existing

    def filter_by(self, **kwargs):
        return self

    def first(self):
        return self._existing


class FakeSession:
    """Minimal session covering the /confirm code path."""

    def __init__(self, txn=None, existing=None, commit_raises=False):
        self._txn = txn
        self._existing = existing
        self._commit_raises = commit_raises
        self.added = []
        self.committed = False
        self.rolled_back = False

    def get(self, model, ident):
        return self._txn

    def query(self, *args, **kwargs):
        return _ConfQuery(self._existing)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        if self._commit_raises:
            raise RuntimeError("psycopg2: could not commit (connection lost)")
        self.committed = True

    def rollback(self):
        self.rolled_back = True


@pytest.fixture
def client():
    app = flask.Flask(__name__)
    app.register_blueprint(routes.confirmed_ctl_bp)
    return app.test_client()


def _configure_crm_db(monkeypatch):
    monkeypatch.setattr(crm.settings, "CRM_DB_HOST", "permtrak.com")
    monkeypatch.setattr(crm.settings, "CRM_DB_USER", "permtrak2_crm")
    monkeypatch.setattr(crm.settings, "CRM_DB_PASS", "x")
    monkeypatch.setattr(crm.settings, "CRM_DB_NAME", "permtrak2_crm")


def _patch_db(monkeypatch, session):
    @contextmanager
    def fake_get_db():
        yield session

    monkeypatch.setattr(routes, "get_db", fake_get_db)


# --------------------------------------------------------------------------- #
# trxstring / amount / url formatters
# --------------------------------------------------------------------------- #
def test_format_signed_amount_debit_and_credit():
    assert routes._format_signed_amount(-2226.94) == "-$2,226.94"
    assert routes._format_signed_amount(-2000) == "-$2,000.00"
    assert routes._format_signed_amount(1368.0) == "$1,368.00"
    assert routes._format_signed_amount(None) == ""


def test_build_trxstring_has_tab_and_signed_amount():
    trx = routes._build_trxstring(_txn())
    # Exactly one TAB separating the memo composite from the signed amount.
    assert trx.count("\t") == 1
    memo, amount = trx.split("\t")
    assert amount == "-$2,226.94"
    # Richest composite from the model: payment type, vendor, ON MM/DD, Debit.
    assert memo == "PURCH W/O PIN LA TIMES MEDIA GR ON 06/26 Debit"


def test_build_gmail_url_strips_ad_number():
    url = routes._build_gmail_url("8021354  ", "FMfcgzThreadId")
    assert url == "https://mail.google.com/mail/u/1/#search/8021354/FMfcgzThreadId"


def test_build_gmail_url_empty_without_thread():
    assert routes._build_gmail_url("8021354", "") == ""
    assert routes._build_gmail_url("8021354", None) == ""


# --------------------------------------------------------------------------- #
# update_ad_clearance — the single gated, allowlisted write
# --------------------------------------------------------------------------- #
def test_update_ad_clearance_single_allowlisted_update(monkeypatch):
    monkeypatch.setattr(crm.settings, "CRM_WRITE_ENABLED", True)
    _configure_crm_db(monkeypatch)
    conn = FakeConn()
    monkeypatch.setattr(crm, "_connect_write", lambda: conn)

    crm.update_ad_clearance(
        ad_crm_id="6a343d2127bb55b5a",
        trxstring="PURCH W/O PIN LA TIMES MEDIA GR ON 06/26 Debit\t-$2,226.94",
        urlgmailadconfirm="https://mail.google.com/mail/u/1/#search/8021354/FMfcgz",
        datepaid=date(2026, 6, 26),
    )

    # EXACTLY ONE statement, and it is the UPDATE.
    assert len(conn.cur.executed) == 1
    sql, params = conn.cur.executed[0]
    assert sql.strip().upper().startswith("UPDATE T_E_S_T_P_E_R_M")

    # Only the 4 allowlisted columns are set — and nothing else.
    for col in ("statclearancenews", "trxstring", "urlgmailadconfirm", "datepaidnews"):
        assert f"{col}=%s" in sql
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert set_clause.count("=%s") == 4  # no 5th column ever
    assert "WHERE id=%s" in sql
    # 4 SET binds + 1 WHERE bind, all parameterized (never interpolated).
    assert sql.count("%s") == 5

    # statclearancenews is the JSON multi-enum literal '["Done"]' (not 'Done').
    assert params[0] == '["Done"]'
    assert '["Done"]' in params
    # ad_crm_id is bound (the WHERE id value), never string-interpolated.
    assert params[-1] == "6a343d2127bb55b5a"
    assert "6a343d2127bb55b5a" not in sql
    # date formatted YYYY-MM-DD.
    assert params[3] == "2026-06-26"

    assert conn.committed is True
    assert conn.closed is True


def test_update_ad_clearance_disabled_never_connects(monkeypatch):
    monkeypatch.setattr(crm.settings, "CRM_WRITE_ENABLED", False)

    def _boom():  # pragma: no cover - must never be called
        raise AssertionError("must not connect when write is disabled")

    monkeypatch.setattr(crm, "_connect", _boom)

    with pytest.raises(crm.CrmWriteDisabled):
        crm.update_ad_clearance(
            ad_crm_id="REC1",
            trxstring="x\t-$1.00",
            urlgmailadconfirm="",
            datepaid=date(2026, 6, 26),
        )


def test_update_ad_clearance_empty_date_binds_null(monkeypatch):
    monkeypatch.setattr(crm.settings, "CRM_WRITE_ENABLED", True)
    _configure_crm_db(monkeypatch)
    conn = FakeConn()
    monkeypatch.setattr(crm, "_connect_write", lambda: conn)

    crm.update_ad_clearance("REC1", "memo\t-$1.00", "", datepaid="")
    _sql, params = conn.cur.executed[0]
    assert params[3] is None


# --------------------------------------------------------------------------- #
# /confirm wiring
# --------------------------------------------------------------------------- #
def _confirm_body(**overrides):
    body = {
        "ad_crm_id": "6a343d2127bb55b5a",
        "ad_number": "8021354  ",  # trailing spaces from CRM adnumbernews
        "bank_txn_id": 1,
        "gmail_thread_id": "FMfcgzThreadId",
        "gmail_subject": "Ad confirmation",
        "confirmed_by": "tester",
    }
    body.update(overrides)
    return body


def test_confirm_disabled_writes_postgres_only(client, monkeypatch):
    monkeypatch.setattr(routes.settings, "CRM_WRITE_ENABLED", False)
    monkeypatch.setattr(routes, "store_confirmed_match", lambda **kw: None)
    session = FakeSession(txn=_txn(), existing=None)
    _patch_db(monkeypatch, session)

    # If the write were attempted it would raise (gate off) — assert it is not.
    def _must_not_write(**kwargs):  # pragma: no cover
        raise AssertionError("update_ad_clearance must not be called when disabled")

    monkeypatch.setattr(routes.crm_client, "update_ad_clearance", _must_not_write)

    resp = client.post("/confirmed-ctl/confirm", json=_confirm_body())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["crm_write"] == "disabled"
    assert data["crm_values"]["statclearancenews"] == "Done"
    # Postgres still written.
    assert session.committed is True
    assert any(isinstance(o, AdConfirmation) for o in session.added)


def test_confirm_enabled_calls_update_with_verified_formats(client, monkeypatch):
    monkeypatch.setattr(routes.settings, "CRM_WRITE_ENABLED", True)
    monkeypatch.setattr(routes, "store_confirmed_match", lambda **kw: None)
    session = FakeSession(txn=_txn(), existing=None)
    _patch_db(monkeypatch, session)

    calls = {}

    def _fake_write(ad_crm_id, trxstring, urlgmailadconfirm, datepaid):
        calls.update(
            ad_crm_id=ad_crm_id,
            trxstring=trxstring,
            urlgmailadconfirm=urlgmailadconfirm,
            datepaid=datepaid,
        )

    monkeypatch.setattr(routes.crm_client, "update_ad_clearance", _fake_write)

    resp = client.post("/confirmed-ctl/confirm", json=_confirm_body())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["crm_write"] == "written"

    # trxstring: TAB present + signed amount.
    assert "\t" in calls["trxstring"]
    assert calls["trxstring"].endswith("\t-$2,226.94")
    # urlgmailadconfirm: /u/1/#search deep link with stripped ad number.
    assert calls["urlgmailadconfirm"] == (
        "https://mail.google.com/mail/u/1/#search/8021354/FMfcgzThreadId"
    )
    # datepaid: YYYY-MM-DD.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", calls["datepaid"])
    assert calls["datepaid"] == "2026-06-26"
    assert calls["ad_crm_id"] == "6a343d2127bb55b5a"

    # Postgres committed after a successful CRM write.
    assert session.committed is True


def test_confirm_crm_write_failure_rolls_back_no_postgres(client, monkeypatch):
    monkeypatch.setattr(routes.settings, "CRM_WRITE_ENABLED", True)
    monkeypatch.setattr(routes, "store_confirmed_match", lambda **kw: None)
    session = FakeSession(txn=_txn(), existing=None)
    _patch_db(monkeypatch, session)

    def _boom(**kwargs):
        raise RuntimeError("pymysql: (2003) Can't connect to MySQL server")

    monkeypatch.setattr(routes.crm_client, "update_ad_clearance", _boom)

    resp = client.post("/confirmed-ctl/confirm", json=_confirm_body())
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "crm_write_failed"
    # No raw pymysql detail leaked to the client.
    assert "pymysql" not in body["detail"]
    # Postgres NOT committed; the session was rolled back and no confirmation
    # was persisted (cleanly retryable).
    assert session.committed is False
    assert session.rolled_back is True
    assert not any(isinstance(o, AdConfirmation) for o in session.added)


def test_confirm_enabled_no_txn_skips_write(client, monkeypatch):
    monkeypatch.setattr(routes.settings, "CRM_WRITE_ENABLED", True)
    monkeypatch.setattr(routes, "store_confirmed_match", lambda **kw: None)
    session = FakeSession(txn=None, existing=None)
    _patch_db(monkeypatch, session)

    def _must_not_write(**kwargs):  # pragma: no cover
        raise AssertionError("no txn -> update_ad_clearance must not be called")

    monkeypatch.setattr(routes.crm_client, "update_ad_clearance", _must_not_write)

    resp = client.post(
        "/confirmed-ctl/confirm", json=_confirm_body(bank_txn_id=None)
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["crm_write"] == "skipped_no_txn"
    assert session.committed is True


# --------------------------------------------------------------------------- #
# SHOULD-FIX #2 — rowcount / FOUND_ROWS
# --------------------------------------------------------------------------- #
def test_update_ad_clearance_rowcount_zero_raises_crm_write_error(monkeypatch):
    """rowcount==0 (no CRM row matched the id) -> CrmWriteError, no false success.

    The connection still commits (the UPDATE ran), but because FOUND_ROWS makes
    rowcount reflect MATCHED rows, 0 means the id does not exist.
    """
    monkeypatch.setattr(crm.settings, "CRM_WRITE_ENABLED", True)
    _configure_crm_db(monkeypatch)
    conn = FakeConn(rowcount=0)
    monkeypatch.setattr(crm, "_connect_write", lambda: conn)

    with pytest.raises(crm.CrmWriteError):
        crm.update_ad_clearance(
            ad_crm_id="does-not-exist",
            trxstring="memo\t-$1.00",
            urlgmailadconfirm="",
            datepaid=date(2026, 6, 26),
        )
    # The UPDATE was issued and committed; only the rowcount check failed it.
    assert len(conn.cur.executed) == 1
    assert conn.committed is True


def test_update_ad_clearance_uses_found_rows_client_flag(monkeypatch):
    """The write connection MUST be opened with client_flag=CLIENT.FOUND_ROWS.

    Assert on the kwargs passed to pymysql.connect by the real _connect_write.
    """
    import pymysql
    from pymysql.constants import CLIENT

    _configure_crm_db(monkeypatch)
    captured = {}

    def _fake_connect(**kwargs):
        captured.update(kwargs)
        return FakeConn(rowcount=1)

    monkeypatch.setattr(pymysql, "connect", _fake_connect)

    conn = crm._connect_write()
    assert isinstance(conn, FakeConn)
    assert "client_flag" in captured
    assert captured["client_flag"] & CLIENT.FOUND_ROWS == CLIENT.FOUND_ROWS


def test_update_ad_clearance_rejects_empty_ad_crm_id(monkeypatch):
    """Empty/None ad_crm_id -> raise before any connection/UPDATE."""
    monkeypatch.setattr(crm.settings, "CRM_WRITE_ENABLED", True)
    _configure_crm_db(monkeypatch)

    def _boom():  # pragma: no cover - must never be called
        raise AssertionError("must not connect with an empty ad_crm_id")

    monkeypatch.setattr(crm, "_connect_write", _boom)

    for bad in ("", None):
        with pytest.raises(crm.CrmWriteError):
            crm.update_ad_clearance(
                ad_crm_id=bad,
                trxstring="memo\t-$1.00",
                urlgmailadconfirm="",
                datepaid=date(2026, 6, 26),
            )


def test_confirm_crm_write_error_returns_502_no_postgres(client, monkeypatch):
    """/confirm: CrmWriteError (0 rows matched) -> 502, Postgres NOT committed."""
    monkeypatch.setattr(routes.settings, "CRM_WRITE_ENABLED", True)
    monkeypatch.setattr(routes, "store_confirmed_match", lambda **kw: None)
    session = FakeSession(txn=_txn(), existing=None)
    _patch_db(monkeypatch, session)

    def _no_row(**kwargs):
        raise crm.CrmWriteError("no CRM row matched ad_crm_id")

    monkeypatch.setattr(routes.crm_client, "update_ad_clearance", _no_row)

    resp = client.post("/confirmed-ctl/confirm", json=_confirm_body())
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["status"] == "crm_write_failed"
    assert body["detail"] == "no CRM row matched ad_crm_id"
    assert body["ad_crm_id"] == "6a343d2127bb55b5a"
    # No confirmation persisted; session rolled back.
    assert session.committed is False
    assert session.rolled_back is True
    assert not any(isinstance(o, AdConfirmation) for o in session.added)


# --------------------------------------------------------------------------- #
# SHOULD-FIX #1 — reverse orphan (CRM written, Postgres commit fails)
# --------------------------------------------------------------------------- #
def test_confirm_postgres_commit_fails_after_crm_write(client, monkeypatch, caplog):
    """CRM write ok but db.commit() raises -> controlled 500 + CRITICAL log.

    No AdConfirmation persists (commit failed); the reconcile log carries the
    written ad_crm_id so it can be self-healed by an idempotent retry.
    """
    monkeypatch.setattr(routes.settings, "CRM_WRITE_ENABLED", True)
    monkeypatch.setattr(routes, "store_confirmed_match", lambda **kw: None)
    session = FakeSession(txn=_txn(), existing=None, commit_raises=True)
    _patch_db(monkeypatch, session)

    monkeypatch.setattr(routes.crm_client, "update_ad_clearance", lambda **kw: None)

    with caplog.at_level(logging.CRITICAL):
        resp = client.post("/confirmed-ctl/confirm", json=_confirm_body())

    assert resp.status_code == 500
    body = resp.get_json()
    assert body["status"] == "postgres_commit_failed_after_crm_write"
    assert body["ad_crm_id"] == "6a343d2127bb55b5a"
    # CRITICAL reconcile log emitted, naming the ad_crm_id.
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical
    assert any("6a343d2127bb55b5a" in r.getMessage() for r in critical)
    assert any("commit FAILED" in r.getMessage() for r in critical)
    # commit_raises means committed never flips true; no confirmation persisted.
    assert session.committed is False


# --------------------------------------------------------------------------- #
# NIT — _build_trxstring tolerates a missing txn_date
# --------------------------------------------------------------------------- #
def test_build_trxstring_none_date_no_crash():
    """None txn_date -> sensible trxstring without the 'ON MM/DD' part, no crash."""
    txn = _txn()
    txn.txn_date = None
    trx = routes._build_trxstring(txn)
    # Still one TAB + signed amount; the date fragment is simply omitted.
    assert trx.count("\t") == 1
    memo, amount = trx.split("\t")
    assert amount == "-$2,226.94"
    assert "ON " not in memo
    assert memo == "PURCH W/O PIN LA TIMES MEDIA GR Debit"
