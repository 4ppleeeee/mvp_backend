from dataclasses import dataclass

from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.ingestion.domain import EvidenceBundle, EvidenceOrigin, MediaExtractionError, MediaMetadata, Transcript, TranscriptSegment
from app.ingestion.media import BiliNoteYtDlpAcquirer, MediaEgressPolicy
from app.ingestion.service import IngestionService
from app.llm import SourceAnalysis
from app.models import IngestionJob


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        metadata=MediaMetadata(title="视频", source_platform="youtube", canonical_url="https://youtu.be/x"),
        transcript=Transcript(
            language="zh",
            origin=EvidenceOrigin.PLATFORM_CAPTION,
            full_text="旅行路线",
            segments=(TranscriptSegment(start_seconds=0, end_seconds=1, text="旅行路线"),),
        ),
    )


def test_media_policy_only_adds_proxy_for_configured_route() -> None:
    default = MediaEgressPolicy()
    configured = MediaEgressPolicy("http://user:secret@example.test:8080")

    assert default.route == "router_default"
    assert "proxy" not in default.yt_dlp_options()
    assert configured.route == "configured_proxy"
    assert configured.yt_dlp_options()["proxy"] == "http://user:secret@example.test:8080"
    assert configured.transcript_session().proxies == {
        "http": "http://user:secret@example.test:8080",
        "https": "http://user:secret@example.test:8080",
    }


def test_ytdlp_options_enable_node_ejs_and_explicit_retries(monkeypatch) -> None:
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, *_args, **_kwargs):
            return {"id": "x", "ext": "m4a", "title": "x"}

    class FakeYtDlp:
        YoutubeDL = FakeYoutubeDL

    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", FakeYtDlp())
    BiliNoteYtDlpAcquirer(policy=MediaEgressPolicy()).download(
        "https://youtu.be/x", __import__("pathlib").Path("/tmp"), platform="youtube", skip_download=True
    )

    assert captured["js_runtimes"] == {"node": {}}
    assert captured["remote_components"] == ["ejs:github"]
    assert captured["retries"] == 2
    assert captured["fragment_retries"] == 2
    assert captured["extractor_retries"] == 2


def test_ytdlp_video_download_uses_task_local_video_output(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, *_args, **_kwargs):
            output = tmp_path / "abcdefghijk.mp4"
            output.write_bytes(b"video")
            return {"id": "abcdefghijk", "ext": "mp4", "title": "video"}

    class FakeYtDlp:
        YoutubeDL = FakeYoutubeDL

    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", FakeYtDlp())
    from app.ingestion.media import BiliNoteYtDlpAcquirer

    result = BiliNoteYtDlpAcquirer(MediaEgressPolicy()).download_video(
        "https://youtu.be/abcdefghijk", tmp_path, platform="youtube"
    )

    assert result == tmp_path / "abcdefghijk.mp4"
    assert captured["format"] == "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    assert captured["merge_output_format"] == "mp4"
    assert captured["outtmpl"] == str(tmp_path / "%(id)s.%(ext)s")


@dataclass
class FakePipeline:
    result: EvidenceBundle | None = None
    error: Exception | None = None
    calls: int = 0

    def extract(self, _url: str, _job_id: str) -> EvidenceBundle:
        self.calls += 1
        if self.error:
            raise self.error
        assert self.result is not None
        return self.result


class FakeLlm:
    async def analyze_source(self, **_kwargs):
        return SourceAnalysis(is_travel_related=False, reason="test")


def _service(tmp_path, pipeline, fallback=None):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        job = IngestionJob(input_type="url", original_url="https://youtu.be/x", source_platform="youtube", media_type="video")
        session.add(job)
        session.commit()
        session.refresh(job)
        result = IngestionService(session=session, llm_client=FakeLlm(), pipeline=pipeline, fallback_pipeline=fallback).run(job.job_id)
        return result


def test_retryable_default_media_failure_uses_configured_pipeline_once(tmp_path) -> None:
    primary = FakePipeline(error=MediaExtractionError("caption", "router_default", "RequestBlocked", True))
    fallback = FakePipeline(result=_bundle())
    job = _service(tmp_path, primary, fallback)

    assert primary.calls == 1
    assert fallback.calls == 1
    assert job.status == "succeeded"
    assert job.media_egress == "configured_proxy"


def test_media_failure_without_fallback_is_terminal_and_safe(tmp_path) -> None:
    primary = FakePipeline(error=MediaExtractionError("caption", "router_default", "RequestBlocked", True))
    job = _service(tmp_path, primary)

    assert job.status == "failed"
    assert job.media_egress == "router_default"
    assert job.failure_stage == "caption"
    assert "secret" not in (job.error_message or "")


def test_configured_media_failure_does_not_loop_to_primary_again(tmp_path) -> None:
    primary = FakePipeline(error=MediaExtractionError("caption", "router_default", "RequestBlocked", True))
    fallback = FakePipeline(error=MediaExtractionError("audio", "configured_proxy", "TLS EOF", True))
    job = _service(tmp_path, primary, fallback)

    assert primary.calls == 1
    assert fallback.calls == 1
    assert job.status == "failed"
    assert job.media_egress == "configured_proxy"
    assert job.failure_stage == "audio"
