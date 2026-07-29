from dataclasses import dataclass
from enum import StrEnum


class ResourceKind(StrEnum):
    ARTICLE = "article"
    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


class Capability(StrEnum):
    METADATA = "metadata"
    ARTICLE_BODY = "article_body"
    CAPTION = "caption"
    AUDIO = "audio"
    VIDEO = "video"
    TRANSCRIPTION = "transcription"
    KEYFRAMES = "keyframes"
    OCR = "ocr"


_CAPABILITY_ORDER = (
    Capability.METADATA,
    Capability.ARTICLE_BODY,
    Capability.CAPTION,
    Capability.AUDIO,
    Capability.VIDEO,
    Capability.TRANSCRIPTION,
    Capability.KEYFRAMES,
    Capability.OCR,
)


@dataclass(frozen=True)
class SourceProbe:
    source_platform: str
    resource_kind: ResourceKind
    capabilities: frozenset[Capability]
    canonical_url: str | None = None

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def ordered_capabilities(self) -> tuple[Capability, ...]:
        return tuple(capability for capability in _CAPABILITY_ORDER if capability in self.capabilities)
