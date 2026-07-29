from urllib.parse import urlsplit

from app.ingestion.article import ArticleContentParser
from app.ingestion.capabilities import Capability, ResourceKind
from app.ingestion.domain import MediaType, ResourceDescriptor
from app.ingestion.sources import SourceRegistry


class ResourceClassifier:
    def __init__(self, adapters: tuple[object, ...]) -> None:
        self._adapters = adapters

    @classmethod
    def default(cls) -> "ResourceClassifier":
        return cls(SourceRegistry.default().adapters)

    def classify_url(self, url: str) -> ResourceDescriptor:
        normalized_url = url.strip()
        parsed = urlsplit(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("unsupported URL scheme")
        for adapter in self._adapters:
            if adapter.matches(normalized_url):
                probe = adapter.probe(normalized_url)
                media_type = {
                    "article": MediaType.ARTICLE,
                    "audio": MediaType.AUDIO,
                    "video": MediaType.VIDEO,
                    "image": MediaType.IMAGE,
                }.get(probe.resource_kind.value, MediaType.UNKNOWN)
                return ResourceDescriptor(
                    original_url=normalized_url,
                    canonical_url=adapter.normalize(normalized_url),
                    media_type=media_type,
                    source_platform=adapter.platform,
                    resource_kind=probe.resource_kind,
                    capabilities=probe.capabilities,
                )
        article_platform = ArticleContentParser.platform_for_url(normalized_url)
        return ResourceDescriptor(
            original_url=normalized_url,
            canonical_url=normalized_url,
            media_type=MediaType.ARTICLE,
            source_platform=None if article_platform == "web" else article_platform,
            resource_kind=ResourceKind.ARTICLE,
            capabilities=frozenset({Capability.METADATA, Capability.ARTICLE_BODY}),
        )
