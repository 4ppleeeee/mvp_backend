from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.models import PoiCrawlRecord


class PoiSyncService:
    def __init__(
        self,
        *,
        engine: Engine,
        get_crawl: Callable[[str], object],
        get_pages: Callable[[str, int, int], object],
        generate_draft: Callable[..., object],
        create_attraction: Callable[[dict[str, object]], object],
    ) -> None:
        self._engine = engine
        self._get_crawl = get_crawl
        self._get_pages = get_pages
        self._generate_draft = generate_draft
        self._create_attraction = create_attraction

    def run(self, crawl_task_id: str) -> str:
        record = self._record(crawl_task_id)
        if record.sync_status == "created":
            return "created"
        if record.sync_status == "creating":
            return "creating"
        crawl = _mapping(self._get_crawl(crawl_task_id))
        sources = crawl.get("sources") if isinstance(crawl.get("sources"), list) else []
        readable_pages = self._read_pages(sources)
        if not readable_pages:
            if str(crawl.get("status") or "").lower() in {"queued", "running", "crawling", "pending"}:
                self._save_status(crawl_task_id, "crawling", None)
                return "crawling"
            self._save_failed(crawl_task_id, "Crawlab did not produce readable pages")
            return "failed"
        try:
            draft = _mapping(_resolve(self._generate_draft(poi=record.poi_json, pages=readable_pages)))
        except Exception as exc:
            self._save_failed(crawl_task_id, str(exc) or exc.__class__.__name__)
            return "failed"
        draft.update({"poi_id": record.poi_id, "poi_key": record.poi_key, "name": record.poi_name})
        self._save_creating(crawl_task_id, draft)
        try:
            created = _mapping(self._create_attraction({"poiId": record.poi_id, "attrInfo": _attr_info(draft, record)}))
        except Exception as exc:
            self._save_status(crawl_task_id, "creating", f"Attraction create outcome is unknown: {exc}")
            return "creating"
        attraction_id = _attraction_id(created)
        if attraction_id is None:
            self._save_status(crawl_task_id, "creating", "Attraction create response did not include an attraction ID")
            return "creating"
        with Session(self._engine) as session:
            current = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).one()
            current.sync_status = "created"
            current.attraction_id = attraction_id
            current.draft_json = draft
            current.sync_error = None
            current.updated_at = datetime.now(timezone.utc)
            session.add(current)
            session.commit()
        return "created"

    def pending_crawl_ids(self) -> list[str]:
        with Session(self._engine) as session:
            records = session.exec(
                select(PoiCrawlRecord)
                .where(PoiCrawlRecord.sync_status.in_(("queued", "crawling")))
                .order_by(PoiCrawlRecord.created_at)
            ).all()
        return [record.crawl_task_id for record in records]

    def _read_pages(self, sources: list[object]) -> list[dict[str, object]]:
        pages: list[dict[str, object]] = []
        for source in sources:
            native_task_id = source.get("nativeTaskId") if isinstance(source, dict) else None
            if not isinstance(native_task_id, str) or not native_task_id:
                continue
            offset = 0
            while True:
                batch = _mapping(self._get_pages(native_task_id, offset, 10)).get("pages")
                if not isinstance(batch, list):
                    break
                pages.extend(page for page in batch if isinstance(page, dict) and page.get("markdown"))
                if len(batch) < 10:
                    break
                offset += len(batch)
        return pages

    def _save_failed(self, crawl_task_id: str, error: str) -> None:
        self._save_status(crawl_task_id, "failed", error)

    def _save_creating(self, crawl_task_id: str, draft: dict[str, object]) -> None:
        with Session(self._engine) as session:
            current = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).one()
            current.sync_status = "creating"
            current.draft_json = draft
            current.sync_error = None
            current.updated_at = datetime.now(timezone.utc)
            session.add(current)
            session.commit()

    def _save_status(self, crawl_task_id: str, sync_status: str, sync_error: str | None) -> None:
        with Session(self._engine) as session:
            current = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).one()
            current.sync_status = sync_status
            current.sync_error = sync_error
            current.updated_at = datetime.now(timezone.utc)
            session.add(current)
            session.commit()

    def _record(self, crawl_task_id: str) -> PoiCrawlRecord:
        with Session(self._engine) as session:
            return session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).one()


def _attr_info(draft: dict[str, object], record: PoiCrawlRecord) -> dict[str, object]:
    info = {key: draft[key] for key in ("description", "tags") if draft.get(key) is not None}
    info["name"] = record.poi_name
    info["cityName"] = str(record.poi_json.get("city") or "")
    info["countryName"] = "中国"
    info["currencyCode"] = "CNY"
    return info


def _mapping(value: object) -> dict[str, object]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return value if isinstance(value, dict) else {}


def _attraction_id(payload: dict[str, object]) -> str | None:
    for key in ("attractionId", "attraction_id", "id"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _resolve(value: object) -> object:
    return asyncio.run(value) if inspect.isawaitable(value) else value
