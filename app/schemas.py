from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Category(StrEnum):
    eat = "eat"
    drink = "drink"
    play = "play"
    entertainment = "entertainment"
    stay = "stay"
    transport = "transport"
    unknown = "unknown"


STANDARD_TAGS = {
    "拍照好看",
    "本地特色",
    "小众",
    "热门",
    "排队",
    "预约",
    "性价比高",
    "贵",
    "亲子",
    "情侣",
    "朋友",
    "独行",
    "雨天",
    "夜景",
    "购物",
    "甜品",
    "咖啡",
    "海鲜",
    "烧鸟",
    "拉面",
    "博物馆",
    "公园",
    "寺庙",
    "海边",
    "温泉",
    "避坑",
}


class CollectSourceRequest(BaseModel):
    input_type: Literal["url", "image", "text"] = "url"
    url: str | None = None
    title: str = Field(min_length=1)
    body_text: str = Field(min_length=1)
    source_platform: str | None = None
    cover_image_url: str | None = None


class CreateIngestionRequest(BaseModel):
    input_type: Literal["url"] = "url"
    url: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("url must not be empty")
        return normalized


class IngestionAcceptedResponse(BaseModel):
    job_id: str
    status: str


class IngestionStatusResponse(BaseModel):
    job_id: str
    status: str
    stage: str
    source_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    media_egress: str | None = None
    failure_stage: str | None = None
    progress_percent: int = 0
    progress_message: str | None = None
    progress_updated_at: datetime | None = None


class AnalyzeImageRequest(BaseModel):
    input_type: Literal["image"] = "image"
    image_base64: str = Field(min_length=1)
    title_hint: str | None = None
    source_platform: str | None = None


class ClientConfigResponse(BaseModel):
    api_base_url: str
    service: str
    llm_model: str


class AnalyzeSourceResponse(BaseModel):
    is_travel_related: bool
    reason: str | None = None
    confidence: float
    title: str | None = None
    body_text: str | None = None
    destination: str
    category: str
    location_name: str | None
    normalized_tags: list[str]
    raw_tags: list[str]


class TravelSourceCard(BaseModel):
    source_id: str
    title: str
    original_url: str | None
    source_platform: str | None
    cover_image_url: str | None
    destination: str
    category: str
    location_name: str | None
    normalized_tags: list[str]
    raw_tags: list[str]
    created_at: datetime


class CollectSourceResponse(BaseModel):
    saved: bool
    reason: str | None = None
    source: TravelSourceCard | None = None


class SourceListResponse(BaseModel):
    items: list[TravelSourceCard]


class ChatRecommendRequest(BaseModel):
    message: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=20)


class UsedSource(BaseModel):
    source_id: str
    title: str
    original_url: str | None
    cover_image_url: str | None
    source_platform: str | None
    destination: str
    category: str
    normalized_tags: list[str]


class ChatRecommendResponse(BaseModel):
    answer: str
    used_sources: list[UsedSource]
