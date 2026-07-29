from abc import ABC
from pathlib import Path
from urllib.parse import urlsplit

from app.ingestion.domain import MediaMetadata, TemporaryAudio, Transcript
from app.ingestion.media import BiliNoteYtDlpAcquirer, MediaEgressPolicy


class BaseVideoAdapter(ABC):
    platform: str
    hosts: frozenset[str]

    def __init__(self, *, media_egress_policy: MediaEgressPolicy | None = None) -> None:
        self._media_egress_policy = media_egress_policy or MediaEgressPolicy()

    def matches(self, url: str) -> bool:
        host = urlsplit(url).hostname
        if host is None:
            return False
        return host.lower() in self.hosts

    def normalize(self, url: str) -> str:
        return url

    def fetch_caption(self, _: str) -> Transcript | None:
        """Return no caption when a platform has no supported public caption API."""
        return None

    def fetch_metadata(self, url: str) -> MediaMetadata:
        return BiliNoteYtDlpAcquirer(self._media_egress_policy).download(
            url, Path("."), platform=self.platform, skip_download=True, phase="metadata"
        ).metadata

    def acquire_audio(self, url: str, job_dir: Path) -> TemporaryAudio:
        result = BiliNoteYtDlpAcquirer(self._media_egress_policy).download(
            url, job_dir, platform=self.platform, phase="audio"
        )
        if result.audio is None:
            raise RuntimeError("audio download did not produce a file")
        return result.audio

    def acquire_video(self, url: str, job_dir: Path) -> Path:
        return BiliNoteYtDlpAcquirer(self._media_egress_policy).download_video(
            url, job_dir, platform=self.platform
        )
