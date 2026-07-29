import asyncio
import base64
import shutil
from pathlib import Path

from sqlmodel import Session, select

from app.llm import decide_ingestion, normalize_analysis
from app.models import IngestionJob, SourceEvidence, TravelSource


class ImageIngestionService:
    def __init__(self, *, session: Session, llm_client: object) -> None:
        self._session = session
        self._llm_client = llm_client

    def run(self, job_id: str) -> IngestionJob:
        job = self._session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
        try:
            job.status, job.stage = "running", "analyzing"
            self._save(job)
            image_path = Path(job.input_path or "")
            image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
            analysis = normalize_analysis(asyncio.run(self._llm_client.analyze_image(image_base64=image_base64, title_hint=image_path.name)))
            job.stage = "saving"
            job.analysis_json = analysis.model_dump(mode="json")
            job.ingest_decision = decide_ingestion(analysis)
            job.evidence_text = analysis.body_text or analysis.title or image_path.name
            job.evidence_origin = "ocr"
            job.evidence_metadata_json = {"title": analysis.title or image_path.name}
            self._save(job)
            if job.ingest_decision == "accept":
                source = TravelSource(
                    title=analysis.title or image_path.name,
                    body_text=analysis.body_text or analysis.title or image_path.name,
                    summary_text=analysis.body_text,
                    original_url=None,
                    source_platform="image",
                    cover_image_url=None,
                    destination=analysis.destination,
                    category=analysis.category,
                    location_name=analysis.location_name,
                    normalized_tags=analysis.normalized_tags,
                    raw_tags=analysis.raw_tags,
                )
                self._session.add(source)
                self._session.flush()
                self._session.add(SourceEvidence(source_id=source.source_id, origin="ocr", language=None, full_text=source.body_text))
                job.source_id = source.source_id
            job.status, job.stage = "succeeded", "succeeded"
            self._save(job)
        except Exception as exc:
            job.status, job.stage, job.error_code, job.error_message = "failed", "failed", "image_ingestion_failed", str(exc)
            self._save(job)
        finally:
            if job.input_path:
                shutil.rmtree(Path(job.input_path).parent, ignore_errors=True)
        return job

    def _save(self, job: IngestionJob) -> None:
        self._session.add(job)
        self._session.commit()
        self._session.refresh(job)
