from app.ingestion.capabilities import Capability, ResourceKind
from app.ingestion.sources import SourceRegistry


def test_registry_resolves_bilibili_source_without_treating_kind_as_adapter() -> None:
    source = SourceRegistry.default().resolve("https://www.bilibili.com/video/BV1xx411c7mD")

    assert source is not None
    assert source.platform == "bilibili"
    probe = source.probe("https://www.bilibili.com/video/BV1xx411c7mD")
    assert probe.resource_kind is ResourceKind.VIDEO
    assert probe.supports(Capability.AUDIO)


def test_registry_resolves_xiaoyuzhou_as_audio_source() -> None:
    source = SourceRegistry.default().resolve("https://www.xiaoyuzhoufm.com/episode/abc")

    assert source is not None
    assert source.platform == "xiaoyuzhou"
    assert source.probe("https://www.xiaoyuzhoufm.com/episode/abc").resource_kind is ResourceKind.AUDIO
