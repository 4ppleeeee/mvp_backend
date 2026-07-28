from app.ingestion.adapters.base import BaseVideoAdapter


class DouyinAdapter(BaseVideoAdapter):
    platform = "douyin"
    hosts = frozenset({"douyin.com", "www.douyin.com", "v.douyin.com", "tiktok.com", "www.tiktok.com"})
