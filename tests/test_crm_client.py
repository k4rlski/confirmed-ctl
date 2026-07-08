"""Unit tests for the read-only CRM adapter (confirmed_ctl.crm.client).

No live DB: the pymysql connection is replaced by an in-memory fake that records
every SQL statement executed so we can assert (a) correct query text / binding
and (b) that the adapter issues SELECT-only statements (never a write).
"""

import re
from datetime import date

import pytest

from confirmed_ctl.crm import client as crm
from confirmed_ctl.db.models import CrmAd


class FakeCursor:
    """Records executed SQL + params and returns canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.executed = []  # list of (sql, params)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def _sample_row(**overrides):
    row = {
        "owner": "karl",
        "id": "REC123",
        "adsapproveddate": date(2026, 6, 1),
        "datebuynews": date(2026, 6, 17),
        "datenewsstart": date(2026, 6, 15),
        "name": "Eduexplora International",
        "jobtitle": "Analyst",
        "attyname": "Jane Atty",
        "beneficiarylast": "Doe",
        "entity": "JKT",
        "statclearancenews": '["Confirmed"]',
        "statnews": '["Active"]',
        "statacctgcreditnews": '["Confirmed"]',
        "dboxemailthreadcase": "thread-1",
        "adnumbernews": "IPR00160880",
        "newspapers_name": "Miami Herald",
        "rank": 1,
        "pricenewsreal": 1368.0,
    }
    row.update(overrides)
    return row


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(crm.settings, "CRM_DB_HOST", "permtrak.com")
    monkeypatch.setattr(crm.settings, "CRM_DB_USER", "ro_user")
    monkeypatch.setattr(crm.settings, "CRM_DB_PASS", "x")
    monkeypatch.setattr(crm.settings, "CRM_DB_NAME", "permtrak2_crm")


# --------------------------------------------------------------------------- #
# parse_enum
# --------------------------------------------------------------------------- #
def test_parse_enum_json_array():
    assert crm.parse_enum('["Confirmed"]') == "Confirmed"
    assert crm.parse_enum('["Active Case"]') == "Active Case"


def test_parse_enum_plain_and_none():
    assert crm.parse_enum("Confirmed") == "Confirmed"
    assert crm.parse_enum("  Active  ") == "Active"
    assert crm.parse_enum(None) is None
    assert crm.parse_enum("") is None
    assert crm.parse_enum([]) is None
    assert crm.parse_enum(["Done"]) == "Done"


def test_parse_enum_malformed_json_falls_back_to_string():
    # A leading '[' but invalid JSON should not raise — return the raw text.
    assert crm.parse_enum("[oops") == "[oops"


# --------------------------------------------------------------------------- #
# list_clearances
# --------------------------------------------------------------------------- #
def test_list_clearances_query_verbatim_and_mapping(monkeypatch, configured):
    conn = FakeConn([_sample_row(), _sample_row(id="REC999", pricenewsreal=500.0)])
    monkeypatch.setattr(crm, "_connect", lambda: conn)

    ads = crm.list_clearances()

    # Exact query text (verbatim ABCF-X clearances query) with no params.
    sql, params = conn.cursor_obj.executed[0]
    assert sql == crm.CLEARANCES_QUERY
    assert params is None
    # EspoCRM JSON enum string forms appear verbatim in the WHERE clause.
    assert "statnews='[\"Active\"]'" in sql
    assert "statclearancenews='[\"Confirmed\"]'" in sql
    assert "t_e_s_t_p_e_r_m.statpermcase='[\"Active Case\"]'" in sql
    assert "(entity='JKT' OR entity='PA')" in sql
    assert "ORDER BY datebuynews DESC" in sql

    assert len(ads) == 2
    ad = ads[0]
    assert isinstance(ad, CrmAd)
    assert ad.crm_id == "REC123"
    assert ad.ad_number == "IPR00160880"
    assert ad.client_name == "Eduexplora International"
    assert ad.newspaper_name == "Miami Herald"
    assert ad.run_date == date(2026, 6, 15)
    assert ad.expected_charge_date == date(2026, 6, 17)
    assert ad.expected_amount == 1368.0
    assert conn.closed is True


def test_list_clearances_charge_date_falls_back_to_run_date(monkeypatch, configured):
    conn = FakeConn([_sample_row(datebuynews=None)])
    monkeypatch.setattr(crm, "_connect", lambda: conn)

    ad = crm.list_clearances()[0]
    assert ad.expected_charge_date == date(2026, 6, 15)  # fell back to datenewsstart


def test_list_clearances_unconfigured_returns_empty(monkeypatch):
    monkeypatch.setattr(crm.settings, "CRM_DB_HOST", "")

    def _boom():  # pragma: no cover - must never be called
        raise AssertionError("must not connect when unconfigured")

    monkeypatch.setattr(crm, "_connect", _boom)
    assert crm.list_clearances() == []


# --------------------------------------------------------------------------- #
# get_ad
# --------------------------------------------------------------------------- #
def test_get_ad_parameterized(monkeypatch, configured):
    conn = FakeConn([_sample_row()])
    monkeypatch.setattr(crm, "_connect", lambda: conn)

    ad = crm.get_ad("REC123")

    sql, params = conn.cursor_obj.executed[0]
    assert sql == crm.GET_AD_QUERY
    # ad_crm_id is bound as a parameter, NEVER string-interpolated.
    assert params == ("REC123",)
    assert "%s" in sql
    assert "REC123" not in sql
    assert ad.crm_id == "REC123"


def test_get_ad_returns_none_on_no_row(monkeypatch, configured):
    conn = FakeConn([])
    monkeypatch.setattr(crm, "_connect", lambda: conn)
    assert crm.get_ad("nope") is None


def test_get_ad_unconfigured_returns_none(monkeypatch):
    monkeypatch.setattr(crm.settings, "CRM_DB_HOST", "")
    assert crm.get_ad("REC123") is None


# --------------------------------------------------------------------------- #
# Read-only guarantee
# --------------------------------------------------------------------------- #
def test_adapter_issues_only_selects(monkeypatch, configured):
    conn = FakeConn([_sample_row()])
    monkeypatch.setattr(crm, "_connect", lambda: conn)

    crm.list_clearances()
    crm.get_ad("REC123")

    # Word-boundary match so the read-only ``deleted=0`` filter (contains the
    # substring "DELETE") is not mistaken for a write verb.
    write_re = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|TRUNCATE)\b")
    for sql, _ in conn.cursor_obj.executed:
        upper = sql.upper()
        assert upper.lstrip().startswith("SELECT")
        assert write_re.search(upper) is None


def test_module_source_has_no_write_sql():
    # Belt-and-suspenders: the adapter source contains no write statements.
    import inspect

    src = inspect.getsource(crm).upper()
    for tok in ("INSERT INTO", "UPDATE ", "DELETE FROM", "REPLACE INTO", " DROP ", "TRUNCATE"):
        assert tok not in src
