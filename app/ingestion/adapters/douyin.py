from pathlib import Path

from app.ingestion.adapters.base import BaseVideoAdapter
from app.ingestion.domain import MediaExtractionError, MediaMetadata, TemporaryAudio
from app.ingestion.douyin_api import DouyinApiClient, DouyinMedia
from app.ingestion.media import BiliNoteFfmpegAudioExtractor, MediaEgressPolicy


class DouyinAdapter(BaseVideoAdapter):
    platform = "douyin"
    hosts = frozenset({"douyin.com", "www.douyin.com", "v.douyin.com", "tiktok.com", "www.tiktok.com"})

    def __init__(self, *, media_egress_policy: MediaEgressPolicy | None = None) -> None:
        super().__init__(media_egress_policy=media_egress_policy)
        self._api_client = DouyinApiClient(policy=self._media_egress_policy)
        self._audio_extractor = BiliNoteFfmpegAudioExtractor()

    def fetch_metadata(self, url: str) -> MediaMetadata:
        media = self._fetch_media(url, phase="metadata")
        return self._metadata_from(media, url)

    def acquire_audio(self, url: str, job_dir: Path) -> TemporaryAudio:
        media = self._fetch_media(url, phase="audio")
        video_path = self._download(media, job_dir, phase="audio")
        try:
            audio = self._audio_extractor.extract(video_path, job_dir)
        except Exception as exc:
            raise MediaExtractionError("audio", self._media_egress_policy.route, str(exc), retryable=True) from exc
        return TemporaryAudio(path=audio.path, duration_seconds=media.duration_seconds)

    def acquire_video(self, url: str, job_dir: Path) -> Path:
        media = self._fetch_media(url, phase="video")
        return self._download(media, job_dir, phase="video")

    def _fetch_media(self, url: str, *, phase: str) -> DouyinMedia:
        try:
            return self._api_client.fetch_media(url)
        except Exception as exc:
            raise MediaExtractionError(phase, self._media_egress_policy.route, str(exc), retryable=True) from exc

    def _download(self, media: DouyinMedia, job_dir: Path, *, phase: str) -> Path:
        try:
            return self._api_client.download_video(media, job_dir / f"{media.aweme_id}.mp4")
        except Exception as exc:
            raise MediaExtractionError(phase, self._media_egress_policy.route, str(exc), retryable=True) from exc

    @staticmethod
    def _metadata_from(media: DouyinMedia, url: str) -> MediaMetadata:
        return MediaMetadata(
            title=media.title,
            source_platform="douyin",
            canonical_url=url,
            duration_seconds=media.duration_seconds,
            author=media.author,
            published_at=media.published_at,
            thumbnail_url=media.thumbnail_url,
        )
