from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Boolean, Integer, String, DateTime, Text, ForeignKey, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

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


class InventoryCycle(Base):
    """One full inventory pass for a single domain.

    A cycle may reconcile missing listings only when every configured target for
    the domain completed successfully and the final inventory passes safety checks.
    """

    __tablename__ = "inventory_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # running | completed | failed | rejected
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", index=True)

    targets_expected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    targets_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    targets_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    listings_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    listings_missing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    listings_deactivated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Listing(Base):
    """Canonical state per listing URL.

    Why this exists:
    - We want to know whether a listing has had a *detail* scrape without creating
      extra ExtractionRun/ExtractionItem rows.
    - We want to re-queue a listing for detail scraping when its index snapshot
      changes (often the title contains the price).
    - We want to track whether a listing is still present in the source inventory
      without creating historical ExtractionItem duplicates for unchanged listings.

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

    # Detail media + raw snapshot
    images: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    # Inventory lifecycle state
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_seen_cycle_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("inventory_cycles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    missing_cycles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inactive_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Detail state ("detail modifier")
    detail_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    detail_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


Index("ix_listings_domain_detail_done", Listing.domain, Listing.detail_done)
Index("ix_listings_domain_active", Listing.domain, Listing.active)
Index("ix_listings_domain_last_seen_cycle", Listing.domain, Listing.last_seen_cycle_id)