from urllib.parse import urlsplit

from app.ingestion.adapters.base import BaseVideoAdapter
from app.ingestion.domain import Transcript


class XiaohongshuAdapter(BaseVideoAdapter):
    """Public video-note adapter; yt-dlp acquires temporary media for Whisper."""

    platform = "xiaohongshu"
    hosts = frozenset({"xiaohongshu.com", "www.xiaohongshu.com"})

    def matches(self, url: str) -> bool:
        parsed = urlsplit(url)
        return (parsed.hostname or "").lower() in self.hosts and parsed.path.startswith("/discovery/item/")

    def fetch_caption(self, _: str) -> Transcript | None:
        """Xiaohongshu exposes no supported public caption endpoint."""
        return None
