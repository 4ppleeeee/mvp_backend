from collections.abc import Iterable
from typing import Protocol

from app.ingestion.adapters import default_video_adapters
from app.ingestion.adapters.xiaohongshu import XiaohongshuAdapter
from app.ingestion.adapters.xiaoyuzhou import XiaoyuzhouAdapter
from app.ingestion.capabilities import SourceProbe
from app.ingestion.media import MediaEgressPolicy


class SourceAdapter(Protocol):
    platform: str

    def matches(self, url: str) -> bool: ...

    def normalize(self, url: str) -> str: ...

    def probe(self, url: str) -> SourceProbe: ...


class SourceRegistry:
    def __init__(self, adapters: Iterable[SourceAdapter]) -> None:
        self._adapters = tuple(adapters)

    @classmethod
    def default(
        cls,
        media_egress_policy: MediaEgressPolicy | None = None,
        *,
        include_xiaohongshu: bool = False,
    ) -> "SourceRegistry":
        adapters = list(default_video_adapters(media_egress_policy, include_xiaohongshu=include_xiaohongshu))
        adapters.append(XiaoyuzhouAdapter())
        return cls(tuple(adapters))

    def resolve(self, url: str) -> SourceAdapter | None:
        normalized = url.strip()
        return next((adapter for adapter in self._adapters if adapter.matches(normalized)), None)

    @property
    def adapters(self) -> tuple[SourceAdapter, ...]:
        return self._adapters
