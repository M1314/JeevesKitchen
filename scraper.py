"""
Scraper for ecosophia.dreamwidth.org.

Strategy
--------
1. Fetch the archive page (no JavaScript required – Dreamwidth renders server-side).
2. Collect every individual post URL found in the archive.
3. Fetch each post page, parse title / author / date / body / tags / comments.
4. Upsert results into the database.

All network I/O is done with httpx (sync client with connection pooling) to keep
the implementation straightforward and compatible with FastAPI background tasks.
"""

import logging
import re
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from models import Comment, Post, Tag

logger = logging.getLogger(__name__)

BASE_URL = "https://ecosophia.dreamwidth.org"
ARCHIVE_URL = f"{BASE_URL}/archive"
# Be polite: wait between requests
REQUEST_DELAY = 0.5  # seconds

HEADERS = {
    "User-Agent": (
        "JeevesKitchen/1.0 (+https://github.com/M1314/JeevesKitchen; "
        "community archive tool)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Dreamwidth month-archive URLs look like /YYYY/MM/
_MONTH_ARCHIVE_RE = re.compile(r"/\d{4}/\d{2}/?$")
# Individual post URLs look like /NNNNNN.html
_POST_URL_RE = re.compile(r"/(\d+)\.html$")
# Dreamwidth comment IDs live in anchors like id="cmt12345"
_COMMENT_ID_RE = re.compile(r"cmt(\d+)")
# Date strings on Dreamwidth: "April 13th, 2024" or "2024-04-13 12:00"
_DW_DATE_FORMATS = [
    "%B %dst, %Y",
    "%B %dnd, %Y",
    "%B %drd, %Y",
    "%B %dth, %Y",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(client: httpx.Client, url: str) -> Optional[BeautifulSoup]:
    """Fetch *url* and return a BeautifulSoup tree, or None on error."""
    try:
        response = client.get(url, headers=HEADERS, follow_redirects=True, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "lxml")
    except httpx.HTTPStatusError as exc:
        logger.warning("HTTP %s fetching %s", exc.response.status_code, url)
    except httpx.RequestError as exc:
        logger.warning("Request error fetching %s: %s", url, exc)
    return None


def _parse_date(text: str) -> Optional[datetime]:
    """Try several date formats and return a datetime, or None."""
    text = text.strip()
    # Normalise ordinal suffixes: "13th" → "13"
    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text)
    for fmt in _DW_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _extract_text(tag) -> str:
    return tag.get_text(separator=" ", strip=True) if tag else ""


# ---------------------------------------------------------------------------
# Archive crawl: collect all post URLs
# ---------------------------------------------------------------------------


def _collect_post_urls(client: httpx.Client) -> list[str]:
    """Return a deduplicated list of all post URLs found on the archive page."""
    soup = _get(client, ARCHIVE_URL)
    if soup is None:
        logger.error("Could not fetch archive page.")
        return []

    urls: set[str] = set()

    # The Dreamwidth archive lists links to monthly sub-archives AND to
    # individual posts directly.  We handle both.
    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        # Make absolute
        if href.startswith("/"):
            href = BASE_URL + href
        if not href.startswith(BASE_URL):
            continue

        path = urlparse(href).path

        if _POST_URL_RE.search(path):
            urls.add(href.split("?")[0])  # strip query string
        elif _MONTH_ARCHIVE_RE.search(path):
            # Fetch the monthly archive page and collect post links from it
            time.sleep(REQUEST_DELAY)
            month_soup = _get(client, href)
            if month_soup:
                for ma in month_soup.find_all("a", href=True):
                    mhref: str = ma["href"]
                    if mhref.startswith("/"):
                        mhref = BASE_URL + mhref
                    if _POST_URL_RE.search(urlparse(mhref).path):
                        urls.add(mhref.split("?")[0])

    logger.info("Discovered %d post URLs.", len(urls))
    return sorted(urls)


# ---------------------------------------------------------------------------
# Post parsing
# ---------------------------------------------------------------------------


def _parse_post(soup: BeautifulSoup, url: str) -> Optional[dict]:
    """Parse a single post page and return a structured dict."""
    # --- Entry ID -----------------------------------------------------------
    m = _POST_URL_RE.search(urlparse(url).path)
    if not m:
        return None
    entry_id = int(m.group(1))

    # --- Title --------------------------------------------------------------
    title_tag = (
        soup.find("h2", class_="entry-title")
        or soup.find("title")
    )
    title = _extract_text(title_tag) if title_tag else "Untitled"
    # Strip " - Ecosophia" suffix that appears in <title>
    title = re.sub(r"\s*[-–]\s*Ecosophia\s*$", "", title, flags=re.IGNORECASE).strip()

    # --- Author -------------------------------------------------------------
    author_tag = soup.find("span", class_="ljuser") or soup.find(
        "a", class_="url"
    )
    author = _extract_text(author_tag) if author_tag else "ecosophia"

    # --- Date ---------------------------------------------------------------
    date_tag = (
        soup.find("time", class_="datetime")
        or soup.find("span", class_="date")
        or soup.find("abbr", class_="published")
    )
    published_at = None
    if date_tag:
        dt_str = date_tag.get("datetime") or _extract_text(date_tag)
        published_at = _parse_date(dt_str)

    # --- Body ---------------------------------------------------------------
    body_tag = (
        soup.find("div", class_="entry-content")
        or soup.find("article", class_="entry")
        or soup.find("div", id="entry-content")
    )
    body_html = str(body_tag) if body_tag else ""
    body_text = _extract_text(body_tag) if body_tag else ""

    # --- Tags ---------------------------------------------------------------
    tag_names: list[str] = []
    tags_section = soup.find("div", class_="tag") or soup.find("ul", class_="entry-tags")
    if tags_section:
        for t in tags_section.find_all("a"):
            name = _extract_text(t).strip()
            if name:
                tag_names.append(name)

    # --- Comment count ------------------------------------------------------
    comment_count_tag = soup.find("span", class_="comment-count") or soup.find(
        "a", class_="entry-readlink"
    )
    comment_count = 0
    if comment_count_tag:
        nums = re.findall(r"\d+", _extract_text(comment_count_tag))
        if nums:
            comment_count = int(nums[0])

    # --- Comments -----------------------------------------------------------
    comments: list[dict] = []
    comment_section = soup.find("div", id="comments") or soup.find(
        "ol", class_="comment-thread"
    )
    if comment_section:
        for comment_div in comment_section.find_all(
            "div", class_=re.compile(r"\bcomment\b")
        ):
            c_id_str = comment_div.get("id", "")
            c_id_m = _COMMENT_ID_RE.search(c_id_str)
            comment_dw_id = int(c_id_m.group(1)) if c_id_m else None

            c_author_tag = comment_div.find("span", class_="comment-poster") or comment_div.find(
                "span", class_="ljuser"
            )
            c_author = _extract_text(c_author_tag) if c_author_tag else "Anonymous"

            c_date_tag = comment_div.find("time") or comment_div.find(
                "span", class_="datetime"
            )
            c_date = None
            if c_date_tag:
                c_dt_str = c_date_tag.get("datetime") or _extract_text(c_date_tag)
                c_date = _parse_date(c_dt_str)

            c_body_tag = comment_div.find("div", class_="comment-content") or comment_div.find(
                "div", class_="comment-body"
            )
            c_body_html = str(c_body_tag) if c_body_tag else ""
            c_body_text = _extract_text(c_body_tag) if c_body_tag else ""

            # Parent: Dreamwidth nests replies; look for data-parent or ul depth
            parent_id = None
            parent_str = comment_div.get("data-parent-comment", "")
            if parent_str.isdigit():
                parent_id = int(parent_str)

            comments.append(
                {
                    "comment_dw_id": comment_dw_id,
                    "author": c_author,
                    "published_at": c_date,
                    "body_html": c_body_html,
                    "body_text": c_body_text,
                    "parent_comment_dw_id": parent_id,
                }
            )

    return {
        "entry_id": entry_id,
        "url": url,
        "title": title,
        "author": author,
        "published_at": published_at,
        "body_html": body_html,
        "body_text": body_text,
        "comment_count": comment_count,
        "tags": tag_names,
        "comments": comments,
    }


# ---------------------------------------------------------------------------
# Database upsert
# ---------------------------------------------------------------------------


def _upsert_post(db: Session, data: dict) -> Post:
    """Insert or update a Post (and its tags/comments) from *data*."""
    post = db.query(Post).filter(Post.entry_id == data["entry_id"]).first()
    if post is None:
        post = Post(entry_id=data["entry_id"])
        db.add(post)

    post.url = data["url"]
    post.title = data["title"]
    post.author = data["author"]
    post.published_at = data["published_at"]
    post.body_html = data["body_html"]
    post.body_text = data["body_text"]
    post.comment_count = data["comment_count"]
    post.scraped_at = datetime.utcnow()

    # Tags
    tag_objs: list[Tag] = []
    for name in data["tags"]:
        tag = db.query(Tag).filter(Tag.name == name).first()
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
        tag_objs.append(tag)
    post.tags = tag_objs

    # Comments – replace all for simplicity
    for c in post.comments:
        db.delete(c)
    db.flush()

    for c_data in data["comments"]:
        comment = Comment(post=post, **c_data)
        db.add(comment)

    db.commit()
    db.refresh(post)
    return post


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


class ScrapeProgress:
    """Simple mutable container shared between the scraper and the status endpoint."""

    def __init__(self):
        self.running: bool = False
        self.total: int = 0
        self.done: int = 0
        self.errors: int = 0
        self.current_url: str = ""
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "total": self.total,
            "done": self.done,
            "errors": self.errors,
            "current_url": self.current_url,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# Global singleton – good enough for a single-worker deployment
scrape_progress = ScrapeProgress()


def run_scrape(db: Session, force: bool = False) -> ScrapeProgress:
    """
    Scrape the entire ecosophia.dreamwidth.org blog into *db*.

    If *force* is False, posts already in the database are skipped.
    Returns the ScrapeProgress object (also accessible via the module-level singleton).
    """
    global scrape_progress

    if scrape_progress.running:
        logger.info("Scrape already in progress; skipping.")
        return scrape_progress

    scrape_progress = ScrapeProgress()
    scrape_progress.running = True
    scrape_progress.started_at = datetime.utcnow()

    try:
        with httpx.Client() as client:
            urls = _collect_post_urls(client)
            scrape_progress.total = len(urls)

            for url in urls:
                scrape_progress.current_url = url

                if not force:
                    m = _POST_URL_RE.search(urlparse(url).path)
                    if m:
                        existing = (
                            db.query(Post)
                            .filter(Post.entry_id == int(m.group(1)))
                            .first()
                        )
                        if existing:
                            scrape_progress.done += 1
                            continue

                time.sleep(REQUEST_DELAY)
                soup = _get(client, url)
                if soup is None:
                    scrape_progress.errors += 1
                    scrape_progress.done += 1
                    continue

                data = _parse_post(soup, url)
                if data is None:
                    scrape_progress.errors += 1
                    scrape_progress.done += 1
                    continue

                try:
                    _upsert_post(db, data)
                except Exception as exc:
                    logger.error("DB error for %s: %s", url, exc)
                    db.rollback()
                    scrape_progress.errors += 1

                scrape_progress.done += 1
                logger.info(
                    "[%d/%d] Scraped: %s",
                    scrape_progress.done,
                    scrape_progress.total,
                    url,
                )
    except Exception as exc:
        logger.exception("Scrape failed: %s", exc)
    finally:
        scrape_progress.running = False
        scrape_progress.finished_at = datetime.utcnow()
        scrape_progress.current_url = ""

    return scrape_progress
