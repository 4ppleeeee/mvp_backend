"""Capability-driven media extraction following BiliNote's subtitle-first order."""

from dataclasses import replace
from collections.abc import Callable
import inspect
from pathlib import Path
from typing import Protocol

from app.ingestion.capabilities import Capability, ResourceKind
from app.ingestion.domain import EvidenceBundle, MediaExtractionError, MediaMetadata, MediaType, TemporaryAudio, Transcript
from app.ingestion.transcriber import ProgressCallback
from app.ingestion.keyframes import extract_keyframe_images
from app.ingestion.media import JobDirectory
from app.ingestion.planner import IngestionPlan


class PipelineAdapter(Protocol):
    def fetch_metadata(self, url: str) -> MediaMetadata: ...

    def fetch_caption(self, url: str) -> Transcript | None: ...

    def acquire_audio(self, url: str, job_dir: Path) -> TemporaryAudio: ...

    def acquire_video(self, url: str, job_dir: Path) -> Path: ...


class PipelineTranscriber(Protocol):
    def transcribe(self, file_path: str, *, progress_callback: ProgressCallback | None = None) -> Transcript: ...


PipelineProgressCallback = Callable[[str, int, str], None]


class MediaPipeline:
    def __init__(
        self,
        *,
        adapter: PipelineAdapter,
        transcriber: PipelineTranscriber,
        temp_root: Path,
        keyframe_enabled: bool = False,
        frame_interval_seconds: int = 6,
        grid_size: tuple[int, int] = (2, 2),
        plan: IngestionPlan | None = None,
    ) -> None:
        self._adapter = adapter
        self._transcriber = transcriber
        self._temp_root = temp_root
        self._keyframe_enabled = keyframe_enabled
        self._frame_interval_seconds = frame_interval_seconds
        self._grid_size = grid_size
        probe = getattr(adapter, "probe", None)
        self._plan = plan or (IngestionPlan.from_probe(probe("")) if callable(probe) else None)

    @property
    def media_egress(self) -> str:
        return getattr(getattr(self._adapter, "_media_egress_policy", None), "route", "router_default")

    def extract(self, url: str, job_id: str, *, progress_callback: PipelineProgressCallback | None = None) -> EvidenceBundle:
        with JobDirectory(self._temp_root, job_id) as job_dir:
            if progress_callback:
                progress_callback("extracting", 10, "获取平台字幕")
            can_fetch_caption = self._plan is None or self._plan.fetch_caption_first
            transcript = self._adapter.fetch_caption(url) if can_fetch_caption else None
            try:
                if progress_callback:
                    progress_callback("extracting", 20, "获取媒体元数据")
                metadata = self._adapter.fetch_metadata(url)
            except MediaExtractionError:
                raise
            except Exception:
                if transcript is None:
                    raise
                metadata = MediaMetadata(title=url, source_platform="unknown", canonical_url=url)
            keyframe_images: tuple[str, ...] = ()
            if self._keyframe_enabled:
                if self._plan is not None and not self._plan.probe.supports(Capability.VIDEO):
                    raise MediaExtractionError("keyframe", self.media_egress, "source does not support video keyframes", retryable=False)
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
                if self._plan is not None and not self._plan.transcribe_audio_when_caption_missing:
                    raise MediaExtractionError("audio", self.media_egress, "source does not support audio transcription", retryable=False)
                audio = self._adapter.acquire_audio(url, job_dir)
                if progress_callback:
                    progress_callback("extracting", 40, "音频下载完成，准备 Whisper 转写")
                transcribe_callback = (
                    lambda percent, message: progress_callback("transcribing", percent, message)
                    if progress_callback
                    else None
                )
                if "progress_callback" in inspect.signature(self._transcriber.transcribe).parameters:
                    transcript = self._transcriber.transcribe(audio.path, progress_callback=transcribe_callback)
                else:
                    transcript = self._transcriber.transcribe(audio.path)
            if self._plan is not None and self._plan.probe.resource_kind is ResourceKind.AUDIO:
                transcript = replace(transcript, media_type=MediaType.AUDIO)
            return EvidenceBundle(metadata=metadata, transcript=transcript, keyframe_images=keyframe_images)


VideoPipeline = MediaPipeline
