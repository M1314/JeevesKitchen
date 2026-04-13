"""
JeevesKitchen – SQLAlchemy ORM models.

Three tables represent the structured data scraped from
``ecosophia.dreamwidth.org``:

Post
    The primary entity.  Stores metadata (title, author, date) together with
    both the raw HTML body and a plain-text derivative for full-text search.

Tag
    A keyword label applied to one or more posts.  The relationship is
    many-to-many: a post may have several tags and a tag may appear on many
    posts.  The association is recorded in the ``post_tags`` join table.

Comment
    A reader reply beneath a specific post.  Comments preserve the
    Dreamwidth-assigned numeric ID and the parent comment ID so that threaded
    discussion trees can be reconstructed client-side.

All models inherit from ``Base`` (defined in ``database.py``), which registers
them with the shared metadata object used by ``Base.metadata.create_all``.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import relationship

from database import Base

# ---------------------------------------------------------------------------
# Association table: posts ↔ tags (many-to-many)
# ---------------------------------------------------------------------------

# This is a pure join table with no additional columns; SQLAlchemy's
# ``secondary`` parameter on the relationship handles inserts/deletes
# automatically when ``post.tags`` is mutated.
post_tags = Table(
    "post_tags",
    Base.metadata,
    # Foreign key to the post being tagged.
    Column("post_id", Integer, ForeignKey("posts.id"), primary_key=True),
    # Foreign key to the tag being applied.
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)


# ---------------------------------------------------------------------------
# Tag model
# ---------------------------------------------------------------------------

class Tag(Base):
    """A keyword label that can be applied to one or more posts.

    Dreamwidth allows authors to tag entries with free-form keywords.  Each
    unique tag string is stored once here; the ``post_tags`` table records
    which posts carry each tag.

    Attributes
    ----------
    id : int
        Surrogate primary key (auto-assigned by the database).
    name : str
        The tag text exactly as it appears on Dreamwidth (case-sensitive,
        max 255 characters).  Indexed and enforced unique.
    posts : list[Post]
        Back-reference to all ``Post`` objects that carry this tag.  Populated
        automatically via the SQLAlchemy relationship.
    """

    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    # The tag label as scraped from the blog – unique to avoid duplicates.
    name = Column(String(255), unique=True, nullable=False, index=True)

    # Bidirectional many-to-many relationship with Post via the post_tags
    # join table.  SQLAlchemy manages the join-table rows automatically.
    posts = relationship("Post", secondary=post_tags, back_populates="tags")


# ---------------------------------------------------------------------------
# Post model
# ---------------------------------------------------------------------------

class Post(Base):
    """A single blog entry scraped from ecosophia.dreamwidth.org.

    Both the raw HTML body and a plain-text version are stored so that:

    * The HTML can be rendered as-is in a future frontend without re-fetching.
    * The plain text is used for keyword search without HTML noise.

    Attributes
    ----------
    id : int
        Surrogate primary key (auto-assigned by the database).
    entry_id : int
        The numeric identifier embedded in the Dreamwidth URL
        (e.g. ``https://ecosophia.dreamwidth.org/12345.html`` → ``12345``).
        Used as the stable public identifier in the API.
    url : str
        The canonical URL of the post (without query parameters).
    title : str
        The post heading as extracted from the ``<h2 class="entry-title">``
        element, or the ``<title>`` tag as a fallback.
    author : str
        The Dreamwidth username of the post author.
    published_at : datetime | None
        Publish timestamp parsed from the page's date element.  ``None`` if
        the scraper could not determine a date.
    body_html : str
        The raw inner HTML of the post body element, preserved for faithful
        rendering.
    body_text : str
        Plain-text extraction of the body (whitespace-normalised) used for
        full-text keyword search.
    comment_count : int
        Number of comments as reported on the post page.
    scraped_at : datetime
        UTC timestamp of when this row was last written by the scraper.
    tags : list[Tag]
        Tags applied to this post.  Managed via the ``post_tags`` join table.
    comments : list[Comment]
        All scraped comments for this post.  Cascade-deleted when the post
        is deleted.
    """

    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)

    # Dreamwidth numeric entry ID extracted from the URL (e.g. 12345.html → 12345).
    # This is the stable public identifier exposed through the API.
    entry_id = Column(Integer, unique=True, nullable=False, index=True)

    # Canonical post URL (query string stripped) – also unique per post.
    url = Column(String(512), unique=True, nullable=False)

    title = Column(String(512))
    author = Column(String(255))
    published_at = Column(DateTime, nullable=True)

    # Raw HTML body preserved so it can be re-rendered later without
    # re-fetching from Dreamwidth.
    body_html = Column(Text)

    # Plain-text derivative used for keyword search queries.
    body_text = Column(Text)

    # Denormalised comment count sourced directly from the post page.
    comment_count = Column(Integer, default=0)

    # Recorded as UTC-aware so it can be compared across time zones.
    scraped_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Many-to-many relationship with Tag via the post_tags association table.
    tags = relationship("Tag", secondary=post_tags, back_populates="posts")

    # One-to-many relationship with Comment.  Cascade ensures comments are
    # removed automatically if their parent post is deleted.
    comments = relationship("Comment", back_populates="post", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Comment model
# ---------------------------------------------------------------------------

class Comment(Base):
    """A reader comment posted beneath a ``Post``.

    Dreamwidth assigns each comment a unique numeric ID that appears in the
    page's HTML (``id="cmt12345"``).  The parent comment ID is preserved so
    that the original threaded reply structure can be reconstructed without
    storing the full nesting in the database.

    Attributes
    ----------
    id : int
        Surrogate primary key.
    post_id : int
        Foreign key linking this comment to its parent ``Post``.
    comment_dw_id : int | None
        The Dreamwidth-assigned comment identifier extracted from the
        ``id="cmt<N>"`` HTML attribute.  ``None`` if not found on the page.
    author : str
        Username of the commenter, or ``"Anonymous"`` for logged-out replies.
    published_at : datetime | None
        Timestamp of the comment, or ``None`` if unparseable.
    body_html : str
        Raw HTML of the comment body.
    body_text : str
        Plain-text version of the comment body.
    parent_comment_dw_id : int | None
        ``comment_dw_id`` of the comment this reply is directed at, allowing
        clients to reconstruct threaded trees.  ``None`` for top-level
        comments.
    post : Post
        Back-reference to the owning ``Post`` instance.
    """

    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)

    # Foreign key to the post this comment belongs to.
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False)

    # Dreamwidth comment ID from the HTML anchor (e.g. id="cmt12345" → 12345).
    # Indexed to allow efficient lookup when reconstructing comment threads.
    comment_dw_id = Column(Integer, nullable=True, index=True)

    # Commenter's Dreamwidth username.  Defaults to "Anonymous" for
    # non-authenticated commenters.
    author = Column(String(255))

    published_at = Column(DateTime, nullable=True)

    # Full HTML of the comment preserved for rich rendering.
    body_html = Column(Text)

    # Plain-text version for display and search.
    body_text = Column(Text)

    # The comment_dw_id of this comment's parent, or None for top-level replies.
    # Storing the DW ID (rather than the surrogate key) keeps this value stable
    # if rows are re-created during a force-rescrape.
    parent_comment_dw_id = Column(Integer, nullable=True)

    # Back-reference to the owning Post.
    post = relationship("Post", back_populates="comments")

