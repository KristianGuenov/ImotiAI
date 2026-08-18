from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ExtractedItem(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    image: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    texts: List[str] = Field(default_factory=list)
    rawText: Optional[str] = None
    # v2 raw harvester fields (all optional for backwards compat)
    description: Optional[str] = None
    raw_jsonld: List[Dict[str, Any]] = Field(default_factory=list)
    raw_kv: List[Dict[str, Any]] = Field(default_factory=list)
    raw_text_blocks: List[Dict[str, Any]] = Field(default_factory=list)
    raw_state_blobs: List[Dict[str, Any]] = Field(default_factory=list)
    raw_contacts: Dict[str, Any] = Field(default_factory=dict)
    raw_media: Dict[str, Any] = Field(default_factory=dict)
    signals: Dict[str, Any] = Field(default_factory=dict)


class ExtractionIn(BaseModel):
    dataVersion: int = 1
    sourceUrl: str = Field(..., description="URL of the page where extraction occurred")
    pageTitle: Optional[str] = None
    extractedAt: datetime
    meta: Dict[str, Any] = Field(default_factory=dict)
    items: List[ExtractedItem] = Field(default_factory=list)


class ExtractionOut(BaseModel):
    id: int
    dataVersion: int
    sourceUrl: str
    pageTitle: Optional[str]
    extractedAt: datetime
    receivedAt: datetime
    itemCount: int
    meta: Dict[str, Any]
    items: List[ExtractedItem]


class ExtractionListItem(BaseModel):
    id: int
    sourceUrl: str
    pageTitle: Optional[str]
    extractedAt: datetime
    receivedAt: datetime
    itemCount: int


class ExtractionListOut(BaseModel):
    items: List[ExtractionListItem]


class DetailQueueOut(BaseModel):
    urls: List[str] = Field(default_factory=list)


# --- Inventory lifecycle ---


class InventoryCycleStartIn(BaseModel):
    domain: str
    targetsExpected: int = Field(default=0, ge=0)
    meta: Dict[str, Any] = Field(default_factory=dict)


class InventoryCycleStartOut(BaseModel):
    id: int
    domain: str
    status: str
    startedAt: datetime
    targetsExpected: int


class InventoryCycleCompleteIn(BaseModel):
    success: bool
    targetsSucceeded: int = Field(default=0, ge=0)
    targetsFailed: int = Field(default=0, ge=0)
    meta: Dict[str, Any] = Field(default_factory=dict)


class InventoryCycleCompleteOut(BaseModel):
    id: int
    domain: str
    status: str
    completedAt: datetime
    targetsExpected: int
    targetsSucceeded: int
    targetsFailed: int
    listingsSeen: int
    listingsMissing: int
    listingsDeactivated: int
    reconciliationApplied: bool
    message: str


# --- Batch create (speed path, used by detail_runner when enabled) ---


class ExtractionBatchIn(BaseModel):
    items: List[ExtractionIn] = Field(default_factory=list)


class ExtractionBatchResult(BaseModel):
    id: int
    sourceUrl: str


class ExtractionBatchOut(BaseModel):
    results: List[ExtractionBatchResult] = Field(default_factory=list)