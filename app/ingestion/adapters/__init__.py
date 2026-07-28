from app.ingestion.adapters.bilibili import BilibiliAdapter
from app.ingestion.adapters.douyin import DouyinAdapter
from app.ingestion.adapters.kuaishou import KuaishouAdapter
from app.ingestion.adapters.youtube import YoutubeAdapter


def default_video_adapters() -> tuple[object, ...]:
    return (YoutubeAdapter(), BilibiliAdapter(), DouyinAdapter(), KuaishouAdapter())
