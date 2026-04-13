"""
JeevesKitchen – Dreamwidth scraper.

Strategy
--------
1. Fetch the archive page (no JavaScript required – Dreamwidth renders server-side HTML).
2. Walk every monthly sub-archive link to collect all individual post URLs.
3. Fetch each post page; parse title, author, publish date, body, tags, and
   threaded comments.
4. Upsert every parsed post into the database, creating or updating tags and
   replacing comments on each run.

Network I/O
-----------
All HTTP calls are made through a single ``httpx.Client`` session (with
connection keep-alive) so that TLS handshake overhead is paid only once per
scrape.  A configurable ``REQUEST_DELAY`` pause is inserted between requests
to avoid hammering the host server – this is the polite-crawling contract
stated in the ``User-Agent`` header.

Concurrency model
-----------------
The scraper runs synchronously inside a ``threading.Thread`` spawned by the
``/scrape/start`` endpoint.  Progress is tracked in a module-level
``ScrapeProgress`` singleton that the ``/scrape/status`` endpoint reads
without locking (acceptable because Python's GIL makes attribute reads and
writes atomic for simple types).
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from models import Comment, Post, Tag

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# All log messages from this module appear under the "scraper" name in the
# application's log output, making it easy to filter scraper activity.
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Root URL for all requests to the Dreamwidth journal.
BASE_URL = "https://ecosophia.dreamwidth.org"

# The main archive page lists all months for which posts exist.
ARCHIVE_URL = f"{BASE_URL}/archive"

# Seconds to sleep between consecutive HTTP GET requests.  Keeps the scraper
# well within any implicit rate limits on the host server.
REQUEST_DELAY = 0.5  # seconds

# HTTP headers sent with every request.  The descriptive User-Agent string
# identifies the bot and provides a contact URL in case the site operator
# wants to reach out.
HEADERS = {
    "User-Agent": (
        "JeevesKitchen/1.0 (+https://github.com/M1314/JeevesKitchen; "
        "community archive tool)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Compiled regular expressions
# ---------------------------------------------------------------------------

# Monthly archive URLs appear as /YYYY/MM/ (with optional trailing slash).
_MONTH_ARCHIVE_RE = re.compile(r"/\d{4}/\d{2}/?$")

# Individual post URLs appear as /NNNNNN.html; capture group 1 = entry ID.
_POST_URL_RE = re.compile(r"/(\d+)\.html$")

# Dreamwidth embeds comment IDs in element id attributes as "cmt<N>".
# Capture group 1 = the numeric comment ID.
_COMMENT_ID_RE = re.compile(r"cmt(\d+)")

# Date formats attempted when parsing Dreamwidth date strings.
# Ordinal suffixes (1st, 2nd, …) are stripped by a regex before these formats
# are tried, so only the bare "%B %d, %Y" form is needed for human-readable
# dates.
_DW_DATE_FORMATS = [
    "%B %d, %Y",          # e.g. "April 13, 2024"  (after suffix stripping)
    "%Y-%m-%d %H:%M",     # e.g. "2024-04-13 14:30"
    "%Y-%m-%dT%H:%M:%S",  # ISO 8601 from <time datetime="…"> attributes
]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _get(client: httpx.Client, url: str) -> Optional[BeautifulSoup]:
    """Perform an HTTP GET and return a parsed BeautifulSoup document.

    Uses the shared ``httpx.Client`` for connection reuse.  On any HTTP error
    (4xx / 5xx) or network-level error the exception is caught, a warning is
    logged, and ``None`` is returned so the caller can skip the URL gracefully.

    Parameters
    ----------
    client:
        The active ``httpx.Client`` session with connection pooling.
    url:
        Absolute URL to fetch.

    Returns
    -------
    BeautifulSoup | None
        Parsed HTML tree, or ``None`` if the request failed.
    """
    try:
        response = client.get(url, headers=HEADERS, follow_redirects=True, timeout=30)
        # Raise an exception for 4xx / 5xx responses so they are caught below.
        response.raise_for_status()
        # Parse with lxml for speed and lenient HTML handling.
        return BeautifulSoup(response.text, "lxml")
    except httpx.HTTPStatusError as exc:
        logger.warning("HTTP %s fetching %s", exc.response.status_code, url)
    except httpx.RequestError as exc:
        logger.warning("Request error fetching %s: %s", url, exc)
    return None


def _parse_date(text: str) -> Optional[datetime]:
    """Convert a Dreamwidth date string to a ``datetime`` object.

    Dreamwidth renders dates in several formats depending on context (archive
    links vs. post headers vs. ``<time>`` element attributes).  This function
    normalises ordinal day suffixes first, then tries each known format in
    order.

    Parameters
    ----------
    text:
        Raw date string as extracted from the HTML.

    Returns
    -------
    datetime | None
        Parsed datetime, or ``None`` if no known format matches.
    """
    text = text.strip()
    # Remove ordinal suffixes so "%B %d, %Y" matches "April 13th, 2024"
    # as well as "April 13, 2024".
    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text)
    for fmt in _DW_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _extract_text(tag) -> str:
    """Return the visible text content of a BeautifulSoup element.

    Concatenates all text nodes with a single space separator and strips
    leading/trailing whitespace.  Returns an empty string if ``tag`` is
    ``None``, making it safe to call without an explicit None-check.

    Parameters
    ----------
    tag:
        A BeautifulSoup ``Tag`` object, or ``None``.

    Returns
    -------
    str
        Normalised visible text content.
    """
    return tag.get_text(separator=" ", strip=True) if tag else ""


# ---------------------------------------------------------------------------
# Archive crawl: collect all post URLs
# ---------------------------------------------------------------------------


def _collect_post_urls(client: httpx.Client) -> list[str]:
    """Discover every post URL published on the blog.

    The Dreamwidth ``/archive`` page lists links to each calendar month.
    This function:

    1. Fetches the archive index.
    2. Identifies links that match the monthly-archive pattern
       (``/YYYY/MM/``) and fetches each of those pages.
    3. Collects every link matching the post pattern (``/NNNNNN.html``)
       found on both the main archive page and all monthly pages.
    4. Returns a sorted, deduplicated list of absolute post URLs.

    Parameters
    ----------
    client:
        Active ``httpx.Client`` session.

    Returns
    -------
    list[str]
        Sorted list of absolute post URLs with query strings stripped.
    """
    soup = _get(client, ARCHIVE_URL)
    if soup is None:
        logger.error("Could not fetch archive page.")
        return []

    # Use a set for automatic deduplication across archive pages.
    urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href: str = a["href"]

        # Resolve relative URLs against the base domain.
        if href.startswith("/"):
            href = BASE_URL + href

        # Skip off-site links entirely.
        if not href.startswith(BASE_URL):
            continue

        path = urlparse(href).path

        if _POST_URL_RE.search(path):
            # Direct post link on the archive page – strip any query string
            # (e.g. ?style=mine) to get the canonical URL.
            urls.add(href.split("?")[0])

        elif _MONTH_ARCHIVE_RE.search(path):
            # Monthly sub-archive – fetch it and harvest the post links within.
            time.sleep(REQUEST_DELAY)
            month_soup = _get(client, href)
            if month_soup:
                for ma in month_soup.find_all("a", href=True):
                    mhref: str = ma["href"]
                    # Resolve relative URLs on the monthly page as well.
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
    """Extract all structured data from a single post page.

    The parser uses multiple CSS-class fallbacks for each field because
    Dreamwidth journals can be styled in different ways (the "site skin"
    vs. custom journal styles expose slightly different markup).

    Parameters
    ----------
    soup:
        Parsed HTML of the post page.
    url:
        Canonical URL of the post (used to extract the entry ID).

    Returns
    -------
    dict | None
        Dictionary with keys ``entry_id``, ``url``, ``title``, ``author``,
        ``published_at``, ``body_html``, ``body_text``, ``comment_count``,
        ``tags``, and ``comments``.  Returns ``None`` if the entry ID cannot
        be determined (i.e. the URL does not match the expected pattern).
    """
    # --- Entry ID -----------------------------------------------------------
    # The numeric portion of the URL is the stable Dreamwidth entry ID.
    m = _POST_URL_RE.search(urlparse(url).path)
    if not m:
        return None
    entry_id = int(m.group(1))

    # --- Title --------------------------------------------------------------
    # Prefer the in-page heading element; fall back to the <title> tag which
    # includes a " - Ecosophia" suffix that is stripped below.
    title_tag = (
        soup.find("h2", class_="entry-title")
        or soup.find("title")
    )
    title = _extract_text(title_tag) if title_tag else "Untitled"
    # Remove the " - Ecosophia" site-name suffix added by the browser <title>.
    title = re.sub(r"\s*[-–]\s*Ecosophia\s*$", "", title, flags=re.IGNORECASE).strip()

    # --- Author -------------------------------------------------------------
    # Dreamwidth renders usernames inside <span class="ljuser"> elements.
    # A plain <a class="url"> is used as a fallback for minimalist themes.
    author_tag = soup.find("span", class_="ljuser") or soup.find(
        "a", class_="url"
    )
    author = _extract_text(author_tag) if author_tag else "ecosophia"

    # --- Publish date -------------------------------------------------------
    # Dreamwidth emits dates in several elements depending on the page style:
    #   <time class="datetime">    – machine-readable datetime attribute
    #   <span class="date">        – human-readable text
    #   <abbr class="published">   – microformat hAtom
    date_tag = (
        soup.find("time", class_="datetime")
        or soup.find("span", class_="date")
        or soup.find("abbr", class_="published")
    )
    published_at = None
    if date_tag:
        # Prefer the machine-readable ``datetime`` attribute; fall back to
        # visible text when the attribute is absent.
        dt_str = date_tag.get("datetime") or _extract_text(date_tag)
        published_at = _parse_date(dt_str)

    # --- Post body ----------------------------------------------------------
    # The body container differs across Dreamwidth themes:
    #   <div class="entry-content"> – most common
    #   <article class="entry">     – modern semantic markup
    #   <div id="entry-content">    – some legacy themes
    body_tag = (
        soup.find("div", class_="entry-content")
        or soup.find("article", class_="entry")
        or soup.find("div", id="entry-content")
    )
    # Preserve raw HTML for faithful rendering in the frontend.
    body_html = str(body_tag) if body_tag else ""
    # Plain-text version for search queries.
    body_text = _extract_text(body_tag) if body_tag else ""

    # --- Tags ---------------------------------------------------------------
    # Tags appear in a <div class="tag"> or <ul class="entry-tags"> section,
    # each tag wrapped in an <a> link.
    tag_names: list[str] = []
    tags_section = soup.find("div", class_="tag") or soup.find("ul", class_="entry-tags")
    if tags_section:
        for t in tags_section.find_all("a"):
            name = _extract_text(t).strip()
            if name:
                tag_names.append(name)

    # --- Comment count ------------------------------------------------------
    # Dreamwidth shows the count in a <span class="comment-count"> element or
    # in the text of the "N comments" read link.
    comment_count_tag = soup.find("span", class_="comment-count") or soup.find(
        "a", class_="entry-readlink"
    )
    comment_count = 0
    if comment_count_tag:
        # Extract the first integer from text like "42 comments".
        nums = re.findall(r"\d+", _extract_text(comment_count_tag))
        if nums:
            comment_count = int(nums[0])

    # --- Comments -----------------------------------------------------------
    # Comments live in a <div id="comments"> or <ol class="comment-thread">.
    # Each individual comment is a <div> whose class list contains "comment".
    comments: list[dict] = []
    comment_section = soup.find("div", id="comments") or soup.find(
        "ol", class_="comment-thread"
    )
    if comment_section:
        for comment_div in comment_section.find_all(
            "div", class_=re.compile(r"\bcomment\b")
        ):
            # Extract the Dreamwidth comment ID from the element's id attribute
            # (format: "cmt12345").
            c_id_str = comment_div.get("id", "")
            c_id_m = _COMMENT_ID_RE.search(c_id_str)
            comment_dw_id = int(c_id_m.group(1)) if c_id_m else None

            # Commenter username – try the standard ljuser span first.
            c_author_tag = comment_div.find("span", class_="comment-poster") or comment_div.find(
                "span", class_="ljuser"
            )
            c_author = _extract_text(c_author_tag) if c_author_tag else "Anonymous"

            # Comment timestamp – prefer the machine-readable <time> element.
            c_date_tag = comment_div.find("time") or comment_div.find(
                "span", class_="datetime"
            )
            c_date = None
            if c_date_tag:
                c_dt_str = c_date_tag.get("datetime") or _extract_text(c_date_tag)
                c_date = _parse_date(c_dt_str)

            # Comment body HTML and plain text.
            c_body_tag = comment_div.find("div", class_="comment-content") or comment_div.find(
                "div", class_="comment-body"
            )
            c_body_html = str(c_body_tag) if c_body_tag else ""
            c_body_text = _extract_text(c_body_tag) if c_body_tag else ""

            # Parent comment ID – Dreamwidth signals reply depth through
            # ``data-parent-comment`` attributes.  A numeric value means this
            # comment is a reply to that DW comment ID; absent or non-numeric
            # means it is a top-level comment.
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
    """Write a parsed post (and its associated tags and comments) to the database.

    The operation is an "upsert":

    * If a ``Post`` with the same ``entry_id`` already exists it is updated
      in-place so the row's primary key and any foreign-key references from
      other tables are preserved.
    * If no matching row exists a new ``Post`` is inserted.

    Tag handling
    ~~~~~~~~~~~~
    Tags are looked up or created individually.  The post's ``tags`` collection
    is replaced wholesale on each call so that removed tags are correctly
    de-associated.

    Comment handling
    ~~~~~~~~~~~~~~~~
    The full comment set is replaced on every upsert.  All existing
    ``Comment`` rows for the post are deleted (``db.flush()`` applies the
    deletes before the inserts to avoid unique-constraint violations), then the
    freshly parsed comments are inserted.  This is simpler than diffing and is
    acceptable because comment counts on Dreamwidth are relatively small.

    Parameters
    ----------
    db:
        Active SQLAlchemy session.  The caller is responsible for closing it.
    data:
        Parsed post dictionary as returned by ``_parse_post``.

    Returns
    -------
    Post
        The persisted (and refreshed) ``Post`` ORM object.
    """
    # Look up an existing row by the stable Dreamwidth entry ID.
    post = db.query(Post).filter(Post.entry_id == data["entry_id"]).first()
    if post is None:
        # First time we have seen this post – create a new row.
        post = Post(entry_id=data["entry_id"])
        db.add(post)

    # Update every scalar field to reflect the latest scraped values.
    post.url = data["url"]
    post.title = data["title"]
    post.author = data["author"]
    post.published_at = data["published_at"]
    post.body_html = data["body_html"]
    post.body_text = data["body_text"]
    post.comment_count = data["comment_count"]
    # Record when this row was last written.
    post.scraped_at = datetime.now(timezone.utc)

    # --- Tags ---------------------------------------------------------------
    # Resolve each tag name to a Tag row, creating it if necessary.
    tag_objs: list[Tag] = []
    for name in data["tags"]:
        tag = db.query(Tag).filter(Tag.name == name).first()
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
        tag_objs.append(tag)
    # Assigning the list replaces the old association completely (SQLAlchemy
    # handles the join-table diff automatically).
    post.tags = tag_objs

    # --- Comments -----------------------------------------------------------
    # Delete old comments before inserting the freshly-scraped set.
    # db.flush() sends the DELETE statements to the database immediately so
    # that subsequent INSERTs do not collide on unique constraints.
    for c in post.comments:
        db.delete(c)
    db.flush()

    for c_data in data["comments"]:
        comment = Comment(post=post, **c_data)
        db.add(comment)

    db.commit()
    # Refresh the instance so relationship collections reflect the new state.
    db.refresh(post)
    return post


# ---------------------------------------------------------------------------
# Public API: ScrapeProgress and run_scrape
# ---------------------------------------------------------------------------


class ScrapeProgress:
    """Mutable state container that tracks the progress of an active or completed scrape.

    A single instance of this class (``scrape_progress``) is held at module
    level and updated by ``run_scrape`` as it processes URLs.  The
    ``/scrape/status`` endpoint reads from this object without locking;
    Python's GIL guarantees atomic reads/writes for simple attribute types
    (``bool``, ``int``, ``str``), making this approach thread-safe for the
    single-worker deployment model used by this application.

    Attributes
    ----------
    running : bool
        ``True`` while a scrape is actively executing.
    total : int
        Total number of post URLs discovered on the archive page.
    done : int
        Number of URLs processed so far (successful or errored).
    errors : int
        Number of URLs that could not be fetched or parsed.
    current_url : str
        URL currently being processed; empty string when idle.
    started_at : datetime | None
        UTC timestamp when the current/last scrape began.
    finished_at : datetime | None
        UTC timestamp when the current/last scrape finished; ``None`` while running.
    """

    def __init__(self):
        self.running: bool = False
        self.total: int = 0
        self.done: int = 0
        self.errors: int = 0
        self.current_url: str = ""
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Serialise progress state to a plain dictionary suitable for JSON responses.

        Returns
        -------
        dict
            Progress snapshot with ISO 8601 timestamps.
        """
        return {
            "running": self.running,
            "total": self.total,
            "done": self.done,
            "errors": self.errors,
            "current_url": self.current_url,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# Module-level singleton – shared between run_scrape and the status endpoint.
# A single-worker deployment means this is always the authoritative state.
scrape_progress = ScrapeProgress()


def run_scrape(db: Session, force: bool = False) -> ScrapeProgress:
    """Scrape the entire ecosophia.dreamwidth.org blog and persist the results.

    This function is intended to be called from a background thread (see
    ``routers/scrape.py``).  It updates the module-level ``scrape_progress``
    singleton so that in-flight progress can be observed via the status
    endpoint.

    Parameters
    ----------
    db:
        SQLAlchemy session created by the calling thread.  Must be closed by
        the caller after this function returns.
    force:
        If ``False`` (default), posts already present in the database are
        skipped to avoid unnecessary network traffic on incremental runs.
        If ``True``, every discovered URL is re-fetched and re-upserted.

    Returns
    -------
    ScrapeProgress
        The (now completed) progress object, identical to the module-level
        ``scrape_progress`` singleton.
    """
    global scrape_progress

    # Guard against concurrent scrapes (e.g. two simultaneous POST requests).
    if scrape_progress.running:
        logger.info("Scrape already in progress; skipping.")
        return scrape_progress

    # Reset progress for this run.
    scrape_progress = ScrapeProgress()
    scrape_progress.running = True
    scrape_progress.started_at = datetime.now(timezone.utc)

    try:
        # A single httpx.Client is used for the entire scrape so that TCP
        # connections are reused across requests (HTTP keep-alive / HTTP/2).
        with httpx.Client() as client:
            urls = _collect_post_urls(client)
            scrape_progress.total = len(urls)

            for url in urls:
                scrape_progress.current_url = url

                # Skip already-scraped posts unless a full re-scrape was requested.
                if not force:
                    m = _POST_URL_RE.search(urlparse(url).path)
                    if m:
                        existing = (
                            db.query(Post)
                            .filter(Post.entry_id == int(m.group(1)))
                            .first()
                        )
                        if existing:
                            # Post is already in the database; move on.
                            scrape_progress.done += 1
                            continue

                # Polite delay before each fetch.
                time.sleep(REQUEST_DELAY)

                soup = _get(client, url)
                if soup is None:
                    # Network/HTTP error; count as error and continue.
                    scrape_progress.errors += 1
                    scrape_progress.done += 1
                    continue

                data = _parse_post(soup, url)
                if data is None:
                    # URL did not match expected post pattern; skip.
                    scrape_progress.errors += 1
                    scrape_progress.done += 1
                    continue

                try:
                    _upsert_post(db, data)
                except Exception as exc:
                    # Database write failed; roll back the current transaction
                    # so the session remains usable for subsequent posts.
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
        # Catch-all for unexpected failures (e.g. loss of DB connectivity)
        # so the finally block always runs and ``running`` is reset.
        logger.exception("Scrape failed: %s", exc)
    finally:
        # Always clear the running flag and record the finish time so that
        # the status endpoint reflects the correct state after any exit path.
        scrape_progress.running = False
        scrape_progress.finished_at = datetime.now(timezone.utc)
        scrape_progress.current_url = ""

    return scrape_progress

