import re
from typing import Protocol

from app.ingestion.adapters.base import BaseVideoAdapter
from app.ingestion.domain import EvidenceOrigin, MediaExtractionError, Transcript, TranscriptSegment
from app.ingestion.media import MediaEgressPolicy


class YoutubeCaptionClient(Protocol):
    def list(self, video_id: str) -> object: ...


class YoutubeAdapter(BaseVideoAdapter):
    platform = "youtube"
    hosts = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})

    def __init__(self, *, caption_client: YoutubeCaptionClient | None = None, media_egress_policy: MediaEgressPolicy | None = None) -> None:
        super().__init__(media_egress_policy=media_egress_policy)
        self._caption_client = caption_client

    def fetch_caption(self, video_url: str) -> Transcript | None:
        video_id = self._extract_video_id(video_url)
        if not video_id:
            return None
        try:
            transcript_list = self._get_caption_client().list(video_id)
            languages = ["zh-Hans", "zh", "zh-CN", "zh-TW", "en", "en-US", "ja"]
            transcript = None
            try:
                transcript = transcript_list.find_manually_created_transcript(languages)
            except Exception:
                try:
                    transcript = transcript_list.find_generated_transcript(languages)
                except Exception:
                    for candidate in transcript_list:
                        transcript = candidate
                        break
            if transcript is None:
                return None

            segments = tuple(
                TranscriptSegment(
                    start_seconds=float(_snippet_value(snippet, "start", 0)),
                    end_seconds=float(_snippet_value(snippet, "start", 0))
                    + float(_snippet_value(snippet, "duration", 0)),
                    text=str(_snippet_value(snippet, "text", "")).strip(),
                )
                for snippet in transcript.fetch()
                if str(_snippet_value(snippet, "text", "")).strip()
            )
            if not segments:
                return None
            return Transcript(
                language=transcript.language_code,
                origin=EvidenceOrigin.AUTO_CAPTION if transcript.is_generated else EvidenceOrigin.PLATFORM_CAPTION,
                full_text=" ".join(segment.text for segment in segments),
                segments=segments,
            )
        except Exception as exc:
            if exc.__class__.__name__ in {"NoTranscriptFound", "TranscriptsDisabled", "VideoUnavailable"}:
                return None
            raise MediaExtractionError("caption", self._media_egress_policy.route, str(exc), retryable=True) from exc

    def _get_caption_client(self) -> YoutubeCaptionClient:
        if self._caption_client is None:
            from youtube_transcript_api import YouTubeTranscriptApi

            self._caption_client = YouTubeTranscriptApi(
                http_client=self._media_egress_policy.transcript_session()
            )
        return self._caption_client

    @staticmethod
    def _extract_video_id(url: str) -> str | None:
        match = re.search(r"(?:v=|youtu\.be/|shorts/)([0-9A-Za-z_-]{11})", url)
        return match.group(1) if match else None


def _snippet_value(snippet: object, key: str, default: object) -> object:
    if isinstance(snippet, dict):
        return snippet.get(key, default)
    return getattr(snippet, key, default)
