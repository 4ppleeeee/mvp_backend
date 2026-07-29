from app.ingestion.capabilities import Capability, ResourceKind, SourceProbe
from app.ingestion.planner import IngestionPlan


def test_audio_plan_does_not_request_video_keyframes() -> None:
    plan = IngestionPlan.from_probe(
        SourceProbe(
            source_platform="xiaoyuzhou",
            resource_kind=ResourceKind.AUDIO,
            capabilities=frozenset({Capability.METADATA, Capability.AUDIO, Capability.TRANSCRIPTION}),
        )
    )

    assert plan.fetch_caption_first is False
    assert plan.transcribe_audio_when_caption_missing is True
    assert plan.extract_keyframes is False


def test_video_plan_can_use_caption_and_audio_fallback() -> None:
    plan = IngestionPlan.from_probe(
        SourceProbe(
            source_platform="bilibili",
            resource_kind=ResourceKind.VIDEO,
            capabilities=frozenset({Capability.METADATA, Capability.CAPTION, Capability.AUDIO, Capability.TRANSCRIPTION}),
        )
    )

    assert plan.fetch_caption_first is True
    assert plan.transcribe_audio_when_caption_missing is True
