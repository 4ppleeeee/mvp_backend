"""Douyin's public web-detail flow adapted from BiliNote's downloader.

No cookies, browser state, tokens, or media are persisted.  A fresh msToken is
requested in-memory for each detail request, then discarded.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote, urlencode, urlsplit

from app.ingestion.media import MediaEgressPolicy

if TYPE_CHECKING:
    import requests


DOUYIN_WEB_URL = "https://www.douyin.com"
_DETAIL_HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
}
_MS_TOKEN_URL = "https://mssdk.bytedance.com/web/report"
_MS_TOKEN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36 Edg/117.0.2045.47"
)
_MS_TOKEN_STR_DATA = (
    "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"
    "D+kHUhTS2BGfsHadR3d5j8lNhBPzA5e+mE=="
)


@dataclass(frozen=True)
class DouyinMedia:
    aweme_id: str
    title: str
    video_url: str
    duration_seconds: float | None
    author: str | None
    published_at: str | None
    thumbnail_url: str | None


class MsTokenClient(Protocol):
    def fetch(self) -> str: ...


class ABogusSigner(Protocol):
    def sign(self, params: dict[str, object]) -> str: ...


class BiliNoteABogusSigner:
    def sign(self, params: dict[str, object]) -> str:
        from app.ingestion.douyin_abogus import ABogus

        return ABogus().get_value(params)


class BiliNoteMsTokenClient:
    """The mssdk request and transport retry policy from BiliNote."""

    def __init__(self, policy: MediaEgressPolicy) -> None:
        self._policy = policy

    def fetch(self) -> str:
        import httpx

        payload = json.dumps(
            {
                "magic": 538969122,
                "version": 1,
                "dataType": 8,
                "strData": _MS_TOKEN_STR_DATA,
                "tspFromClient": int(dt.datetime.now(dt.UTC).timestamp() * 1000),
            }
        )
        options: dict[str, object] = {"transport": httpx.HTTPTransport(retries=5), "timeout": 20.0}
        if self._policy.proxy_url:
            options["proxy"] = self._policy.proxy_url
        with httpx.Client(**options) as client:
            response = client.post(
                _MS_TOKEN_URL,
                content=payload,
                headers={"User-Agent": _MS_TOKEN_USER_AGENT, "Content-Type": "application/json"},
            )
            response.raise_for_status()
            ms_token = response.cookies.get("msToken")
        if not ms_token or len(ms_token) not in (120, 128):
            raise ValueError("Douyin msToken response was invalid")
        return ms_token


class DouyinApiClient:
    def __init__(
        self,
        *,
        policy: MediaEgressPolicy | None = None,
        session: "requests.Session | None" = None,
        token_client: MsTokenClient | None = None,
        signer: ABogusSigner | None = None,
        token_attempts: int = 5,
    ) -> None:
        self._policy = policy or MediaEgressPolicy()
        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        if self._policy.proxy_url:
            self._session.proxies.update({"http": self._policy.proxy_url, "https": self._policy.proxy_url})
        self._token_client = token_client or BiliNoteMsTokenClient(self._policy)
        self._signer = signer or BiliNoteABogusSigner()
        self._token_attempts = token_attempts

    def fetch_media(self, url: str) -> DouyinMedia:
        aweme_id = self._extract_aweme_id(url)
        if not aweme_id:
            raise ValueError("Douyin aweme id was not found")
        params: dict[str, object] = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": 1,
            "version_code": "290100",
            "version_name": "29.1.0",
            "cookie_enabled": "true",
            "screen_width": 1920,
            "screen_height": 1080,
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "130.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "130.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": 12,
            "device_memory": 8,
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "from_user_page": "1",
            "locate_query": "false",
            "need_time_list": "1",
            "pc_libra_divert": "Windows",
            "publish_video_strategy_type": "2",
            "round_trip_time": "0",
            "show_live_replay_strategy": "1",
            "time_list_query": "0",
            "whale_cut_token": "",
            "update_version_code": "170400",
            "aweme_id": aweme_id,
            "msToken": self._fetch_ms_token(),
        }
        signed = quote(self._signer.sign(params), safe="")
        detail_url = f"{DOUYIN_WEB_URL}/aweme/v1/web/aweme/detail/?{urlencode(params)}&a_bogus={signed}"
        response = self._session.get(detail_url, headers=_DETAIL_HEADERS, timeout=20)
        response.raise_for_status()
        return self._parse_media(response.json())

    def download_video(self, media: DouyinMedia, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self._session.get(media.video_url, headers=_DETAIL_HEADERS, timeout=60, stream=True)
        response.raise_for_status()
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError("Douyin video download produced no data")
        return destination

    def _extract_aweme_id(self, url: str) -> str | None:
        parsed = urlsplit(url)
        resolved = url
        if parsed.hostname and parsed.hostname.lower() == "v.douyin.com":
            response = self._session.head(url, allow_redirects=True, headers=_DETAIL_HEADERS, timeout=20)
            # BiliNote deliberately uses the final redirect URL even when the
            # HEAD endpoint returns a non-2xx status after resolving it.
            resolved = response.url
        for component in urlsplit(resolved).path.split("/"):
            if component.isdigit() and len(component) >= 10:
                return component
        query = urlsplit(resolved).query
        for item in query.split("&"):
            name, _, value = item.partition("=")
            if name == "aweme_id" and value.isdigit():
                return value
        return None

    def _fetch_ms_token(self) -> str:
        last_error: Exception | None = None
        for _ in range(self._token_attempts):
            try:
                return self._token_client.fetch()
            except Exception as exc:
                last_error = exc
        raise RuntimeError("Douyin msToken request failed") from last_error

    @staticmethod
    def _parse_media(payload: dict[str, object]) -> DouyinMedia:
        detail = payload.get("aweme_detail")
        if not isinstance(detail, dict):
            raise ValueError("Douyin detail response did not contain an aweme")
        video = detail.get("video")
        if not isinstance(video, dict):
            raise ValueError("Douyin detail response did not contain a video")
        video_url = _first_url(video.get("download_addr")) or _first_url(video.get("play_addr"))
        if not video_url:
            raise ValueError("Douyin detail response did not contain a video address")
        author = detail.get("author")
        cover = video.get("cover_original_scale") or video.get("cover")
        created_at = detail.get("create_time")
        published_at = None
        if isinstance(created_at, (int, float)):
            published_at = dt.datetime.fromtimestamp(created_at, dt.UTC).date().isoformat()
        duration = video.get("duration")
        return DouyinMedia(
            aweme_id=str(detail.get("aweme_id") or ""),
            title=str(detail.get("item_title") or detail.get("desc") or detail.get("aweme_id") or "Douyin video"),
            video_url=video_url,
            duration_seconds=float(duration) / 1000 if isinstance(duration, (int, float)) else None,
            author=str(author.get("nickname") or "") or None if isinstance(author, dict) else None,
            published_at=published_at,
            thumbnail_url=_first_url(cover),
        )


def _first_url(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    urls = value.get("url_list")
    if not isinstance(urls, list):
        return None
    for url in urls:
        if isinstance(url, str) and url:
            return url
    return None

