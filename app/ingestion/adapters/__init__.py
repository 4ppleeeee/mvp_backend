from app.ingestion.adapters.bilibili import BilibiliAdapter
from app.ingestion.adapters.douyin import DouyinAdapter
from app.ingestion.adapters.kuaishou import KuaishouAdapter
from app.ingestion.adapters.youtube import YoutubeAdapter
from app.ingestion.media import MediaEgressPolicy


def default_video_adapters(media_egress_policy: MediaEgressPolicy | None = None) -> tuple[object, ...]:
    return (
        YoutubeAdapter(media_egress_policy=media_egress_policy),
        BilibiliAdapter(),
        DouyinAdapter(),
        KuaishouAdapter(),
    )
