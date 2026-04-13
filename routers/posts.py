"""
JeevesKitchen – post and tag query endpoints.

These read-only endpoints expose the content scraped from
``ecosophia.dreamwidth.org`` and stored in the database.

Post endpoints
--------------
GET /posts
    Paginated list of posts, newest first.  Supports three optional filters:
    ``tag`` (exact tag name), ``author`` (case-insensitive substring), and
    ``search`` (case-insensitive keyword search across title and body text).

GET /posts/{entry_id}
    Full detail for a single post identified by its Dreamwidth entry ID,
    including the complete HTML/text body and all scraped comments.

Tag endpoints
-------------
GET /tags
    Alphabetical list of all unique tag names with their post counts.

GET /tags/{name}/posts
    Paginated list of posts carrying a specific tag, newest first.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from database import get_db
from models import Post, Tag

# Routes mounted under /posts
router = APIRouter(prefix="/posts", tags=["posts"])


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _post_summary(post: Post) -> dict:
    """Serialise a ``Post`` to a lightweight summary dictionary.

    Omits the full HTML / text body so that list responses remain compact.
    The ``body_text`` and ``body_html`` fields are included only in the
    detail endpoint response.

    Parameters
    ----------
    post : Post
        Hydrated ORM instance (tags relationship must be loaded).

    Returns
    -------
    dict
        JSON-serialisable summary with ISO 8601 timestamps.
    """
    return {
        "entry_id": post.entry_id,
        "url": post.url,
        "title": post.title,
        "author": post.author,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "comment_count": post.comment_count,
        # Flatten the tags relationship to a plain list of name strings.
        "tags": [t.name for t in post.tags],
        "scraped_at": post.scraped_at.isoformat() if post.scraped_at else None,
    }


def _comment_detail(c) -> dict:
    """Serialise a ``Comment`` to a plain dictionary.

    The raw HTML body is intentionally excluded from the API response to keep
    responses clean; only the plain-text body is returned.  If rich rendering
    is needed in the future, ``body_html`` can be added here.

    Parameters
    ----------
    c : Comment
        ORM comment instance.

    Returns
    -------
    dict
        JSON-serialisable comment detail.
    """
    return {
        "comment_dw_id": c.comment_dw_id,
        "author": c.author,
        "published_at": c.published_at.isoformat() if c.published_at else None,
        "body_text": c.body_text,
        # Preserved so clients can reconstruct threaded discussion trees.
        "parent_comment_dw_id": c.parent_comment_dw_id,
    }


# ---------------------------------------------------------------------------
# Post list and detail endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List posts",
    response_description="Paginated post summaries",
)
def list_posts(
    db: Session = Depends(get_db),
    tag: Optional[str] = Query(None, description="Filter by exact tag name"),
    author: Optional[str] = Query(None, description="Filter by author (case-insensitive substring)"),
    search: Optional[str] = Query(None, description="Keyword search across title and body text"),
    skip: int = Query(0, ge=0, description="Number of results to skip (for pagination)"),
    limit: int = Query(20, ge=1, le=200, description="Maximum number of results to return"),
) -> dict:
    """Return a paginated, filtered list of posts ordered newest-first.

    All three filter parameters are optional and can be combined:

    * **tag** performs an exact-match join against the ``tags`` table.
    * **author** performs a case-insensitive ``LIKE`` search.
    * **search** performs a case-insensitive ``LIKE`` search across both
      ``title`` and ``body_text`` (OR logic).

    The response envelope includes ``total`` (matching rows before pagination)
    so clients can calculate the number of pages.
    """
    # Base query – eager-load tags to avoid N+1 queries when serialising.
    q = db.query(Post).options(joinedload(Post.tags)).order_by(Post.published_at.desc())

    # Apply optional filters.
    if tag:
        # Inner join against the tags table to restrict to posts with this tag.
        q = q.join(Post.tags).filter(Tag.name == tag)
    if author:
        # Case-insensitive substring match (portable across SQLite and PostgreSQL).
        q = q.filter(Post.author.ilike(f"%{author}%"))
    if search:
        # Match the keyword in either the title or the plain-text body.
        q = q.filter(
            or_(
                Post.title.ilike(f"%{search}%"),
                Post.body_text.ilike(f"%{search}%"),
            )
        )

    # Count before applying pagination so the client knows the full result size.
    total = q.count()
    posts = q.offset(skip).limit(limit).all()

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "results": [_post_summary(p) for p in posts],
    }


@router.get(
    "/{entry_id}",
    summary="Get a single post",
    response_description="Full post detail including body and comments",
)
def get_post(entry_id: int, db: Session = Depends(get_db)) -> dict:
    """Return full detail for a single post identified by its Dreamwidth entry ID.

    The response includes the complete plain-text and HTML bodies, all tags,
    and a flat list of comments.  Comment ``parent_comment_dw_id`` values can
    be used to reconstruct the original threaded discussion tree client-side.

    Parameters
    ----------
    entry_id : int
        The numeric Dreamwidth entry ID (the ``NNNNNN`` portion of the post
        URL ``/NNNNNN.html``).

    Raises
    ------
    HTTPException (404)
        If no post with the given entry ID exists in the database.
    """
    post = (
        db.query(Post)
        # Eager-load both relationships to avoid additional queries during serialisation.
        .options(joinedload(Post.tags), joinedload(Post.comments))
        .filter(Post.entry_id == entry_id)
        .first()
    )
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found.")

    return {
        # Include the standard summary fields …
        **_post_summary(post),
        # … plus the full body content excluded from list responses.
        "body_text": post.body_text,
        "body_html": post.body_html,
        "comments": [_comment_detail(c) for c in post.comments],
    }


# ---------------------------------------------------------------------------
# Tag endpoints (separate router mounted at /tags)
# ---------------------------------------------------------------------------

# Routes mounted under /tags – kept in this file to co-locate related serialisation.
tags_router = APIRouter(prefix="/tags", tags=["tags"])


@tags_router.get(
    "",
    summary="List all tags",
    response_description="Alphabetical list of tags with post counts",
)
def list_tags(db: Session = Depends(get_db)) -> list:
    """Return every tag in the database sorted alphabetically.

    Each item includes the tag's surrogate ``id``, its ``name``, and a
    ``post_count`` derived from the loaded relationship.  This endpoint is
    primarily useful for populating a tag-browser or autocomplete widget.
    """
    tags = db.query(Tag).order_by(Tag.name).all()
    return [{"id": t.id, "name": t.name, "post_count": len(t.posts)} for t in tags]


@tags_router.get(
    "/{name}/posts",
    summary="List posts by tag",
    response_description="Paginated posts carrying the specified tag",
)
def posts_by_tag(
    name: str,
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0, description="Number of results to skip"),
    limit: int = Query(20, ge=1, le=200, description="Maximum number of results to return"),
) -> dict:
    """Return posts that carry the specified tag, newest first.

    The tag name is matched exactly (case-sensitive) as stored during scraping.

    Parameters
    ----------
    name : str
        Exact tag name (e.g. ``"peak oil"``).

    Raises
    ------
    HTTPException (404)
        If no tag with the given name exists in the database.
    """
    # Verify the tag exists before constructing the post query.
    tag = db.query(Tag).filter(Tag.name == name).first()
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found.")

    # Perform pagination at the database level rather than slicing in Python
    # to avoid loading every matching post into memory.
    total_q = db.query(Post).join(Post.tags).filter(Tag.name == name)
    total = total_q.count()
    posts = (
        total_q.options(joinedload(Post.tags))
        .order_by(Post.published_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return {
        "tag": name,
        "total": total,
        "skip": skip,
        "limit": limit,
        "results": [_post_summary(p) for p in posts],
    }

