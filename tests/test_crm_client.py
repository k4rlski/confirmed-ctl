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
        "datenewsend": date(2026, 6, 20),
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
        "casenumber": "A-2026-0042",
        "jobsitestate": "CA",
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
    # Richer ad-identifying fields (ABCF-X columns).
    assert ad.case_number == "A-2026-0042"
    assert ad.state == "CA"
    assert ad.attorney == "Jane Atty"
    assert ad.entity == "JKT"
    # Additional ABCF-X reconcile columns.
    assert ad.job_title == "Analyst"
    assert ad.run_end == date(2026, 6, 20)
    # status_news is the raw EspoCRM enum string, passed through as-is.
    assert ad.status_news == '["Active"]'
    assert ad.owner == "karl"
    # Additional ABCF-X contract columns.
    assert ad.approved_date == date(2026, 6, 1)
    # buy_date is datebuynews surfaced distinctly from expected_charge_date.
    assert ad.buy_date == date(2026, 6, 17)
    assert ad.beneficiary_last == "Doe"
    # clearance_status is the raw EspoCRM enum string, passed through as-is.
    assert ad.clearance_status == '["Confirmed"]'
    assert conn.closed is True


def test_select_from_includes_new_columns():
    # The read SELECT must fetch casenumber + jobsitestate (attyname + entity
    # were already selected) so _row_to_crm_ad can map the ABCF-X columns.
    assert "t_e_s_t_p_e_r_m.casenumber" in crm._SELECT_FROM
    assert "t_e_s_t_p_e_r_m.jobsitestate" in crm._SELECT_FROM
    assert "t_e_s_t_p_e_r_m.attyname" in crm._SELECT_FROM
    assert "t_e_s_t_p_e_r_m.entity" in crm._SELECT_FROM
    # datenewsend is the newly added column (jobtitle, statnews, news.owner were
    # already selected) feeding run_end.
    assert "t_e_s_t_p_e_r_m.datenewsend" in crm._SELECT_FROM
    assert "t_e_s_t_p_e_r_m.jobtitle" in crm._SELECT_FROM
    assert "t_e_s_t_p_e_r_m.statnews" in crm._SELECT_FROM
    assert "news.owner AS owner" in crm._SELECT_FROM


def test_row_to_crm_ad_maps_richer_fields_and_strips_ad_number(monkeypatch, configured):
    # adnumbernews carries a trailing space in the CRM — it must be stripped.
    conn = FakeConn([_sample_row(adnumbernews="IPR00160880 ")])
    monkeypatch.setattr(crm, "_connect", lambda: conn)

    ad = crm.list_clearances()[0]
    assert ad.ad_number == "IPR00160880"  # trailing space stripped
    assert ad.case_number == "A-2026-0042"
    assert ad.state == "CA"
    assert ad.attorney == "Jane Atty"
    assert ad.entity == "JKT"
    assert ad.job_title == "Analyst"
    assert ad.run_end == date(2026, 6, 20)
    assert ad.status_news == '["Active"]'
    assert ad.owner == "karl"


def test_row_to_crm_ad_ad_number_none_safe(monkeypatch, configured):
    conn = FakeConn([_sample_row(adnumbernews=None)])
    monkeypatch.setattr(crm, "_connect", lambda: conn)
    ad = crm.list_clearances()[0]
    assert ad.ad_number is None


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
# list_reconciled
# --------------------------------------------------------------------------- #
def test_list_reconciled_query_where_done_and_mapping(monkeypatch, configured):
    conn = FakeConn([_sample_row(statclearancenews='["Done"]')])
    monkeypatch.setattr(crm, "_connect", lambda: conn)

    ads = crm.list_reconciled()

    sql, params = conn.cursor_obj.executed[0]
    assert sql == crm.RECONCILED_QUERY
    assert params is None
    # WHERE matches Done (not Confirmed); the rest of the WHERE is unchanged.
    assert "statclearancenews='[\"Done\"]'" in sql
    assert "statclearancenews='[\"Confirmed\"]'" not in sql
    assert "statnews='[\"Active\"]'" in sql
    assert "(entity='JKT' OR entity='PA')" in sql
    assert "t_e_s_t_p_e_r_m.statpermcase='[\"Active Case\"]'" in sql
    assert "t_e_s_t_p_e_r_m.deleted=0" in sql
    assert "ORDER BY datebuynews DESC" in sql
    # Same CrmAd shape (incl. the new contract fields).
    assert len(ads) == 1
    ad = ads[0]
    assert isinstance(ad, CrmAd)
    assert ad.crm_id == "REC123"
    assert ad.clearance_status == '["Done"]'
    assert ad.buy_date == date(2026, 6, 17)
    assert conn.closed is True


def test_list_reconciled_unconfigured_returns_empty(monkeypatch):
    monkeypatch.setattr(crm.settings, "CRM_DB_HOST", "")

    def _boom():  # pragma: no cover - must never be called
        raise AssertionError("must not connect when unconfigured")

    monkeypatch.setattr(crm, "_connect", _boom)
    assert crm.list_reconciled() == []


def test_reconciled_query_is_select_only():
    write_re = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|TRUNCATE)\b")
    upper = crm.RECONCILED_QUERY.upper()
    assert upper.lstrip().startswith("SELECT")
    assert write_re.search(upper) is None


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


def test_read_query_constants_are_select_only():
    # Belt-and-suspenders: the READ query constants issue SELECT only and carry
    # no write verbs. (The module also has update_ad_clearance — the single,
    # gated, allowlisted write — which is covered by tests/test_crm_writeback.py.)
    write_re = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|TRUNCATE)\b")
    for query in (crm.CLEARANCES_QUERY, crm.GET_AD_QUERY, crm._SELECT_FROM):
        upper = query.upper()
        assert upper.lstrip().startswith("SELECT")
        assert write_re.search(upper) is None
