from app.ingestion.adapters.base import BaseVideoAdapter


class YoutubeAdapter(BaseVideoAdapter):
    platform = "youtube"
    hosts = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
