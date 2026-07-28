from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


def new_source_id() -> str:
    return f"src_{uuid4().hex[:16]}"


def new_job_id() -> str:
    return f"ing_{uuid4().hex[:16]}"


def new_evidence_id() -> str:
    return f"evd_{uuid4().hex[:16]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    created_at: datetime = Field(default_factory=utc_now, index=True)


class IngestionJob(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(default_factory=new_job_id, index=True, unique=True)
    input_type: str = Field(index=True)
    original_url: str | None = Field(default=None, index=True)
    input_path: str | None = None
    canonical_url: str | None = Field(default=None, index=True)
    source_platform: str | None = Field(default=None, index=True)
    media_type: str = Field(default="unknown", index=True)
    status: str = Field(default="queued", index=True)
    stage: str = Field(default="queued", index=True)
    attempt_count: int = Field(default=0)
    max_attempts: int = Field(default=2)
    error_code: str | None = None
    error_message: str | None = None
    source_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class SourceEvidence(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    evidence_id: str = Field(default_factory=new_evidence_id, index=True, unique=True)
    source_id: str = Field(index=True)
    kind: str = Field(default="transcript")
    origin: str = Field(index=True)
    language: str | None = None
    full_text: str
    segments: list[dict[str, object]] = Field(default_factory=list, sa_column=Column(JSON))
    metadata_json: dict[str, object] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now, index=True)
