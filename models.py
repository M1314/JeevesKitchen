from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import relationship

from database import Base

# Many-to-many: posts <-> tags
post_tags = Table(
    "post_tags",
    Base.metadata,
    Column("post_id", Integer, ForeignKey("posts.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False, index=True)

    posts = relationship("Post", secondary=post_tags, back_populates="tags")


class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    # Dreamwidth numeric entry ID extracted from the URL (e.g. 12345.html → 12345)
    entry_id = Column(Integer, unique=True, nullable=False, index=True)
    url = Column(String(512), unique=True, nullable=False)
    title = Column(String(512))
    author = Column(String(255))
    published_at = Column(DateTime, nullable=True)
    # Raw HTML body preserved so it can be re-parsed later
    body_html = Column(Text)
    # Plain-text version for full-text search
    body_text = Column(Text)
    comment_count = Column(Integer, default=0)
    scraped_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    tags = relationship("Tag", secondary=post_tags, back_populates="posts")
    comments = relationship("Comment", back_populates="post", cascade="all, delete-orphan")


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False)
    # Dreamwidth comment ID (from the anchor / data attribute)
    comment_dw_id = Column(Integer, nullable=True, index=True)
    author = Column(String(255))
    published_at = Column(DateTime, nullable=True)
    body_html = Column(Text)
    body_text = Column(Text)
    parent_comment_dw_id = Column(Integer, nullable=True)

    post = relationship("Post", back_populates="comments")
