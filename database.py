"""
JeevesKitchen – database configuration.

This module creates a single SQLAlchemy engine and session factory that are
shared across the entire application.  It also defines the declarative
``Base`` class that all ORM models inherit from, and provides the
``get_db`` dependency used by FastAPI route handlers.

Database selection
------------------
The target database is controlled by the ``DATABASE_URL`` environment variable:

* **Not set (local development):** falls back to ``sqlite:///./jeeves.db``,
  creating a file-based SQLite database in the current working directory.
  No additional infrastructure is required.
* **Set to a PostgreSQL URL (Render / Heroku):** the engine connects to the
  provided PostgreSQL instance.  Older hosting platforms still supply
  ``postgres://`` URIs; SQLAlchemy 2.x requires the ``postgresql://`` scheme,
  so a one-time string replacement is applied automatically.

Connection arguments
--------------------
SQLite requires ``check_same_thread=False`` because FastAPI may call
``get_db`` from any thread.  This argument is omitted for PostgreSQL, which
handles concurrent access natively through its own connection pool.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# ---------------------------------------------------------------------------
# Database URL resolution
# ---------------------------------------------------------------------------

# Read the connection string from the environment; default to a local SQLite
# file so the application works out-of-the-box without extra setup.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./jeeves.db")

# Render (and historically Heroku) inject a ``postgres://`` URL, but
# SQLAlchemy 2.x dropped support for that scheme in favour of
# ``postgresql://``.  Patch the URL silently so deployments on those
# platforms work without manual configuration.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# SQLite needs check_same_thread=False because FastAPI runs handlers in a
# thread pool and the same connection object may be accessed from multiple
# threads within a single request.  PostgreSQL does not need this argument.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# The engine is the low-level connection factory.  SQLAlchemy's connection pool
# (QueuePool by default) is used for PostgreSQL; SQLite uses StaticPool
# implicitly through the connect_args override above.
engine = create_engine(DATABASE_URL, connect_args=connect_args)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

# autocommit=False  – changes must be committed explicitly; this prevents
#                     accidental partial writes.
# autoflush=False   – SQLAlchemy will not flush pending changes to the DB
#                     automatically before every query; route handlers decide
#                     when to flush/commit for predictable behaviour.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models in JeevesKitchen.

    Every model that inherits from ``Base`` will be registered with the
    shared metadata object, enabling ``Base.metadata.create_all`` to create
    all tables in a single call.
    """


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def get_db():
    """Yield a database session for use within a single FastAPI request.

    This is a FastAPI *dependency* used via ``Depends(get_db)``.  It follows
    the recommended "one session per request" pattern:

    * A new ``SessionLocal`` instance is created at the start of the request.
    * The session is yielded to the route handler.
    * The ``finally`` block guarantees the session is closed (and the
      underlying connection returned to the pool) even if an exception is
      raised during request processing.

    Yields
    ------
    sqlalchemy.orm.Session
        An active database session bound to the configured engine.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
