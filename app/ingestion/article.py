"""Public HTML extraction for article-style ingestion without account state."""

import html as html_module
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import requests

from app.ingestion.domain import EvidenceBundle, EvidenceOrigin, MediaExtractionError, MediaMetadata, MediaType, Transcript, TranscriptSegment


@dataclass(frozen=True)
class ArticleContent:
    platform: str
    title: str | None
    body_text: str | None
    cover_image_url: str | None


@dataclass(frozen=True)
class FetchedHtml:
    url: str
    html: str


class HtmlFetcher(Protocol):
    def fetch(self, url: str) -> FetchedHtml: ...


class SafeHtmlFetcher:
    """Fetch a bounded public HTML page while validating every redirect target."""

    _max_redirects = 5
    _max_bytes = 2 * 1024 * 1024

    def fetch(self, url: str) -> FetchedHtml:
        current_url = url
        for _ in range(self._max_redirects + 1):
            self._require_public_url(current_url)
            response = requests.get(
                current_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; TripGuard/0.1; +https://github.com/4ppleeeee/mvp_backend)",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                },
                timeout=(10, 20),
                allow_redirects=False,
                stream=True,
            )
            try:
                if response.is_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError(f"HTTP {response.status_code} without redirect location")
                    current_url = urljoin(current_url, location)
                    continue
                if not 200 <= response.status_code < 300:
                    raise ValueError(f"HTTP {response.status_code}")
                content_type = response.headers.get("Content-Type", "").lower()
                if "html" not in content_type:
                    raise ValueError("response is not HTML")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise ValueError("HTML response exceeds 2 MiB")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                return FetchedHtml(url=current_url, html=b"".join(chunks).decode(encoding, errors="replace"))
            finally:
                response.close()
        raise ValueError("too many redirects")

    @staticmethod
    def _require_public_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("unsupported URL scheme")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise ValueError("unable to resolve URL host") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("URL host is not publicly routable")


class _MetadataHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value for name, value in attrs}
        if tag.lower() == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content and key.lower() not in self.meta:
                self.meta[key.lower()] = content.strip()
        if tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    @property
    def title(self) -> str | None:
        value = " ".join(self._title_parts).strip()
        return value or None


class ArticleContentParser:
    """Port of the demo's public XHS state parser and generic meta fallback."""

    _xhs_hosts = frozenset({"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.cn", "xhslink.com"})
    _mafengwo_hosts = frozenset({"mafengwo.cn", "www.mafengwo.cn", "imfw.cn"})

    def parse(self, url: str, page_html: str) -> ArticleContent:
        platform = self.platform_for_url(url)
        if platform == "xiaohongshu":
            xhs_content = self._parse_xhs(url, page_html)
            if xhs_content is not None:
                return xhs_content
        return self._parse_generic(platform, page_html)

    @classmethod
    def platform_for_url(cls, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        if host in cls._xhs_hosts or host.endswith(".xiaohongshu.com"):
            return "xiaohongshu"
        if host in cls._mafengwo_hosts or host.endswith(".mafengwo.cn"):
            return "mafengwo"
        return "web"

    def _parse_xhs(self, url: str, page_html: str) -> ArticleContent | None:
        state = self._extract_initial_state(page_html)
        if state is None:
            return None
        try:
            root = json.loads(re.sub(r"(?<![\\w\"])undefined(?![\\w\"])", "null", state))
        except json.JSONDecodeError:
            return None
        note_id = self._note_id(url)
        note = (
            root.get("noteData", {}).get("data", {}).get("noteData")
            or root.get("note", {}).get("noteDetailMap", {}).get(note_id or "", {}).get("note")
        )
        if not isinstance(note, dict):
            detail_map = root.get("note", {}).get("noteDetailMap", {})
            if isinstance(detail_map, dict):
                note = next(
                    (item.get("note") for item in detail_map.values() if isinstance(item, dict) and isinstance(item.get("note"), dict)),
                    None,
                )
        if not isinstance(note, dict):
            return None
        images = note.get("imageList")
        first_image = images[0] if isinstance(images, list) and images and isinstance(images[0], dict) else {}
        return ArticleContent(
            platform="xiaohongshu",
            title=self._clean_text(note.get("title")),
            body_text=self._clean_text(note.get("desc")) or self._extract_xhs_dom_description(page_html),
            cover_image_url=self._clean_text(first_image.get("urlDefault")) or self._clean_text(first_image.get("url")),
        )

    @staticmethod
    def _extract_initial_state(page_html: str) -> str | None:
        marker = "window.__INITIAL_STATE__="
        start = page_html.find(marker)
        if start < 0:
            return None
        value_start = start + len(marker)
        end = page_html.find("</script>", value_start)
        return page_html[value_start : len(page_html) if end < 0 else end].strip().rstrip(";")

    def _parse_generic(self, platform: str, page_html: str) -> ArticleContent:
        parser = _MetadataHtmlParser()
        parser.feed(page_html)
        title = parser.meta.get("og:title") or parser.meta.get("twitter:title") or parser.title
        description = parser.meta.get("description") or parser.meta.get("og:description") or parser.meta.get("twitter:description")
        image = parser.meta.get("og:image") or parser.meta.get("twitter:image")
        return ArticleContent(
            platform=platform,
            title=self._clean_text(title),
            body_text=self._clean_text(description),
            cover_image_url=self._clean_text(image),
        )

    @staticmethod
    def _note_id(url: str) -> str | None:
        match = re.search(r"/(?:explore|(?:discovery/)?item)/([0-9a-fA-F]+)", url)
        return match.group(1) if match else None

    @staticmethod
    def _extract_xhs_dom_description(page_html: str) -> str | None:
        match = re.search(r"<div[^>]+class=[\"'][^\"']*author-desc-content[^\"']*[\"'][^>]*>(.*?)</div>", page_html, re.IGNORECASE | re.DOTALL)
        if match is None:
            return None
        text = re.sub(r"<br\\s*/?>", "\n", match.group(1), flags=re.IGNORECASE)
        text = re.sub(r"<!--.*?-->|<[^>]+>", "", text, flags=re.DOTALL)
        return ArticleContentParser._clean_text(text, preserve_lines=True)

    @staticmethod
    def _clean_text(value: object, preserve_lines: bool = False) -> str | None:
        if not isinstance(value, str):
            return None
        decoded = html_module.unescape(value)
        cleaned = "\n".join(part.strip() for part in decoded.splitlines() if part.strip()) if preserve_lines else " ".join(decoded.split())
        return cleaned or None


class ArticlePipeline:
    media_egress = "router_default"

    def __init__(self, *, fetcher: HtmlFetcher | None = None, parser: ArticleContentParser | None = None) -> None:
        self._fetcher = fetcher or SafeHtmlFetcher()
        self._parser = parser or ArticleContentParser()

    def extract(self, url: str, _: str) -> EvidenceBundle:
        try:
            page = self._fetcher.fetch(url)
            content = self._parser.parse(page.url, page.html)
        except Exception as exc:
            raise MediaExtractionError("metadata", self.media_egress, str(exc)) from exc
        body_text = content.body_text or content.title
        if not body_text:
            raise MediaExtractionError("metadata", self.media_egress, "public page has no extractable title or text")
        title = content.title or body_text[:80]
        return EvidenceBundle(
            metadata=MediaMetadata(
                title=title,
                source_platform=content.platform,
                canonical_url=page.url,
                thumbnail_url=content.cover_image_url,
            ),
            transcript=Transcript(
                language="zh",
                origin=EvidenceOrigin.ARTICLE,
                full_text=body_text,
                segments=(TranscriptSegment(start_seconds=0, end_seconds=0, text=body_text),),
                media_type=MediaType.ARTICLE,
            ),
        )
