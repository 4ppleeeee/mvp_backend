import asyncio

from sqlmodel import Session, select

from app.ingestion.domain import EvidenceBundle
from app.llm import normalize_analysis
from app.models import IngestionJob, SourceEvidence, TravelSource


class IngestionService:
    def __init__(self, *, session: Session, llm_client: object, pipeline: object) -> None:
        self._session = session
        self._llm_client = llm_client
        self._pipeline = pipeline

    def run(self, job_id: str) -> IngestionJob:
        job = self._session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
        try:
            self._update(job, status="running", stage="extracting")
            bundle: EvidenceBundle = self._pipeline.extract(job.original_url or "", job.job_id)
            self._update(
                job,
                stage="analyzing",
                canonical_url=bundle.metadata.canonical_url,
                source_platform=bundle.metadata.source_platform,
                media_type="video",
            )
            analysis = normalize_analysis(
                asyncio.run(
                    self._llm_client.analyze_source(
                        title=bundle.metadata.title,
                        body_text=bundle.transcript.full_text,
                        url=bundle.metadata.canonical_url,
                        source_platform=bundle.metadata.source_platform,
                    )
                )
            )
            self._update(job, stage="saving")
            if analysis.is_travel_related:
                source = TravelSource(
                    title=analysis.title or bundle.metadata.title,
                    body_text=analysis.body_text or bundle.transcript.full_text,
                    original_url=job.original_url,
                    source_platform=bundle.metadata.source_platform,
                    cover_image_url=bundle.metadata.thumbnail_url,
                    destination=analysis.destination,
                    category=analysis.category,
                    location_name=analysis.location_name,
                    normalized_tags=analysis.normalized_tags,
                    raw_tags=analysis.raw_tags,
                )
                self._session.add(source)
                self._session.flush()
                self._session.add(
                    SourceEvidence(
                        source_id=source.source_id,
                        origin=bundle.transcript.origin.value,
                        language=bundle.transcript.language,
                        full_text=bundle.transcript.full_text,
                        segments=[
                            {
                                "start_seconds": segment.start_seconds,
                                "end_seconds": segment.end_seconds,
                                "text": segment.text,
                            }
                            for segment in bundle.transcript.segments
                        ],
                        metadata_json={
                            "title": bundle.metadata.title,
                            "author": bundle.metadata.author,
                            "published_at": bundle.metadata.published_at,
                            "duration_seconds": bundle.metadata.duration_seconds,
                            "thumbnail_url": bundle.metadata.thumbnail_url,
                        },
                    )
                )
                job.source_id = source.source_id
            self._update(job, status="succeeded", stage="succeeded")
            return job
        except Exception as exc:
            self._update(job, status="failed", stage="failed", error_code="ingestion_failed", error_message=str(exc))
            return job

    def _update(self, job: IngestionJob, **values: object) -> None:
        for name, value in values.items():
            setattr(job, name, value)
        self._session.add(job)
        self._session.commit()
        self._session.refresh(job)
