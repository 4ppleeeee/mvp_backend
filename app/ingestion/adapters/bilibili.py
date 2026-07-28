from app.ingestion.adapters.base import BaseVideoAdapter


class BilibiliAdapter(BaseVideoAdapter):
    platform = "bilibili"
    hosts = frozenset({"bilibili.com", "www.bilibili.com", "b23.tv"})
