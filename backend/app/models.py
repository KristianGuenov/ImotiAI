from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Boolean, Integer, String, DateTime, Text, ForeignKey, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    page_title: Mapped[str | None] = mapped_column(String(512), nullable=True)

    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    items: Mapped[list["ExtractionItem"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class ExtractionItem(Base):
    __tablename__ = "extraction_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("extraction_runs.id", ondelete="CASCADE"), nullable=False)
    run: Mapped["ExtractionRun"] = relationship(back_populates="items")

    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    item_url: Mapped[str | None] = mapped_column(String(2048), nullable=True, index=True)
    image: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    images: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    texts: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)


Index("ix_extraction_items_run_id", ExtractionItem.run_id)


class Listing(Base):
    """Canonical state per listing URL.

    Why this exists:
    - We want to know whether a listing has had a *detail* scrape without creating
      extra ExtractionRun/ExtractionItem rows.
    - We want to re-queue a listing for detail scraping when its index snapshot
      changes (often the title contains the price).

    NOTE: This is not meant to replace your historical runs; it's a fast
    "latest state" table.
    """

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Latest index snapshot (lightweight)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    image: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    # Detail state ("detail modifier")
    detail_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    detail_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


Index("ix_listings_domain_detail_done", Listing.domain, Listing.detail_done)
