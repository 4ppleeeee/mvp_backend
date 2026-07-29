from app.ingestion.capabilities import Capability, ResourceKind, SourceProbe


def test_source_probe_describes_audio_capabilities() -> None:
    probe = SourceProbe(
        source_platform="xiaoyuzhou",
        resource_kind=ResourceKind.AUDIO,
        capabilities=frozenset({Capability.METADATA, Capability.AUDIO, Capability.TRANSCRIPTION}),
    )

    assert probe.resource_kind is ResourceKind.AUDIO
    assert probe.supports(Capability.AUDIO)
    assert not probe.supports(Capability.VIDEO)


def test_source_probe_returns_capabilities_in_stable_order() -> None:
    probe = SourceProbe(
        source_platform="bilibili",
        resource_kind=ResourceKind.VIDEO,
        capabilities=frozenset({Capability.TRANSCRIPTION, Capability.CAPTION, Capability.METADATA}),
    )

    assert probe.ordered_capabilities() == (
        Capability.METADATA,
        Capability.CAPTION,
        Capability.TRANSCRIPTION,
    )
