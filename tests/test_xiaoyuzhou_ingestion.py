from pathlib import Path

from app.ingestion.adapters.xiaoyuzhou import XiaoyuzhouAdapter
from app.ingestion.capabilities import Capability, ResourceKind
from app.ingestion.domain import MediaExtractionError
from app.ingestion.article import FetchedHtml


class FakeFetcher:
    def __init__(self, html: str) -> None:
        self.html = html

    def fetch(self, url: str) -> FetchedHtml:
        return FetchedHtml(url=url, html=self.html)


def test_xiaoyuzhou_probe_is_audio_with_transcription_capability() -> None:
    probe = XiaoyuzhouAdapter().probe("https://www.xiaoyuzhoufm.com/episode/abc")

    assert probe.resource_kind is ResourceKind.AUDIO
    assert probe.capabilities == frozenset({Capability.METADATA, Capability.AUDIO, Capability.TRANSCRIPTION})


def test_xiaoyuzhou_extracts_public_metadata_from_episode_fixture() -> None:
    html = Path("tests/fixtures/xiaoyuzhou_episode.html").read_text()
    adapter = XiaoyuzhouAdapter(fetcher=FakeFetcher(html))

    metadata = adapter.fetch_metadata("https://www.xiaoyuzhoufm.com/episode/abc")

    assert metadata.title == "测试播客单集"
    assert metadata.author == "测试主播"
    assert metadata.duration_seconds == 1234.0


def test_xiaoyuzhou_rejects_audio_urls_outside_public_media_host() -> None:
    adapter = XiaoyuzhouAdapter(fetcher=FakeFetcher('"https://example.com/audio.m4a"'))

    try:
        adapter.fetch_metadata("https://www.xiaoyuzhoufm.com/episode/abc")
    except MediaExtractionError as exc:
        assert exc.phase == "audio"
        assert "not allowed" in exc.safe_message
    else:
        raise AssertionError("expected an allowlist failure")
