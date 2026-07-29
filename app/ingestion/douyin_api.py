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
# Required by the same public mssdk request used in BiliNote. It is protocol
# data, not an account credential, and is sent only to the upstream endpoint.
_MS_TOKEN_STR_DATA = (
    "fWOdJTQR3/jwmZqBBsPO6tdNEc1jX7YTwPg0Z8CT+j3HScLFbj2Zm1XQ7/lqgSutntVKLJWaY3Hc/+vc0h+So9N1t6EqiImu5jKyUa+S4NPy6cNP0x9CUQQgb4+RRihCgsn4QyV8jivEFOsj3N5zFQbzXRyOV+9aG5B5EAnwpn8C70llsWq0zJz1VjN6y2KZiBZRyonAHE8feSGpwMDeUTllvq6BG3AQZz7RrORLWNCLEoGzM6bMovYVPRAJipuUML4Hq/568bNb5vqAo0eOFpvTZjQFgbB7f/CtAYYmnOYlvfrHKBKvb0TX6AjYrw2qmNNEer2ADJosmT5kZeBsogDui8rNiI/OOdX9PVotmcSmHOLRfw1cYXTgwHXr6cJeJveuipgwtUj2FNT4YCdZfUGGyRDz5bR5bdBuYiSRteSX12EktobsKPksdhUPGGv99SI1QRVmR0ETdWqnKWOj/7ujFZsNnfCLxNfqxQYEZEp9/U01CHhWLVrdzlrJ1v+KJH9EA4P1Wo5/2fuBFVdIz2upFqEQ11DJu8LSyD43qpTok+hFG3Moqrr81uPYiyPHnUvTFgwA/TIE11mTc/pNvYIb8IdbE4UAlsR90eYvPkI+rK9KpYN/l0s9ti9sqTth12VAw8tzCQvhKtxevJRQntU3STeZ3coz9Dg8qkvaSNFWuBDuyefZBGVSgILFdMy33//l/eTXhQpFrVc9OyxDNsG6cvdFwu7trkAENHU5eQEWkFSXBx9Ml54+fa3LvJBoacfPViyvzkJworlHcYYTG392L4q6wuMSSpYUconb+0c5mwqnnLP6MvRdm/bBTaY2Q6RfJd8"
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
            response.raise_for_status()
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
