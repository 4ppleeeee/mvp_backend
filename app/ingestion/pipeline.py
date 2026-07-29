"""Subtitle-first video extraction following BiliNote's NoteGenerator order."""

from pathlib import Path
from typing import Protocol

from app.ingestion.domain import EvidenceBundle, MediaExtractionError, MediaMetadata, TemporaryAudio, Transcript
from app.ingestion.keyframes import extract_keyframe_images
from app.ingestion.media import JobDirectory


class PipelineAdapter(Protocol):
    def fetch_metadata(self, url: str) -> MediaMetadata: ...

    def fetch_caption(self, url: str) -> Transcript | None: ...

    def acquire_audio(self, url: str, job_dir: Path) -> TemporaryAudio: ...

    def acquire_video(self, url: str, job_dir: Path) -> Path: ...


class PipelineTranscriber(Protocol):
    def transcribe(self, file_path: str) -> Transcript: ...


class VideoPipeline:
    def __init__(
        self,
        *,
        adapter: PipelineAdapter,
        transcriber: PipelineTranscriber,
        temp_root: Path,
        keyframe_enabled: bool = False,
        frame_interval_seconds: int = 6,
        grid_size: tuple[int, int] = (2, 2),
    ) -> None:
        self._adapter = adapter
        self._transcriber = transcriber
        self._temp_root = temp_root
        self._keyframe_enabled = keyframe_enabled
        self._frame_interval_seconds = frame_interval_seconds
        self._grid_size = grid_size

    @property
    def media_egress(self) -> str:
        return getattr(getattr(self._adapter, "_media_egress_policy", None), "route", "router_default")

    def extract(self, url: str, job_id: str) -> EvidenceBundle:
        with JobDirectory(self._temp_root, job_id) as job_dir:
            transcript = self._adapter.fetch_caption(url)
            try:
                metadata = self._adapter.fetch_metadata(url)
            except MediaExtractionError:
                raise
            except Exception:
                if transcript is None:
                    raise
                metadata = MediaMetadata(title=url, source_platform="unknown", canonical_url=url)
            keyframe_images: tuple[str, ...] = ()
            if self._keyframe_enabled:
                try:
                    video_path = self._adapter.acquire_video(url, job_dir)
                    keyframe_images = extract_keyframe_images(
                        video_path,
                        job_dir / "keyframes",
                        frame_interval_seconds=self._frame_interval_seconds,
                        grid_size=self._grid_size,
                    )
                except MediaExtractionError:
                    raise
                except Exception as exc:
                    route = self.media_egress
                    raise MediaExtractionError("keyframe", route, str(exc), retryable=True) from exc
            if transcript is None:
                audio = self._adapter.acquire_audio(url, job_dir)
                transcript = self._transcriber.transcribe(audio.path)
            return EvidenceBundle(metadata=metadata, transcript=transcript, keyframe_images=keyframe_images)
