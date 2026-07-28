from urllib.parse import urlsplit

from app.ingestion.adapters import default_video_adapters
from app.ingestion.domain import MediaType, ResourceDescriptor


class ResourceClassifier:
    def __init__(self, adapters: tuple[object, ...]) -> None:
        self._adapters = adapters

    @classmethod
    def default(cls) -> "ResourceClassifier":
        return cls(default_video_adapters())

    def classify_url(self, url: str) -> ResourceDescriptor:
        normalized_url = url.strip()
        parsed = urlsplit(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("unsupported URL scheme")
        for adapter in self._adapters:
            if adapter.matches(normalized_url):
                return ResourceDescriptor(
                    original_url=normalized_url,
                    canonical_url=adapter.normalize(normalized_url),
                    media_type=MediaType.VIDEO,
                    source_platform=adapter.platform,
                )
        return ResourceDescriptor(
            original_url=normalized_url,
            canonical_url=normalized_url,
            media_type=MediaType.ARTICLE,
            source_platform=None,
        )
