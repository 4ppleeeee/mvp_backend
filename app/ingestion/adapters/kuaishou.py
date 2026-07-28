from app.ingestion.adapters.base import BaseVideoAdapter


class KuaishouAdapter(BaseVideoAdapter):
    platform = "kuaishou"
    hosts = frozenset({"kuaishou.com", "www.kuaishou.com", "v.kuaishou.com"})
