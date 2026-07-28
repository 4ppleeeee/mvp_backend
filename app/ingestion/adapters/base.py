from abc import ABC
from urllib.parse import urlsplit


class BaseVideoAdapter(ABC):
    platform: str
    hosts: frozenset[str]

    def matches(self, url: str) -> bool:
        host = urlsplit(url).hostname
        if host is None:
            return False
        return host.lower() in self.hosts

    def normalize(self, url: str) -> str:
        return url
