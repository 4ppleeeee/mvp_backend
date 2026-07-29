from pathlib import Path

from app.ingestion.adapters.base import BaseVideoAdapter
from app.ingestion.douyin_api import DouyinApiClient, DouyinMedia
from app.ingestion.domain import MediaMetadata, TemporaryAudio, Transcript
from app.ingestion.media import BiliNoteFfmpegAudioExtractor, MediaEgressPolicy


class DouyinAdapter(BaseVideoAdapter):
    platform = "douyin"
    hosts = frozenset({"douyin.com", "www.douyin.com", "v.douyin.com", "tiktok.com", "www.tiktok.com"})

    def __init__(
        self,
        *,
        media_egress_policy: MediaEgressPolicy | None = None,
        api_client: DouyinApiClient | None = None,
        audio_extractor: BiliNoteFfmpegAudioExtractor | None = None,
    ) -> None:
        super().__init__(media_egress_policy=media_egress_policy)
        self._api_client = api_client or DouyinApiClient(policy=media_egress_policy)
        self._audio_extractor = audio_extractor or BiliNoteFfmpegAudioExtractor()

    def fetch_caption(self, url: str) -> Transcript | None:
        return None

    def fetch_metadata(self, url: str) -> MediaMetadata:
        media = self._api_client.fetch_media(url)
        return self._metadata(url, media)

    def acquire_audio(self, url: str, job_dir: Path) -> TemporaryAudio:
        media = self._api_client.fetch_media(url)
        video_path = self._api_client.download_video(media, job_dir / f"{media.aweme_id}.mp4")
        return self._audio_extractor.extract(video_path, job_dir)

    def acquire_video(self, url: str, job_dir: Path) -> Path:
        media = self._api_client.fetch_media(url)
        return self._api_client.download_video(media, job_dir / f"{media.aweme_id}.mp4")

    def _metadata(self, url: str, media: DouyinMedia) -> MediaMetadata:
        return MediaMetadata(
            title=media.title,
            source_platform=self.platform,
            canonical_url=url,
            duration_seconds=media.duration_seconds,
            author=media.author,
            published_at=media.published_at,
            thumbnail_url=media.thumbnail_url,
        )
