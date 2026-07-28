"""Subtitle-first video extraction following BiliNote's NoteGenerator order."""

from pathlib import Path
from typing import Protocol

from app.ingestion.domain import EvidenceBundle, MediaMetadata, TemporaryAudio, Transcript
from app.ingestion.media import JobDirectory


class PipelineAdapter(Protocol):
    def fetch_metadata(self, url: str) -> MediaMetadata: ...

    def fetch_caption(self, url: str) -> Transcript | None: ...

    def acquire_audio(self, url: str, job_dir: Path) -> TemporaryAudio: ...


class PipelineTranscriber(Protocol):
    def transcribe(self, file_path: str) -> Transcript: ...


class VideoPipeline:
    def __init__(self, *, adapter: PipelineAdapter, transcriber: PipelineTranscriber, temp_root: Path) -> None:
        self._adapter = adapter
        self._transcriber = transcriber
        self._temp_root = temp_root

    def extract(self, url: str, job_id: str) -> EvidenceBundle:
        with JobDirectory(self._temp_root, job_id) as job_dir:
            metadata = self._adapter.fetch_metadata(url)
            transcript = self._adapter.fetch_caption(url)
            if transcript is None:
                audio = self._adapter.acquire_audio(url, job_dir)
                transcript = self._transcriber.transcribe(audio.path)
            return EvidenceBundle(metadata=metadata, transcript=transcript)
