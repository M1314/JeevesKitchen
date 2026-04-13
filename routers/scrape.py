"""
JeevesKitchen – scrape-control router.

Endpoints
---------
POST /scrape/start
    Kick off a full scrape of ecosophia.dreamwidth.org.  The scrape runs in a
    daemon thread so the HTTP response returns immediately; progress is tracked
    in the ``scrape_progress`` singleton and can be polled via the status
    endpoint.

GET /scrape/status
    Return a JSON snapshot of the current (or most recently completed) scrape
    progress, including counts of processed/errored URLs and the URL that is
    being fetched at the moment of the request.

Concurrency note
----------------
Only one scrape may run at a time.  A second ``POST /scrape/start`` while a
scrape is active returns HTTP 409 Conflict.
"""

import threading

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from scraper import run_scrape, scrape_progress

# All routes in this module are grouped under /scrape in the OpenAPI docs.
router = APIRouter(prefix="/scrape", tags=["scrape"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _scrape_in_thread(force: bool) -> None:
    """Entry point for the background scraper thread.

    Opens a dedicated SQLAlchemy session for the duration of the scrape so
    that the session is not shared with any other request.  The session is
    closed in the ``finally`` block regardless of whether the scrape succeeds
    or raises an exception.

    Parameters
    ----------
    force:
        Passed through to ``run_scrape``; when ``True`` all posts are
        re-fetched even if they already exist in the database.
    """
    # Import here to avoid a circular dependency at module load time.
    from database import SessionLocal

    db = SessionLocal()
    try:
        run_scrape(db, force=force)
    finally:
        # Always release the connection back to the pool.
        db.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/start",
    summary="Start a full site scrape",
    response_description="Confirmation that the scrape thread has been launched",
)
def start_scrape(force: bool = False) -> dict:
    """Start a background scrape of the entire Ecosophia Dreamwidth journal.

    The scrape runs in a **daemon thread** so this endpoint returns immediately.
    Use ``GET /scrape/status`` to monitor progress.

    Parameters
    ----------
    force : bool
        Set to ``true`` to re-fetch and re-upsert posts that are already stored
        in the database.  Useful after a schema change or to pick up edited
        posts.  Defaults to ``false`` (incremental: skip existing posts).

    Raises
    ------
    HTTPException (409)
        If a scrape is already running.  Only one concurrent scrape is
        supported per application instance.
    """
    if scrape_progress.running:
        raise HTTPException(status_code=409, detail="A scrape is already running.")

    # ``daemon=True`` ensures the thread does not prevent the process from
    # exiting cleanly if the application is shut down mid-scrape.
    thread = threading.Thread(target=_scrape_in_thread, args=(force,), daemon=True)
    thread.start()
    return {"message": "Scrape started.", "force": force}


@router.get(
    "/status",
    summary="Poll scrape progress",
    response_description="Current scrape progress snapshot",
)
def scrape_status() -> dict:
    """Return a JSON snapshot of the ongoing or most recently completed scrape.

    The response includes:

    * ``running``      – whether a scrape is currently active
    * ``total``        – total number of post URLs discovered
    * ``done``         – number of URLs processed (successful or errored)
    * ``errors``       – number of URLs that failed to fetch or parse
    * ``current_url``  – URL being processed right now (empty when idle)
    * ``started_at``   – ISO 8601 UTC timestamp of scrape start
    * ``finished_at``  – ISO 8601 UTC timestamp of scrape end (``null`` while running)
    """
    return scrape_progress.to_dict()

