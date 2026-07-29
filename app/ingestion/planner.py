from dataclasses import dataclass

from app.ingestion.capabilities import Capability, SourceProbe


@dataclass(frozen=True)
class IngestionPlan:
    probe: SourceProbe

    @classmethod
    def from_probe(cls, probe: SourceProbe) -> "IngestionPlan":
        return cls(probe=probe)

    @property
    def fetch_caption_first(self) -> bool:
        return self.probe.supports(Capability.CAPTION)

    @property
    def transcribe_audio_when_caption_missing(self) -> bool:
        return self.probe.supports(Capability.AUDIO) and self.probe.supports(Capability.TRANSCRIPTION)

    @property
    def extract_keyframes(self) -> bool:
        return self.probe.supports(Capability.VIDEO) and self.probe.supports(Capability.KEYFRAMES)
