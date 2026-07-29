from app.ingestion.adapters.base import BaseVideoAdapter
from app.ingestion.domain import Transcript


class XiaohongshuAdapter(BaseVideoAdapter):
    """Public video-note adapter; yt-dlp handles the temporary media fetch."""

    platform = "xiaohongshu"
    hosts = frozenset({"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.cn", "xhslink.com"})

    def fetch_caption(self, url: str) -> Transcript | None:
        """XHS has no supported public caption endpoint; let the pipeline use Whisper."""
        return None
