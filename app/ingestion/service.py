import asyncio
import inspect
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.ingestion.domain import EvidenceBundle, MediaExtractionError
from app.llm import SourceAnalysis, decide_ingestion, normalize_analysis
from app.models import IngestionJob, IngestionReview, SourceEvidence, TravelSource


class IngestionService:
    def __init__(self, *, session: Session, llm_client: object, pipeline: object, fallback_pipeline: object | None = None) -> None:
        self._session = session
        self._llm_client = llm_client
        self._pipeline = pipeline
        self._fallback_pipeline = fallback_pipeline

    def run(self, job_id: str) -> IngestionJob:
        job = self._session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
        try:
            self._update(
                job,
                status="running",
                stage="extracting",
                started_at=job.started_at or datetime.now(timezone.utc),
                finished_at=None,
                error_code=None,
                error_message=None,
                failure_stage=None,
                progress_percent=5,
                progress_message="开始解析",
                progress_updated_at=datetime.now(timezone.utc),
            )
            job.media_egress = getattr(self._pipeline, "media_egress", "router_default")
            bundle = self._extract_with_fallback(job)
            self._update(
                job,
                stage="analyzing",
                progress_percent=88,
                progress_message="提取完成，开始分析内容",
                progress_updated_at=datetime.now(timezone.utc),
                canonical_url=bundle.metadata.canonical_url,
                source_platform=bundle.metadata.source_platform,
                media_type=bundle.transcript.media_type.value,
                media_egress=job.media_egress,
                failure_stage=None,
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
            ingest_decision = decide_ingestion(analysis)
            self._update(
                job,
                stage="saving",
                progress_percent=97,
                progress_message="保存解析结果",
                progress_updated_at=datetime.now(timezone.utc),
                analysis_json=analysis.model_dump(mode="json"),
                evidence_text=bundle.transcript.full_text,
                ingest_decision=ingest_decision,
                evidence_origin=bundle.transcript.origin.value,
                evidence_language=bundle.transcript.language,
                evidence_segments=[
                    {
                        "start_seconds": segment.start_seconds,
                        "end_seconds": segment.end_seconds,
                        "text": segment.text,
                    }
                    for segment in bundle.transcript.segments
                ],
                evidence_metadata_json={
                    "title": bundle.metadata.title,
                    "author": bundle.metadata.author,
                    "published_at": bundle.metadata.published_at,
                    "duration_seconds": bundle.metadata.duration_seconds,
                    "thumbnail_url": bundle.metadata.thumbnail_url,
                },
            )
            if ingest_decision == "accept":
                self._save_source(job, analysis, bundle)
            self._update(
                job,
                status="succeeded",
                stage="succeeded",
                progress_percent=100,
                progress_message="解析完成",
                progress_updated_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
            return job
        except MediaExtractionError as exc:
            self._update(
                job,
                status="failed",
                stage="failed",
                error_code="media_extraction_failed",
                error_message=exc.safe_message,
                media_egress=exc.route,
                failure_stage=exc.phase,
                progress_message=f"解析失败：{exc.phase}",
                progress_updated_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
            return job
        except Exception as exc:
            self._update(
                job,
                status="failed",
                stage="failed",
                error_code="ingestion_failed",
                error_message=str(exc),
                progress_message="解析失败",
                progress_updated_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
            return job

    def approve_review(self, job_id: str, *, decision: str, reviewer: str | None = None, reason: str | None = None) -> IngestionJob:
        if decision not in {"accept", "reject"}:
            raise ValueError("review decision must be accept or reject")
        job = self._session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
        if job.status != "succeeded" or job.ingest_decision != "review":
            raise ValueError("ingestion is not awaiting review")
        analysis_data = {
            "is_travel_related": False,
            "confidence": 0.0,
            "reason": None,
            "title": None,
            "body_text": None,
            "destination": "未知",
            "category": "unknown",
            "location_name": None,
            "normalized_tags": [],
            "raw_tags": [],
            **job.analysis_json,
        }
        analysis = SourceAnalysis.model_validate(analysis_data)
        if decision == "accept":
            self._save_source_from_job(job, analysis)
        job.ingest_decision = decision
        job.reviewed_at = datetime.now(timezone.utc)
        job.reviewed_by = reviewer
        job.review_reason = reason
        self._session.add(job)
        self._session.add(
            IngestionReview(
                job_id=job.job_id,
                decision=decision,
                reviewer=reviewer,
                reason=reason,
                policy_version=job.policy_version,
            )
        )
        self._session.commit()
        self._session.refresh(job)
        return job

    def _save_source(self, job: IngestionJob, analysis: SourceAnalysis, bundle: EvidenceBundle) -> None:
        source = TravelSource(
            title=analysis.title or bundle.metadata.title,
            body_text=bundle.transcript.full_text,
            summary_text=analysis.body_text,
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
                    {"start_seconds": segment.start_seconds, "end_seconds": segment.end_seconds, "text": segment.text}
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

    def _save_source_from_job(self, job: IngestionJob, analysis: SourceAnalysis) -> None:
        evidence_metadata = job.evidence_metadata_json or {}
        source = TravelSource(
            title=analysis.title or job.original_url or "未命名资料",
            body_text=job.evidence_text or analysis.body_text or "",
            summary_text=analysis.body_text,
            original_url=job.original_url,
            source_platform=job.source_platform,
            cover_image_url=evidence_metadata.get("thumbnail_url") if isinstance(evidence_metadata.get("thumbnail_url"), str) else None,
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
                origin=job.evidence_origin or "article",
                language=job.evidence_language,
                full_text=job.evidence_text or "",
                segments=job.evidence_segments,
                metadata_json=evidence_metadata,
            )
        )
        job.source_id = source.source_id
    def _extract_with_fallback(self, job: IngestionJob) -> EvidenceBundle:
        def report(stage: str, percent: int, message: str) -> None:
            self._update(
                job,
                stage=stage,
                progress_percent=percent,
                progress_message=message,
                progress_updated_at=datetime.now(timezone.utc),
            )

        try:
            return self._extract_pipeline(self._pipeline, job, report)
        except MediaExtractionError as first_error:
            self._update(job, media_egress=first_error.route, failure_stage=first_error.phase)
            if not first_error.retryable or self._fallback_pipeline is None:
                raise
            fallback_route = getattr(self._fallback_pipeline, "media_egress", "configured_proxy")
            self._update(job, stage="retrying_media_egress", media_egress=fallback_route, failure_stage=None)
            try:
                return self._extract_pipeline(self._fallback_pipeline, job, report)
            except MediaExtractionError:
                raise

    @staticmethod
    def _extract_pipeline(pipeline: object, job: IngestionJob, report: object) -> EvidenceBundle:
        extract = pipeline.extract
        if "progress_callback" in inspect.signature(extract).parameters:
            return extract(job.original_url or "", job.job_id, progress_callback=report)
        return extract(job.original_url or "", job.job_id)

    def _update(self, job: IngestionJob, **values: object) -> None:
        for name, value in values.items():
            setattr(job, name, value)
        self._session.add(job)
        self._session.commit()
        self._session.refresh(job)
