from dataclasses import dataclass
from pathlib import Path

from app.ingestion.adapters.bilibili import BilibiliAdapter
from app.ingestion.adapters.youtube import YoutubeAdapter
from app.ingestion.domain import EvidenceOrigin, EvidenceBundle, MediaMetadata, TemporaryAudio, Transcript, TranscriptSegment
from app.ingestion.pipeline import VideoPipeline
from youtube_transcript_api._transcripts import FetchedTranscriptSnippet


@dataclass
class FakeYoutubeTrack:
    language_code: str
    language: str
    is_generated: bool
    snippets: list[dict[str, object]]

    def fetch(self) -> list[dict[str, object]]:
        return self.snippets


class FakeYoutubeTranscriptList:
    def __init__(self) -> None:
        self.manual = FakeYoutubeTrack("zh-Hans", "Chinese", False, [{"text": "人工字幕", "start": 0, "duration": 2}])
        self.auto = FakeYoutubeTrack("zh", "Chinese", True, [{"text": "自动字幕", "start": 0, "duration": 2}])

    def find_manually_created_transcript(self, _: list[str]) -> FakeYoutubeTrack:
        return self.manual

    def find_generated_transcript(self, _: list[str]) -> FakeYoutubeTrack:
        return self.auto

    def __iter__(self):
        return iter((self.manual, self.auto))


class FakeYoutubeClient:
    def list(self, _: str) -> FakeYoutubeTranscriptList:
        return FakeYoutubeTranscriptList()


def test_youtube_adapter_prefers_bilinote_manual_caption_order() -> None:
    transcript = YoutubeAdapter(caption_client=FakeYoutubeClient()).fetch_caption("https://youtu.be/abcdefghijk")

    assert transcript is not None
    assert transcript.origin is EvidenceOrigin.PLATFORM_CAPTION
    assert transcript.language == "zh-Hans"
    assert transcript.full_text == "人工字幕"


def test_youtube_adapter_accepts_current_transcript_snippet_objects() -> None:
    class ObjectTrack(FakeYoutubeTrack):
        def fetch(self):
            return [FetchedTranscriptSnippet(text="对象字幕", start=1.5, duration=2.0)]

    class ObjectTranscriptList(FakeYoutubeTranscriptList):
        def find_manually_created_transcript(self, _: list[str]) -> ObjectTrack:
            return ObjectTrack("zh-Hans", "Chinese", False, [])

    class ObjectClient:
        def list(self, _: str) -> ObjectTranscriptList:
            return ObjectTranscriptList()

    transcript = YoutubeAdapter(caption_client=ObjectClient()).fetch_caption("https://youtu.be/abcdefghijk")

    assert transcript is not None
    assert transcript.full_text == "对象字幕"
    assert transcript.segments[0].start_seconds == 1.5
    assert transcript.segments[0].end_seconds == 3.5


class FakeBilibiliClient:
    def fetch_tracks(self, _: str, __: int | None) -> list[dict[str, object]]:
        return [
            {"lan": "zh-CN", "ai_type": 1, "subtitle_url": "https://caption.example/ai"},
            {"lan": "zh-CN", "ai_type": 0, "subtitle_url": "https://caption.example/manual"},
        ]

    def fetch_body(self, url: str) -> list[dict[str, object]]:
        assert url == "https://caption.example/manual"
        return [{"from": 0, "to": 2, "content": "B站人工字幕"}]


def test_bilibili_adapter_prefers_bilinote_manual_chinese_track() -> None:
    transcript = BilibiliAdapter(caption_client=FakeBilibiliClient()).fetch_caption(
        "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
    )

    assert transcript is not None
    assert transcript.origin is EvidenceOrigin.PLATFORM_CAPTION
    assert transcript.language == "zh-CN"
    assert transcript.full_text == "B站人工字幕"


@dataclass
class FakeAdapter:
    caption: Transcript | None

    def fetch_metadata(self, url: str) -> MediaMetadata:
        return MediaMetadata(title="视频", source_platform="youtube", canonical_url=url)

    def fetch_caption(self, _: str) -> Transcript | None:
        return self.caption

    def acquire_audio(self, _: str, job_dir: Path) -> TemporaryAudio:
        path = job_dir / "audio.m4a"
        path.write_bytes(b"audio")
        return TemporaryAudio(path=str(path))

    def acquire_video(self, _: str, job_dir: Path) -> Path:
        path = job_dir / "video.mp4"
        path.write_bytes(b"video")
        return path


class FakeTranscriber:
    def transcribe(self, _: str) -> Transcript:
        return Transcript(
            language="zh",
            origin=EvidenceOrigin.ASR,
            full_text="ASR 文本",
            segments=(TranscriptSegment(start_seconds=0, end_seconds=1, text="ASR 文本"),),
        )


def test_video_pipeline_skips_audio_when_bilinote_caption_exists(tmp_path: Path) -> None:
    caption = Transcript(
        language="zh",
        origin=EvidenceOrigin.PLATFORM_CAPTION,
        full_text="平台字幕",
        segments=(TranscriptSegment(start_seconds=0, end_seconds=1, text="平台字幕"),),
    )

    result = VideoPipeline(adapter=FakeAdapter(caption), transcriber=FakeTranscriber(), temp_root=tmp_path).extract(
        "https://youtu.be/abcdefghijk", "ing_caption"
    )

    assert result.transcript is caption
    assert not (tmp_path / "ing_caption").exists()


def test_video_pipeline_transcribes_temporary_audio_without_caption(tmp_path: Path) -> None:
    result = VideoPipeline(adapter=FakeAdapter(None), transcriber=FakeTranscriber(), temp_root=tmp_path).extract(
        "https://youtu.be/abcdefghijk", "ing_asr"
    )

    assert result.transcript.origin is EvidenceOrigin.ASR
    assert result.transcript.full_text == "ASR 文本"
    assert not (tmp_path / "ing_asr").exists()


def test_video_pipeline_keeps_public_caption_when_metadata_is_restricted(tmp_path: Path) -> None:
    caption = Transcript(
        language="zh",
        origin=EvidenceOrigin.PLATFORM_CAPTION,
        full_text="公开视频字幕",
        segments=(TranscriptSegment(start_seconds=0, end_seconds=1, text="公开视频字幕"),),
    )

    class CaptionOnlyAdapter(FakeAdapter):
        def fetch_metadata(self, _: str) -> MediaMetadata:
            raise RuntimeError("metadata access restricted")

    result = VideoPipeline(adapter=CaptionOnlyAdapter(caption), transcriber=FakeTranscriber(), temp_root=tmp_path).extract(
        "https://youtu.be/abcdefghijk", "ing_caption_only"
    )

    assert result.transcript.full_text == "公开视频字幕"
    assert result.metadata.title == "https://youtu.be/abcdefghijk"


def test_video_pipeline_returns_keyframe_images_when_enabled(monkeypatch, tmp_path: Path) -> None:
    caption = Transcript(
        language="zh",
        origin=EvidenceOrigin.PLATFORM_CAPTION,
        full_text="平台字幕",
        segments=(TranscriptSegment(start_seconds=0, end_seconds=1, text="平台字幕"),),
    )
    expected = ("data:image/jpeg;base64,ZmFrZQ==",)
    monkeypatch.setattr("app.ingestion.pipeline.extract_keyframe_images", lambda *_args, **_kwargs: expected)

    result = VideoPipeline(
        adapter=FakeAdapter(caption),
        transcriber=FakeTranscriber(),
        temp_root=tmp_path,
        keyframe_enabled=True,
    ).extract("https://youtu.be/abcdefghijk", "ing_keyframes")

    assert result.keyframe_images == expected
    assert not (tmp_path / "ing_keyframes").exists()


def test_video_pipeline_does_not_download_video_when_keyframes_disabled(monkeypatch, tmp_path: Path) -> None:
    caption = Transcript(
        language="zh",
        origin=EvidenceOrigin.PLATFORM_CAPTION,
        full_text="平台字幕",
        segments=(TranscriptSegment(start_seconds=0, end_seconds=1, text="平台字幕"),),
    )
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("video download must stay disabled")

    monkeypatch.setattr(FakeAdapter, "acquire_video", fail_if_called)

    result = VideoPipeline(adapter=FakeAdapter(caption), transcriber=FakeTranscriber(), temp_root=tmp_path).extract(
        "https://youtu.be/abcdefghijk", "ing_no_keyframes"
    )

    assert result.keyframe_images == ()
    assert called is False
