from collections.abc import Iterator
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings


def create_db_engine(settings: Settings) -> Engine:
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.removeprefix("sqlite:///")
        if db_path not in (":memory:", ""):
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(settings.database_url, connect_args={"check_same_thread": False})
    return create_engine(settings.database_url)


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)
    if engine.dialect.name != "sqlite":
        return
    columns = {column["name"] for column in inspect(engine).get_columns("ingestionjob")}
    source_columns = {column["name"] for column in inspect(engine).get_columns("travelsource")}
    job_columns = columns
    with engine.begin() as connection:
        if "media_egress" not in columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN media_egress VARCHAR"))
        if "failure_stage" not in columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN failure_stage VARCHAR"))
        if "analysis_json" not in columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN analysis_json JSON"))
        if "evidence_text" not in columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN evidence_text TEXT"))
        if "ingest_decision" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN ingest_decision VARCHAR DEFAULT 'pending'"))
        if "policy_version" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN policy_version VARCHAR DEFAULT 'v1'"))
        if "reviewed_at" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN reviewed_at DATETIME"))
        if "reviewed_by" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN reviewed_by VARCHAR"))
        if "review_reason" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN review_reason VARCHAR"))
        if "evidence_origin" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN evidence_origin VARCHAR"))
        if "evidence_language" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN evidence_language VARCHAR"))
        if "evidence_segments" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN evidence_segments JSON"))
        if "evidence_metadata_json" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN evidence_metadata_json JSON"))
        if "progress_percent" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN progress_percent INTEGER DEFAULT 0"))
        if "progress_message" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN progress_message VARCHAR"))
        if "progress_updated_at" not in job_columns:
            connection.execute(text("ALTER TABLE ingestionjob ADD COLUMN progress_updated_at DATETIME"))
        if "summary_text" not in source_columns:
            connection.execute(text("ALTER TABLE travelsource ADD COLUMN summary_text TEXT"))


def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session
