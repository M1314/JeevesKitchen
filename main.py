"""
JeevesKitchen – application entry point.

This module creates the FastAPI application instance, configures logging,
initialises the database schema, and wires together all API routers.

Startup sequence
----------------
1. ``logging.basicConfig`` sets INFO-level logging for all modules so that
   scrape progress, SQL events, and request lifecycle messages appear in the
   console / hosting-platform log stream.
2. ``Base.metadata.create_all`` runs DDL against the configured database to
   create any tables that do not yet exist.  The call is idempotent – existing
   tables are left untouched, making it safe to redeploy without migrations for
   schema-additive changes.
3. Three routers are mounted:
   - ``/scrape``  – trigger and monitor the web scraper
   - ``/posts``   – query scraped posts
   - ``/tags``    – browse posts by tag

Deployment
----------
The app is served by Uvicorn (see ``render.yaml``).  Start locally with::

    uvicorn main:app --reload
"""

import logging

from fastapi import FastAPI

from database import Base, engine
from routers.posts import router as posts_router
from routers.posts import tags_router
from routers.scrape import router as scrape_router

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Configure root logger so that all child loggers (scraper, SQLAlchemy, etc.)
# emit INFO-level messages to stdout.  Hosting platforms (Render, Heroku, …)
# capture stdout automatically.
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------
# Ensure every ORM-mapped table exists in the target database.  Using
# create_all here means no separate Alembic migration step is required for
# a fresh deployment.  For production schema migrations, Alembic should be
# adopted once the schema stabilises.
Base.metadata.create_all(bind=engine)

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
app = FastAPI(
    title="JeevesKitchen",
    description="Search and archive tool for the Ecosophia community blog.",
    version="0.1.0",
)

# Mount the scrape-control router under /scrape
app.include_router(scrape_router)
# Mount the post-query router under /posts
app.include_router(posts_router)
# Mount the tag-query router under /tags
app.include_router(tags_router)


# ---------------------------------------------------------------------------
# Health-check endpoint
# ---------------------------------------------------------------------------

@app.get("/", summary="Health check", tags=["health"])
def root():
    """Return a simple liveness signal so load balancers can verify the service is up."""
    return {"message": "Jeeves backend is running"}