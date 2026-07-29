"""Temporary media handling adapted from BiliNote's downloader implementations.

Copyright (c) 2024 Jeffery Huang. Licensed under the MIT License.
"""

import shutil
import subprocess
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from app.ingestion.domain import MediaMetadata, TemporaryAudio
from app.ingestion.domain import MediaExtractionError


class MediaEgressPolicy:
    def __init__(self, proxy_url: str | None = None) -> None:
        self._proxy_url = proxy_url.strip() if proxy_url and proxy_url.strip() else None

    @property
    def route(self) -> str:
        return "configured_proxy" if self._proxy_url else "router_default"

    @property
    def proxy_url(self) -> str | None:
        """Expose the configured route to source-specific access providers."""
        return self._proxy_url

    def yt_dlp_options(self) -> dict[str, object]:
        options: dict[str, object] = {
            "js_runtimes": {"node": {}},
            "remote_components": ["ejs:github"],
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
        }
        if self._proxy_url:
            options["proxy"] = self._proxy_url
        return options

    def transcript_session(self):
        import requests

        session = requests.Session()
        if self._proxy_url:
            session.proxies.update({"http": self._proxy_url, "https": self._proxy_url})
        return session


class JobDirectory(AbstractContextManager[Path]):
    def __init__(self, root: Path, job_id: str) -> None:
        if not job_id or Path(job_id).name != job_id:
            raise ValueError("invalid job id")
        self._path = root / job_id

    def __enter__(self) -> Path:
        self._path.mkdir(parents=True, exist_ok=False)
        return self._path

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        shutil.rmtree(self._path, ignore_errors=True)


@dataclass(frozen=True)
class BiliNoteMediaResult:
    audio: TemporaryAudio | None
    metadata: MediaMetadata


class BiliNoteYtDlpAcquirer:
    """BiliNote YoutubeDownloader.download adapted to TripGuard values."""

    def __init__(self, policy: MediaEgressPolicy | None = None) -> None:
        self._policy = policy or MediaEgressPolicy()

    def download(self, video_url: str, output_dir: Path, *, platform: str, skip_download: bool = False, phase: str = "audio") -> BiliNoteMediaResult:
        import yt_dlp

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / "%(id)s.%(ext)s")
        ydl_opts: dict[str, object] = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": output_path,
            "noplaylist": True,
            "quiet": True,
        }
        ydl_opts.update(self._policy.yt_dlp_options())
        if skip_download:
            ydl_opts["skip_download"] = True

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=not skip_download)
        except Exception as exc:
            raise MediaExtractionError(phase, self._policy.route, str(exc), retryable=True) from exc

        video_id = str(info["id"])
        extension = str(info.get("ext", "m4a"))
        audio_path = output_dir / f"{video_id}.{extension}"
        metadata = MediaMetadata(
            title=str(info.get("title") or video_id),
            source_platform=platform,
            canonical_url=video_url,
            duration_seconds=float(info["duration"]) if info.get("duration") is not None else None,
            author=str(info.get("channel") or info.get("uploader") or "") or None,
            published_at=str(info.get("upload_date") or "") or None,
            thumbnail_url=str(info.get("thumbnail") or "") or None,
        )
        audio = None if skip_download else TemporaryAudio(path=str(audio_path), duration_seconds=metadata.duration_seconds)
        return BiliNoteMediaResult(audio=audio, metadata=metadata)

    def download_video(self, video_url: str, output_dir: Path, *, platform: str) -> Path:
        import yt_dlp

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / "%(id)s.%(ext)s")
        ydl_opts: dict[str, object] = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_path,
            "noplaylist": True,
            "quiet": True,
        }
        ydl_opts.update(self._policy.yt_dlp_options())
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
        except Exception as exc:
            raise MediaExtractionError("video", self._policy.route, str(exc), retryable=True) from exc

        video_id = str(info["id"])
        extension = str(info.get("ext", "mp4"))
        candidates = (output_dir / f"{video_id}.mp4", output_dir / f"{video_id}.{extension}")
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise MediaExtractionError("video", self._policy.route, "video download produced no file", retryable=True)


class BiliNoteFfmpegAudioExtractor:
    """BiliNote LocalDownloader.convert_to_mp3 adapted to a task-local output."""

    def extract(self, input_path: Path, output_dir: Path) -> TemporaryAudio:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "audio.mp3"
        command = [
            "ffmpeg",
            "-i",
            str(input_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-y",
            str(output_path),
        ]
        subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if not output_path.exists():
            raise RuntimeError("mp3 generation failed")
        return TemporaryAudio(path=str(output_path))
