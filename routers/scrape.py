"""
Scrape-control endpoints.

POST /scrape/start   – kick off a full scrape in a background thread
GET  /scrape/status  – poll progress
"""

import threading

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from scraper import run_scrape, scrape_progress

router = APIRouter(prefix="/scrape", tags=["scrape"])


def _scrape_in_thread(force: bool):
    """Run the scrape in a worker thread so the HTTP response returns immediately."""
    from database import SessionLocal

    db = SessionLocal()
    try:
        run_scrape(db, force=force)
    finally:
        db.close()


@router.post("/start")
def start_scrape(force: bool = False):
    """
    Start a full scrape of ecosophia.dreamwidth.org.

    - **force**: if `true`, re-scrape posts already in the database.
    """
    if scrape_progress.running:
        raise HTTPException(status_code=409, detail="A scrape is already running.")

    thread = threading.Thread(target=_scrape_in_thread, args=(force,), daemon=True)
    thread.start()
    return {"message": "Scrape started.", "force": force}


@router.get("/status")
def scrape_status():
    """Return the current (or last completed) scrape progress."""
    return scrape_progress.to_dict()
