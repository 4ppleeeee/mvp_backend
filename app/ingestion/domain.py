from dataclasses import dataclass
from enum import StrEnum


class MediaType(StrEnum):
    ARTICLE = "article"
    IMAGE = "image"
    VIDEO = "video"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


class EvidenceOrigin(StrEnum):
    PLATFORM_CAPTION = "platform_caption"
    AUTO_CAPTION = "auto_caption"
    ASR = "asr"
    OCR = "ocr"
    ARTICLE = "article"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobStage(StrEnum):
    QUEUED = "queued"
    CLASSIFYING = "classifying"
    EXTRACTING = "extracting"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    SAVING = "saving"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True)
class Transcript:
    language: str | None
    origin: EvidenceOrigin
    full_text: str
    segments: tuple[TranscriptSegment, ...]
    media_type: MediaType = MediaType.VIDEO

    def __post_init__(self) -> None:
        if not self.full_text.strip():
            raise ValueError("transcript text must not be empty")
        if any(segment.end_seconds < segment.start_seconds for segment in self.segments):
            raise ValueError("transcript segment end must not precede start")


@dataclass(frozen=True)
class MediaMetadata:
    title: str
    source_platform: str
    canonical_url: str
    duration_seconds: float | None = None
    author: str | None = None
    published_at: str | None = None
    thumbnail_url: str | None = None


@dataclass(frozen=True)
class TemporaryAudio:
    path: str
    duration_seconds: float | None = None


@dataclass(frozen=True)
class EvidenceBundle:
    metadata: MediaMetadata
    transcript: Transcript
    keyframe_images: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceDescriptor:
    original_url: str
    canonical_url: str
    media_type: MediaType
    source_platform: str | None


class MediaExtractionError(RuntimeError):
    """A safe, routable failure from caption, metadata, or audio extraction."""

    def __init__(self, phase: str, route: str, message: str, retryable: bool = False) -> None:
        self.phase = phase if phase in {"caption", "metadata", "audio", "video", "keyframe"} else "metadata"
        self.route = route if route in {"router_default", "configured_proxy"} else "router_default"
        self.retryable = retryable
        self.safe_message = _sanitize_media_error(message)
        super().__init__(self.safe_message)


def _sanitize_media_error(message: str) -> str:
    text = " ".join(str(message).split())
    for token in ("http://", "https://"):
        while token in text:
            start = text.rfind(" ", 0, text.index(token)) + 1
            end = text.find(" ", text.index(token))
            if end < 0:
                end = len(text)
            text = text[:start] + "[redacted-url]" + text[end:]
    return text[:500]
