from pathlib import Path

from sqlmodel import Session, select

from app.config import Settings
from app.db import create_db_engine, init_db
from app.ingestion.domain import EvidenceBundle, EvidenceOrigin, MediaMetadata, Transcript, TranscriptSegment
from app.ingestion.service import IngestionService
from app.llm import SourceAnalysis
from app.models import IngestionJob, SourceEvidence, TravelSource


class TravelLlm:
    async def analyze_source(self, **_: object) -> SourceAnalysis:
        return SourceAnalysis(
            is_travel_related=True,
            confidence=0.9,
            body_text="表参道咖啡路线的卡片摘要。",
            destination="东京",
            category="eat",
            location_name="表参道",
            normalized_tags=["咖啡"],
            raw_tags=["路线"],
        )


class FakePipeline:
    def extract(self, url: str, job_id: str) -> EvidenceBundle:
        assert url == "https://youtu.be/abcdefghijk"
        assert job_id.startswith("ing_")
        return EvidenceBundle(
            metadata=MediaMetadata(title="东京咖啡路线", source_platform="youtube", canonical_url=url),
            transcript=Transcript(
                language="zh",
                origin=EvidenceOrigin.PLATFORM_CAPTION,
                full_text="表参道咖啡路线",
                segments=(TranscriptSegment(start_seconds=0, end_seconds=2, text="表参道咖啡路线"),),
            ),
        )


def test_ingestion_service_saves_source_and_timestamped_evidence(tmp_path: Path) -> None:
    engine = create_db_engine(Settings(database_url=f"sqlite:///{tmp_path / 'service.db'}"))
    init_db(engine)
    with Session(engine) as session:
        job = IngestionJob(input_type="url", original_url="https://youtu.be/abcdefghijk", source_platform="youtube", media_type="video")
        session.add(job)
        session.commit()
        session.refresh(job)

        result = IngestionService(session=session, llm_client=TravelLlm(), pipeline=FakePipeline()).run(job.job_id)

        source = session.exec(select(TravelSource).where(TravelSource.source_id == result.source_id)).one()
        evidence = session.exec(select(SourceEvidence).where(SourceEvidence.source_id == source.source_id)).one()
        assert result.status == "succeeded"
        assert source.title == "东京咖啡路线"
        assert source.body_text == "表参道咖啡路线"
        assert source.summary_text == "表参道咖啡路线的卡片摘要。"
        assert evidence.full_text == "表参道咖啡路线"
        assert evidence.segments == [{"start_seconds": 0, "end_seconds": 2, "text": "表参道咖啡路线"}]
        assert result.evidence_text == "表参道咖啡路线"
        assert result.analysis_json["destination"] == "东京"
