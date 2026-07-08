"""Database engine + session management.

The engine is created lazily on first use so that importing this module (and the
package as a whole) does not require ``DATABASE_URL`` or a Postgres driver — only
code paths that actually touch the database do.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .. import settings

_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        if not settings.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Copy .env.example to .env and configure it."
            )
        _engine = create_engine(settings.DATABASE_URL, future=True, pool_pre_ping=True)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), future=True, expire_on_commit=False)
    return _SessionFactory


@contextmanager
def get_db() -> Iterator[Session]:
    """Yield a database session, rolling back on error and always closing.

    Callers commit explicitly; this context manager guarantees cleanup.
    """
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
