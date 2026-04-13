"""
Read-only query endpoints for scraped content.

GET /posts               – list posts (paginated, filterable by tag/author/search)
GET /posts/{entry_id}    – single post with tags and comments
GET /tags                – list all tags
GET /tags/{name}/posts   – posts for a specific tag
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from database import get_db
from models import Post, Tag

router = APIRouter(prefix="/posts", tags=["posts"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post_summary(post: Post) -> dict:
    return {
        "entry_id": post.entry_id,
        "url": post.url,
        "title": post.title,
        "author": post.author,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "comment_count": post.comment_count,
        "tags": [t.name for t in post.tags],
        "scraped_at": post.scraped_at.isoformat() if post.scraped_at else None,
    }


def _comment_detail(c) -> dict:
    return {
        "comment_dw_id": c.comment_dw_id,
        "author": c.author,
        "published_at": c.published_at.isoformat() if c.published_at else None,
        "body_text": c.body_text,
        "parent_comment_dw_id": c.parent_comment_dw_id,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_posts(
    db: Session = Depends(get_db),
    tag: Optional[str] = Query(None, description="Filter by tag name"),
    author: Optional[str] = Query(None, description="Filter by author"),
    search: Optional[str] = Query(None, description="Full-text search in title/body"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
):
    """List posts, newest first. Supports filtering by tag, author, and keyword search."""
    q = db.query(Post).options(joinedload(Post.tags)).order_by(Post.published_at.desc())

    if tag:
        q = q.join(Post.tags).filter(Tag.name == tag)
    if author:
        q = q.filter(Post.author.ilike(f"%{author}%"))
    if search:
        q = q.filter(
            or_(
                Post.title.ilike(f"%{search}%"),
                Post.body_text.ilike(f"%{search}%"),
            )
        )

    total = q.count()
    posts = q.offset(skip).limit(limit).all()

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "results": [_post_summary(p) for p in posts],
    }


@router.get("/{entry_id}")
def get_post(entry_id: int, db: Session = Depends(get_db)):
    """Return a single post (including full body and comments) by its Dreamwidth entry ID."""
    post = (
        db.query(Post)
        .options(joinedload(Post.tags), joinedload(Post.comments))
        .filter(Post.entry_id == entry_id)
        .first()
    )
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found.")

    return {
        **_post_summary(post),
        "body_text": post.body_text,
        "body_html": post.body_html,
        "comments": [_comment_detail(c) for c in post.comments],
    }


# ---------------------------------------------------------------------------
# Tag routes
# ---------------------------------------------------------------------------

tags_router = APIRouter(prefix="/tags", tags=["tags"])


@tags_router.get("")
def list_tags(db: Session = Depends(get_db)):
    """Return all tags sorted alphabetically."""
    tags = db.query(Tag).order_by(Tag.name).all()
    return [{"id": t.id, "name": t.name, "post_count": len(t.posts)} for t in tags]


@tags_router.get("/{name}/posts")
def posts_by_tag(
    name: str,
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
):
    """Return posts associated with a specific tag."""
    tag = db.query(Tag).filter(Tag.name == name).first()
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found.")

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
