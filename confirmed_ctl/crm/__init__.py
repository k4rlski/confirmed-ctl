"""Read-only adapter into the MariaDB CRM (``permtrak2_crm``).

Ad / case data lives ONLY in the CRM and is never persisted in the confirmed-ctl
Postgres (see ``confirmed_ctl/db/models.py``). This package exposes a read-only
(SELECT-only) lookup adapter that hydrates :class:`~confirmed_ctl.db.models.CrmAd`
read views from ``t_e_s_t_p_e_r_m``. It NEVER issues INSERT/UPDATE/DELETE.
"""

from .client import get_ad, is_configured, list_clearances, parse_enum

__all__ = ["get_ad", "is_configured", "list_clearances", "parse_enum"]
