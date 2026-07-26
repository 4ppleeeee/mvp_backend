from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


def new_source_id() -> str:
    return f"src_{uuid4().hex[:16]}"


class TravelSource(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source_id: str = Field(default_factory=new_source_id, index=True, unique=True)
    title: str
    body_text: str
    original_url: str | None = Field(default=None, index=True)
    source_platform: str | None = Field(default=None, index=True)
    cover_image_url: str | None = None
    destination: str = Field(index=True)
    category: str = Field(index=True)
    location_name: str | None = None
    normalized_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    raw_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)

