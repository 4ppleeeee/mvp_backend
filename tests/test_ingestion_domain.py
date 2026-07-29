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


def test_classifier_identifies_xiaohongshu_as_article_source() -> None:
    descriptor = ResourceClassifier.default().classify_url("https://www.xiaohongshu.com/explore/66abc")

    assert descriptor.media_type is MediaType.ARTICLE
    assert descriptor.source_platform == "xiaohongshu"


def test_xiaohongshu_video_adapter_matches_public_note_hosts() -> None:
    from app.ingestion.adapters.xiaohongshu import XiaohongshuAdapter

    adapter = XiaohongshuAdapter()

    assert adapter.matches("https://www.xiaohongshu.com/discovery/item/6a6247f0000000000f033570")


def test_xiaohongshu_video_adapter_falls_back_when_public_caption_is_unavailable() -> None:
    from app.ingestion.adapters.xiaohongshu import XiaohongshuAdapter

    assert XiaohongshuAdapter().fetch_caption("https://www.xiaohongshu.com/discovery/item/6a6247f0000000000f033570") is None


@pytest.mark.parametrize(
    ("adapter_path", "url"),
    [
        ("app.ingestion.adapters.douyin.DouyinAdapter", "https://v.douyin.com/9DLOqEFBaMM/"),
        ("app.ingestion.adapters.kuaishou.KuaishouAdapter", "https://v.kuaishou.com/example"),
    ],
)
def test_captionless_video_adapters_fall_back_to_whisper(adapter_path: str, url: str) -> None:
    module_path, class_name = adapter_path.rsplit(".", 1)
    module = __import__(module_path, fromlist=[class_name])
    adapter = getattr(module, class_name)()

    assert adapter.fetch_caption(url) is None


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
