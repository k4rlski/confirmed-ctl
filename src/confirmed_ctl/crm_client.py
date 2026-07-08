"""CRM MySQL client — trigger query + tightly-scoped approved writes.

Read-only by default. Writes are enabled only when ``dry_run=False`` and are
restricted to the exact allow-listed fields from docs/CRM-SCHEMA.md. No other
fields are ever written; no schema changes are made.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from .config import CrmConfig
from .models import Case

logger = logging.getLogger(__name__)

# The ONLY columns confirmed-ctl is ever permitted to write.
ALLOWED_WRITE_FIELDS = frozenset(
    {"statacctgcreditnews", "urlgmailadconfirm", "trxstring", "datepaidnews"}
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_identifier(name: str, kind: str) -> str:
    """Validate a config-supplied SQL identifier (table/column) before interpolation."""
    if not _IDENTIFIER.match(name or ""):
        raise ValueError(f"Invalid {kind} identifier: {name!r}")
    return name


def build_trigger_query(config: CrmConfig) -> tuple[str, list[Any]]:
    """Build the parameterized trigger query and its parameters.

    Returns cases where the payment status is a trigger status and no bank
    transaction string has been written yet.
    """
    placeholders = ", ".join(["%s"] * len(config.trigger_statuses))
    case_number_column = _safe_identifier(config.case_number_column, "case_number_column")
    sql = f"""
        SELECT
          p.id,
          p.{case_number_column}   AS case_number,
          p.name                  AS company,
          p.adnumbernews          AS ad_number,
          p.pricenewsreal         AS invoice_amount,
          p.dateinvoicednews      AS date_invoiced,
          p.statacctgcreditnews   AS payment_status,
          p.trxstring             AS bank_transaction,
          p.urlgmailadconfirm     AS gmail_url,
          p.datepaidnews          AS date_paid,
          p.news_id               AS news_id,
          n.name                  AS newspaper_name,
          n.shortname             AS newspaper_short
        FROM {config.cases_table} p
        JOIN {config.news_table} n ON p.news_id = n.id
        WHERE p.statacctgcreditnews IN ({placeholders})
          AND p.trxstring IS NULL
          AND p.deleted = 0
        ORDER BY p.dateinvoicednews DESC
    """.strip()
    return sql, list(config.trigger_statuses)


def _case_number_from_row(row: dict[str, Any]) -> str:
    """Derive the human case number, falling back to the CRM id when absent."""
    number = row.get("case_number")
    if number is not None and str(number).strip():
        return str(number).strip()
    return str(row.get("id", ""))


def row_to_case(row: dict[str, Any]) -> Case:
    """Map a CRM result row (dict cursor) to a Case model."""
    return Case(
        id=str(row["id"]),
        case_number=_case_number_from_row(row),
        company=str(row.get("company") or ""),
        ad_number=str(row.get("ad_number") or ""),
        invoice_amount=float(row.get("invoice_amount") or 0.0),
        date_invoiced=row.get("date_invoiced"),
        payment_status=str(row.get("payment_status") or ""),
        news_id=str(row.get("news_id") or ""),
        newspaper_name=str(row.get("newspaper_name") or ""),
        newspaper_short=str(row.get("newspaper_short") or ""),
        trxstring=row.get("bank_transaction"),
        gmail_url=row.get("gmail_url"),
        date_paid=row.get("date_paid"),
    )


class CrmClient:
    """MySQL access to the CRM. Connection is lazy so --dry-run works offline."""

    def __init__(self, config: CrmConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run
        self._conn = None

    def _connect(self):
        if self._conn is not None:
            return self._conn
        try:
            import mysql.connector  # imported lazily; not needed for dry-run/tests
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "mysql-connector-python is required for live CRM access"
            ) from exc
        self._conn = mysql.connector.connect(
            host=self.config.host,
            port=self.config.port,
            database=self.config.database,
            user=self.config.user,
            password=self.config.password,
        )
        return self._conn

    def fetch_confirmed_cases(self, case_number: str | None = None) -> list[Case]:
        """Run the trigger query and return matching cases."""
        sql, params = build_trigger_query(self.config)
        conn = self._connect()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            cursor.close()
        cases = [row_to_case(r) for r in rows]
        if case_number:
            cases = [c for c in cases if c.case_number == case_number or c.id == case_number]
        return cases

    def write_case_fields(self, case: Case, fields: dict[str, Any]) -> None:
        """Write allow-listed fields for a case. No-op (logged) in dry-run."""
        illegal = set(fields) - ALLOWED_WRITE_FIELDS
        if illegal:
            raise ValueError(f"Refusing to write non-allow-listed CRM fields: {sorted(illegal)}")
        if not fields:
            return
        if self.dry_run:
            logger.info("[dry-run] would write case %s: %s", case.case_number, fields)
            return
        assignments = ", ".join(f"{name} = %s" for name in fields)
        values = list(fields.values())
        values.append(case.id)
        sql = f"UPDATE {self.config.cases_table} SET {assignments} WHERE id = %s"
        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, values)
            conn.commit()
        finally:
            cursor.close()
        logger.info("wrote case %s: %s", case.case_number, list(fields))

    @staticmethod
    def format_date(value: date | None) -> str | None:
        return value.strftime("%Y-%m-%d") if value else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
