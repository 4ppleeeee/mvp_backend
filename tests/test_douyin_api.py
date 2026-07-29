from pathlib import Path

import pytest

from app.ingestion.adapters.douyin import DouyinAdapter
from app.ingestion.domain import MediaMetadata
from app.ingestion.media import MediaEgressPolicy


class FakeResponse:
    def __init__(
        self,
        *,
        url: str = "",
        json_data: dict | None = None,
        content: bytes = b"",
        status_error: Exception | None = None,
    ) -> None:
        self.url = url
        self._json_data = json_data or {}
        self.content = content
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error
        return None

    def json(self) -> dict:
        return self._json_data

    def iter_content(self, chunk_size: int = 0):
        yield self.content


class FakeSession:
    def __init__(self, detail: dict, *, head_status_error: Exception | None = None) -> None:
        self.detail = detail
        self.head_status_error = head_status_error
        self.proxies: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def head(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("HEAD", url, kwargs))
        return FakeResponse(
            url="https://www.douyin.com/video/7351234567890123456",
            status_error=self.head_status_error,
        )

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        if "aweme/detail" in url:
            return FakeResponse(json_data=self.detail)
        return FakeResponse(content=b"video-bytes")


class FakeTokenClient:
    def __init__(self, values: list[str]) -> None:
        self.values = values
        self.calls = 0

    def fetch(self) -> str:
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class StaticSigner:
    def sign(self, _: dict[str, object]) -> str:
        return "signed-value"


def _detail() -> dict:
    return {
        "aweme_detail": {
            "aweme_id": "7351234567890123456",
            "item_title": "北京五分钟攻略",
            "create_time": 1719187200,
            "author": {"nickname": "途家"},
            "video": {
                "duration": 300000,
                "cover_original_scale": {"url_list": ["https://cover.example/cover.jpg"]},
                "download_addr": {"url_list": ["https://media.example/download.mp4"]},
                "play_addr": {"url_list": ["https://media.example/play.mp4"]},
            },
            "music": {"play_url": {"uri": "https://music.example/background.mp3"}},
        }
    }


def test_douyin_client_resolves_short_link_and_uses_actual_video_not_background_music() -> None:
    from app.ingestion.douyin_api import DouyinApiClient

    session = FakeSession(_detail())
    client = DouyinApiClient(
        session=session,
        token_client=FakeTokenClient(["x" * 120]),
        signer=StaticSigner(),
    )

    media = client.fetch_media("https://v.douyin.com/9DLOqEFBaMM/")

    assert media.aweme_id == "7351234567890123456"
    assert media.video_url == "https://media.example/download.mp4"
    assert media.video_url != _detail()["aweme_detail"]["music"]["play_url"]["uri"]
    assert session.calls[0][0] == "HEAD"
    assert "a_bogus=signed-value" in session.calls[1][1]


def test_douyin_client_uses_the_final_short_link_url_even_when_head_returns_404() -> None:
    from app.ingestion.douyin_api import DouyinApiClient

    client = DouyinApiClient(
        session=FakeSession(_detail(), head_status_error=RuntimeError("404")),
        token_client=FakeTokenClient(["x" * 120]),
        signer=StaticSigner(),
    )

    media = client.fetch_media("https://v.douyin.com/9DLOqEFBaMM/")

    assert media.aweme_id == "7351234567890123456"


def test_douyin_client_retries_transient_ms_token_failures_without_storing_token() -> None:
    from app.ingestion.douyin_api import DouyinApiClient

    token_client = FakeTokenClient([RuntimeError("503"), "x" * 120])
    client = DouyinApiClient(
        session=FakeSession(_detail()),
        token_client=token_client,
        signer=StaticSigner(),
        token_attempts=2,
    )

    client.fetch_media("https://www.douyin.com/video/7351234567890123456")

    assert token_client.calls == 2
    assert not hasattr(client, "ms_token")


def test_douyin_adapter_returns_metadata_and_extracts_audio_from_temporary_video(monkeypatch, tmp_path: Path) -> None:
    from app.ingestion.douyin_api import DouyinMedia

    media = DouyinMedia(
        aweme_id="7351234567890123456",
        title="北京五分钟攻略",
        video_url="https://media.example/download.mp4",
        duration_seconds=300.0,
        author="途家",
        published_at="2024-06-24",
        thumbnail_url="https://cover.example/cover.jpg",
    )

    class FakeClient:
        def __init__(self, **_: object) -> None:
            pass

        def fetch_media(self, _: str) -> DouyinMedia:
            return media

        def download_video(self, _: DouyinMedia, destination: Path) -> Path:
            destination.write_bytes(b"video")
            return destination

    class FakeExtractor:
        def extract(self, input_path: Path, output_dir: Path):
            assert input_path.name == "7351234567890123456.mp4"
            output = output_dir / "audio.mp3"
            output.write_bytes(b"audio")
            from app.ingestion.domain import TemporaryAudio

            return TemporaryAudio(path=str(output), duration_seconds=300.0)

    monkeypatch.setattr("app.ingestion.adapters.douyin.DouyinApiClient", FakeClient)
    monkeypatch.setattr("app.ingestion.adapters.douyin.BiliNoteFfmpegAudioExtractor", FakeExtractor)
    adapter = DouyinAdapter(media_egress_policy=MediaEgressPolicy("http://proxy.example:8080"))

    metadata = adapter.fetch_metadata("https://v.douyin.com/9DLOqEFBaMM/")
    audio = adapter.acquire_audio("https://v.douyin.com/9DLOqEFBaMM/", tmp_path)

    assert metadata == MediaMetadata(
        title="北京五分钟攻略",
        source_platform="douyin",
        canonical_url="https://v.douyin.com/9DLOqEFBaMM/",
        duration_seconds=300.0,
        author="途家",
        published_at="2024-06-24",
        thumbnail_url="https://cover.example/cover.jpg",
    )
    assert Path(audio.path).read_bytes() == b"audio"
