from abc import ABC
from pathlib import Path
from urllib.parse import urlsplit

from app.ingestion.domain import MediaMetadata, TemporaryAudio
from app.ingestion.media import BiliNoteYtDlpAcquirer


class BaseVideoAdapter(ABC):
    platform: str
    hosts: frozenset[str]

    def matches(self, url: str) -> bool:
        host = urlsplit(url).hostname
        if host is None:
            return False
        return host.lower() in self.hosts

    def normalize(self, url: str) -> str:
        return url

    def fetch_metadata(self, url: str) -> MediaMetadata:
        return BiliNoteYtDlpAcquirer().download(url, Path("."), platform=self.platform, skip_download=True).metadata

    def acquire_audio(self, url: str, job_dir: Path) -> TemporaryAudio:
        result = BiliNoteYtDlpAcquirer().download(url, job_dir, platform=self.platform)
        if result.audio is None:
            raise RuntimeError("audio download did not produce a file")
        return result.audio
