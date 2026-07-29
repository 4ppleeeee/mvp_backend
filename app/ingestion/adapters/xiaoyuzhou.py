import html
import re
from pathlib import Path
from urllib.parse import urlsplit

import requests

from app.ingestion.adapters.base import BaseVideoAdapter
from app.ingestion.article import FetchedHtml, HtmlFetcher, SafeHtmlFetcher
from app.ingestion.capabilities import Capability, ResourceKind, SourceProbe
from app.ingestion.domain import MediaExtractionError, MediaMetadata, TemporaryAudio, Transcript


class XiaoyuzhouAdapter(BaseVideoAdapter):
    """Public Xiaoyuzhou episode adapter for audio-only ingestion."""

    platform = "xiaoyuzhou"
    hosts = frozenset({"xiaoyuzhoufm.com", "www.xiaoyuzhoufm.com"})
    _media_hosts = frozenset({"media.xyzcdn.net"})
    _audio_pattern = re.compile(r"https?://[^\"'<>\s]+\.(?:m4a|mp3|aac)(?:\?[^\"'<>\s]*)?", re.IGNORECASE)

    def __init__(self, *, fetcher: HtmlFetcher | None = None) -> None:
        super().__init__()
        self._fetcher = fetcher or SafeHtmlFetcher()

    def probe(self, url: str) -> SourceProbe:
        return SourceProbe(
            source_platform=self.platform,
            resource_kind=ResourceKind.AUDIO,
            capabilities=frozenset({Capability.METADATA, Capability.AUDIO, Capability.TRANSCRIPTION}),
            canonical_url=self.normalize(url),
        )

    def fetch_caption(self, url: str) -> Transcript | None:
        return None

    def fetch_metadata(self, url: str) -> MediaMetadata:
        page = self._fetcher.fetch(url)
        audio_url = self._audio_url(page)
        title = self._meta(page.html, "og:title") or self._title(page.html) or url
        author = self._meta(page.html, "author") or self._meta(page.html, "og:site_name")
        return MediaMetadata(
            title=html.unescape(title).strip(),
            source_platform=self.platform,
            canonical_url=page.url,
            author=html.unescape(author).strip() if author else None,
            thumbnail_url=self._meta(page.html, "og:image"),
            duration_seconds=self._duration(page.html),
        )

    def acquire_audio(self, url: str, job_dir: Path) -> TemporaryAudio:
        try:
            page = self._fetcher.fetch(url)
            audio_url = self._audio_url(page)
            output = job_dir / "xiaoyuzhou.m4a"
            response = requests.get(audio_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=(10, 60), stream=True)
            response.raise_for_status()
            with output.open("wb") as target:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        target.write(chunk)
            return TemporaryAudio(path=str(output), duration_seconds=self._duration(page.html))
        except MediaExtractionError:
            raise
        except Exception as exc:
            raise MediaExtractionError("audio", self.media_egress, str(exc), retryable=True) from exc

    def acquire_video(self, url: str, job_dir: Path) -> Path:
        raise MediaExtractionError("video", self.media_egress, "Xiaoyuzhou source does not provide video", retryable=False)

    @property
    def media_egress(self) -> str:
        return "router_default"

    @classmethod
    def _audio_url(cls, page: FetchedHtml) -> str:
        match = cls._audio_pattern.search(html.unescape(page.html))
        if not match:
            raise MediaExtractionError("audio", "router_default", "Xiaoyuzhou page has no public audio URL", retryable=False)
        parsed = urlsplit(match.group(0))
        if parsed.scheme != "https" or parsed.hostname not in cls._media_hosts:
            raise MediaExtractionError("audio", "router_default", "Xiaoyuzhou audio host is not allowed", retryable=False)
        return match.group(0)

    @staticmethod
    def _meta(page_html: str, name: str) -> str | None:
        pattern = re.compile(rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)', re.IGNORECASE)
        match = pattern.search(page_html)
        return match.group(1) if match else None

    @staticmethod
    def _title(page_html: str) -> str | None:
        match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
        return re.sub(r"\s+", " ", match.group(1)).strip() if match else None

    @staticmethod
    def _duration(page_html: str) -> float | None:
        match = re.search(r'"duration"\s*:\s*(\d+(?:\.\d+)?)', page_html)
        return float(match.group(1)) if match else None
