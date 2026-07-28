import re
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from app.ingestion.adapters.base import BaseVideoAdapter
from app.ingestion.domain import EvidenceOrigin, Transcript, TranscriptSegment


class BilibiliCaptionClient(Protocol):
    def fetch_tracks(self, bvid: str, page: int | None) -> list[dict[str, object]]: ...

    def fetch_body(self, subtitle_url: str) -> list[dict[str, object]]: ...


class PublicBilibiliCaptionClient:
    """BiliNote BilibiliSubtitleFetcher with public requests only."""

    _user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent, "Referer": "https://www.bilibili.com"}

    def fetch_tracks(self, bvid: str, page: int | None) -> list[dict[str, object]]:
        import requests

        view = requests.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid, **({"p": page} if page is not None else {})},
            headers=self._headers(),
            timeout=10,
        ).json()
        if view.get("code") != 0:
            return []
        data = view.get("data", {})
        pages = data.get("pages") or []
        selected = pages[(page or 1) - 1] if pages and (page or 1) <= len(pages) else (pages[0] if pages else data)
        cid = selected.get("cid")
        if not cid:
            return []
        player = requests.get(
            "https://api.bilibili.com/x/player/wbi/v2",
            params={"bvid": bvid, "cid": cid},
            headers=self._headers(),
            timeout=10,
        ).json()
        if player.get("code") != 0:
            return []
        return player.get("data", {}).get("subtitle", {}).get("subtitles", []) or []

    def fetch_body(self, subtitle_url: str) -> list[dict[str, object]]:
        import requests

        url = f"https:{subtitle_url}" if subtitle_url.startswith("//") else subtitle_url
        return requests.get(url, headers=self._headers(), timeout=15).json().get("body") or []


class BilibiliAdapter(BaseVideoAdapter):
    platform = "bilibili"
    hosts = frozenset({"bilibili.com", "www.bilibili.com", "b23.tv"})

    def __init__(self, *, caption_client: BilibiliCaptionClient | None = None) -> None:
        self._caption_client = caption_client

    def fetch_caption(self, video_url: str) -> Transcript | None:
        bvid = self._extract_bvid(video_url)
        if not bvid:
            return None
        try:
            tracks = self._get_caption_client().fetch_tracks(bvid, self._extract_page(video_url))
            track = self._pick_track(tracks)
            if track is None or not track.get("subtitle_url"):
                return None
            body = self._get_caption_client().fetch_body(str(track["subtitle_url"]))
            segments = tuple(
                TranscriptSegment(
                    start_seconds=float(item.get("from", 0)),
                    end_seconds=float(item.get("to", 0)),
                    text=str(item.get("content", "")).strip(),
                )
                for item in body
                if str(item.get("content", "")).strip()
            )
            if not segments:
                return None
            return Transcript(
                language=str(track.get("lan") or "zh"),
                origin=EvidenceOrigin.PLATFORM_CAPTION,
                full_text=" ".join(segment.text for segment in segments),
                segments=segments,
            )
        except Exception:
            return None

    def _get_caption_client(self) -> BilibiliCaptionClient:
        if self._caption_client is None:
            self._caption_client = PublicBilibiliCaptionClient()
        return self._caption_client

    @staticmethod
    def _extract_bvid(url: str) -> str | None:
        match = re.search(r"BV([0-9A-Za-z]+)", url)
        return f"BV{match.group(1)}" if match else None

    @staticmethod
    def _extract_page(url: str) -> int | None:
        value = parse_qs(urlsplit(url).query).get("p", [None])[0]
        return int(value) if value and value.isdigit() and int(value) >= 1 else None

    @staticmethod
    def _pick_track(tracks: list[dict[str, object]]) -> dict[str, object] | None:
        if not tracks:
            return None

        def is_zh(track: dict[str, object]) -> bool:
            language = str(track.get("lan") or "").lower()
            return language.startswith("zh") or language == "ai-zh"

        for track in tracks:
            if is_zh(track) and not track.get("ai_type"):
                return track
        for track in tracks:
            if is_zh(track):
                return track
        return tracks[0]
