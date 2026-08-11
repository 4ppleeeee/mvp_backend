from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.admin_auth import AdminAuthenticator
from app.config import Settings
from app.models import PoiCrawlRecord
from app.poi_integrations import (
    AttractionClient,
    CrawlabClient,
    PoiIntegrationError,
    TencentLocationClient,
)


def create_poi_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/admin/poi")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "admin"))
    authenticator = AdminAuthenticator(settings)

    def ensure_logged_in(request: Request) -> None:
        if not authenticator.configured:
            raise HTTPException(503, detail="admin authentication is not configured")
        try:
            authenticator.require(request)
        except HTTPException as exc:
            raise HTTPException(401, detail="admin login required") from exc

    def response(data: object) -> dict[str, object]:
        return {"ok": True, "data": data}

    def integration_error(exc: PoiIntegrationError) -> HTTPException:
        return HTTPException(502, detail=str(exc))

    @router.get("")
    def poi_console(request: Request):
        if not authenticator.configured:
            raise HTTPException(503, detail="admin authentication is not configured")
        try:
            authenticator.require(request)
        except HTTPException:
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request, "poi.html", {})

    @router.get("/api/health")
    def health(request: Request):
        ensure_logged_in(request)
        return response(
            {
                "locationConfigured": bool(settings.tencent_location_api_key),
                "crawlabConfigured": bool(settings.crawlab_api_token),
                "attractionConfigured": bool(settings.attraction_api_base_url),
            }
        )

    @router.post("/api/location/suggest")
    async def location_suggest(request: Request):
        ensure_logged_in(request)
        payload = await request.json()
        keyword = str(payload.get("keyword") or "").strip()
        region = str(payload.get("region") or "").strip()
        if not 1 <= len(keyword) <= 100:
            raise HTTPException(400, detail="请输入 1–100 个字符的地点名称")
        try:
            candidates = TencentLocationClient(settings).suggest(keyword, region)
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc
        return response({"keyword": keyword, "candidates": candidates})

    @router.post("/api/crawls")
    async def submit_crawl(request: Request):
        ensure_logged_in(request)
        payload = await request.json()
        try:
            result = CrawlabClient(settings).call("POST", "/poi-crawls", payload=payload)
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc
        task = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
        task = task if isinstance(task, dict) else {}
        crawl_task_id = str(task.get("crawlTaskId") or task.get("task_id") or task.get("taskId") or "")
        poi_id = str(payload.get("poiId") or "")
        if crawl_task_id and poi_id:
            with Session(request.app.state.engine) as session:
                record = PoiCrawlRecord(
                    crawl_task_id=crawl_task_id,
                    poi_id=poi_id,
                    poi_key=str(payload.get("poiKey") or f"tencent_map:{poi_id}"),
                    poi_name=str((payload.get("poi") or {}).get("name") or "未命名地点"),
                    poi_json=payload.get("poi") if isinstance(payload.get("poi"), dict) else {},
                    source_urls=payload.get("sourceUrls") if isinstance(payload.get("sourceUrls"), list) else [],
                )
                session.add(record)
                session.commit()
        return response(result)

    @router.get("/api/crawls")
    def list_crawls(request: Request):
        ensure_logged_in(request)
        try:
            return response(CrawlabClient(settings).call("GET", "/poi-crawls"))
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc

    @router.get("/api/crawls/{crawl_task_id}/status")
    def crawl_status(request: Request, crawl_task_id: str):
        ensure_logged_in(request)
        try:
            return response(CrawlabClient(settings).call("GET", f"/poi-crawls/{crawl_task_id}"))
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc

    @router.get("/api/tasks/{native_task_id}/pages")
    def native_pages(request: Request, native_task_id: str, offset: int = 0, limit: int = 20):
        ensure_logged_in(request)
        try:
            return response(CrawlabClient(settings).call("GET", f"/tasks/{native_task_id}/pages", params={"offset": offset, "pageSize": limit}))
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc

    @router.post("/api/tasks/{native_task_id}/search")
    async def native_search(request: Request, native_task_id: str):
        ensure_logged_in(request)
        try:
            return response(CrawlabClient(settings).call("POST", f"/tasks/{native_task_id}/search", payload=await request.json()))
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc

    @router.post("/api/crawls/{crawl_task_id}/draft")
    def generate_draft(request: Request, crawl_task_id: str):
        ensure_logged_in(request)
        with Session(request.app.state.engine) as session:
            record = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).first()
        if record is None:
            raise HTTPException(404, detail="抓取任务关联的 POI 不存在")
        try:
            status_payload = CrawlabClient(settings).call("GET", f"/poi-crawls/{crawl_task_id}")
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc
        status_data = status_payload.get("data") if isinstance(status_payload, dict) and isinstance(status_payload.get("data"), dict) else status_payload
        status_data = status_data if isinstance(status_data, dict) else {}
        sources = [item for item in status_data.get("sources", []) if isinstance(item, dict) and item.get("nativeTaskId")]
        pages: list[dict[str, object]] = []
        try:
            for source in sources:
                if source.get("status") not in {"succeeded", "partially_succeeded"}:
                    continue
                batch = CrawlabClient(settings).call("GET", f"/tasks/{source['nativeTaskId']}/pages", params={"offset": 0, "pageSize": 100})
                batch = batch.get("data") if isinstance(batch, dict) and isinstance(batch.get("data"), dict) else batch
                if isinstance(batch, dict):
                    pages.extend(item for item in batch.get("pages", []) if isinstance(item, dict))
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc
        llm_client = request.app.state.llm_client
        if not hasattr(llm_client, "generate_poi_draft"):
            raise HTTPException(503, detail="当前模型服务不支持景点初稿生成")
        draft = _run_async(llm_client.generate_poi_draft(poi=record.poi_json, pages=pages))
        draft_data = draft.model_dump(mode="json") if hasattr(draft, "model_dump") else draft
        draft_data = draft_data if isinstance(draft_data, dict) else {}
        draft_data.update(
            {
                "poi_id": record.poi_id,
                "poi_key": record.poi_key,
                "name": record.poi_name,
                "evidence": {"crawl_task_id": crawl_task_id, "loaded_pages": len(pages)},
            }
        )
        with Session(request.app.state.engine) as session:
            current = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).one()
            current.draft_json = draft_data
            current.sync_status = "draft"
            current.updated_at = datetime.now(timezone.utc)
            session.add(current)
            session.commit()
        return response({"draft": draft_data, "pages": pages, "status": status_data})

    @router.post("/api/crawls/{crawl_task_id}/create")
    async def create_attraction(request: Request, crawl_task_id: str):
        ensure_logged_in(request)
        payload = await request.json()
        with Session(request.app.state.engine) as session:
            record = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).first()
        if record is None:
            raise HTTPException(404, detail="抓取任务关联的 POI 不存在")
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else payload
        attr_info = _draft_to_attr_info(draft, record)
        try:
            result = AttractionClient(settings).create(poi_id=record.poi_id, attr_info=attr_info)
        except PoiIntegrationError as exc:
            with Session(request.app.state.engine) as session:
                current = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).one()
                current.sync_status = "failed"
                current.sync_error = str(exc)
                current.updated_at = datetime.now(timezone.utc)
                session.add(current)
                session.commit()
            raise integration_error(exc) from exc
        attraction_id = _extract_attraction_id(result)
        with Session(request.app.state.engine) as session:
            current = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).one()
            current.attraction_id = attraction_id
            current.draft_json = draft
            current.sync_status = "created"
            current.sync_error = None
            current.updated_at = datetime.now(timezone.utc)
            session.add(current)
            session.commit()
        return response({"attraction": result, "attractionId": attraction_id, "attrInfo": attr_info})

    @router.get("/api/attractions")
    def batch_attractions(request: Request, cursor: str = "", direction: int = 0, page_size: int = 10):
        ensure_logged_in(request)
        try:
            return response(AttractionClient(settings).batch_get(cursor=cursor, direction=direction, page_size=max(1, min(page_size, 50))))
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc

    @router.get("/api/attractions/{attraction_id}")
    def get_attraction(request: Request, attraction_id: str):
        ensure_logged_in(request)
        try:
            remote = AttractionClient(settings).get(attraction_id)
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc
        poi_id = _extract_poi_id(remote)
        with Session(request.app.state.engine) as session:
            record = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.attraction_id == attraction_id)).first()
            if record is None and poi_id:
                record = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.poi_id == poi_id)).first()
        return response({"attraction": remote, "local": _record_data(record) if record else None})

    @router.post("/api/attractions/{attraction_id}/update")
    async def update_attraction(request: Request, attraction_id: str):
        ensure_logged_in(request)
        payload = await request.json()
        attr_info = payload.get("attrInfo") if isinstance(payload.get("attrInfo"), dict) else payload
        base_info = payload.get("baseInfo") if isinstance(payload.get("baseInfo"), dict) else {}
        try:
            result = AttractionClient(settings).update(attraction_id=attraction_id, attr_info=attr_info, status=base_info.get("status"))
        except PoiIntegrationError as exc:
            raise integration_error(exc) from exc
        return response(result)

    return router


def _run_async(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)


def _draft_to_attr_info(draft: dict[str, Any], record: PoiCrawlRecord) -> dict[str, Any]:
    mapping = {
        "city_name": "cityName",
        "country_name": "countryName",
        "cover_image_url": "coverImageUrl",
        "name_en": "nameEn",
        "opening_time": "openingTime",
        "closing_time": "closingTime",
        "is_free": "isFree",
        "ticket_price": "ticketPrice",
        "currency_code": "currencyCode",
        "recommended_visit_duration": "recommendedVisitDuration",
        "best_season": "bestSeason",
        "local_tip": "localTip",
        "transportation": "transportation",
        "description": "description",
        "rating": "rating",
        "tags": "tags",
        "name": "name",
    }
    info: dict[str, Any] = {target: draft.get(source) for source, target in mapping.items()}
    info["name"] = info.get("name") or record.poi_name
    info["cityName"] = info.get("cityName") or record.poi_json.get("city") or record.poi_json.get("province") or ""
    info["countryName"] = info.get("countryName") or "中国"
    info["currencyCode"] = info.get("currencyCode") or "CNY"
    info["tags"] = info.get("tags") or []
    for key in ("coverImageUrl", "description", "bestSeason", "recommendedVisitDuration", "transportation", "localTip", "nameEn", "openingTime", "closingTime"):
        info[key] = info.get(key) or ""
    for key in ("rating", "ticketPrice"):
        info[key] = info.get(key) if info.get(key) is not None else 0
    info["isFree"] = info.get("isFree") if info.get("isFree") is not None else 0
    return info


def _extract_attraction_id(data: dict[str, Any]) -> str | None:
    for key in ("attractionId", "attraction_id", "id"):
        if data.get(key):
            return str(data[key])
    return None


def _extract_poi_id(data: dict[str, Any]) -> str | None:
    for key in ("poiId", "poi_id"):
        if data.get(key):
            return str(data[key])
    return None


def _record_data(record: PoiCrawlRecord) -> dict[str, Any]:
    return {
        "crawlTaskId": record.crawl_task_id,
        "poiId": record.poi_id,
        "poiKey": record.poi_key,
        "attractionId": record.attraction_id,
        "syncStatus": record.sync_status,
        "draft": record.draft_json,
    }
