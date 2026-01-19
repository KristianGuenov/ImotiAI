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


# --- Batch create (speed path, used by detail_runner when enabled) ---


class ExtractionBatchIn(BaseModel):
    items: List[ExtractionIn] = Field(default_factory=list)


class ExtractionBatchResult(BaseModel):
    id: int
    sourceUrl: str


class ExtractionBatchOut(BaseModel):
    results: List[ExtractionBatchResult] = Field(default_factory=list)
