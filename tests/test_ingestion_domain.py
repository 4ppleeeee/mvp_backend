from pathlib import Path

import pytest

from app.ingestion.classifier import ResourceClassifier
from app.ingestion.domain import EvidenceOrigin, MediaType, Transcript, TranscriptSegment
from app.ingestion.media import JobDirectory
from app.ingestion.transcriber import normalize_bilinote_whisper_segments
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


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://youtu.be/abc123", "youtube"),
        ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
        ("https://v.douyin.com/abc/", "douyin"),
        ("https://v.kuaishou.com/abc", "kuaishou"),
    ],
)
def test_classifier_identifies_supported_video_platform(url: str, platform: str) -> None:
    descriptor = ResourceClassifier.default().classify_url(url)

    assert descriptor.media_type is MediaType.VIDEO
    assert descriptor.source_platform == platform


def test_classifier_treats_unknown_http_url_as_article() -> None:
    descriptor = ResourceClassifier.default().classify_url("https://example.com/travel")

    assert descriptor.media_type is MediaType.ARTICLE
    assert descriptor.source_platform is None


def test_job_directory_removes_temporary_audio_when_it_exits(tmp_path: Path) -> None:
    with JobDirectory(tmp_path, "ing_test") as job_dir:
        (job_dir / "audio.m4a").write_bytes(b"temporary")

    assert not (tmp_path / "ing_test").exists()


def test_bilinote_whisper_normalization_discards_blank_segments() -> None:
    transcript = normalize_bilinote_whisper_segments(
        language="zh",
        segments=[(0.0, 1.0, " 第一段 "), (1.0, 2.0, " ")],
    )

    assert transcript.full_text == "第一段"
    assert transcript.origin is EvidenceOrigin.ASR
    assert transcript.segments == (TranscriptSegment(start_seconds=0.0, end_seconds=1.0, text="第一段"),)
