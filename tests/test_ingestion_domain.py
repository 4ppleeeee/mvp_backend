from app.ingestion.domain import EvidenceOrigin, MediaType, Transcript, TranscriptSegment
from app.schemas import CreateIngestionRequest


def test_create_ingestion_request_accepts_url_only() -> None:
    request = CreateIngestionRequest(url="https://youtu.be/abc123")

    assert request.input_type == "url"
    assert request.url == "https://youtu.be/abc123"


def test_transcript_retains_timestamped_video_evidence() -> None:
    transcript = Transcript(
        language="zh",
        origin=EvidenceOrigin.PLATFORM_CAPTION,
        full_text="第一句 第二句",
        segments=(
            TranscriptSegment(start_seconds=0, end_seconds=2, text="第一句"),
            TranscriptSegment(start_seconds=2, end_seconds=4, text="第二句"),
        ),
    )

    assert transcript.media_type is MediaType.VIDEO
    assert transcript.segments[0].text == "第一句"
