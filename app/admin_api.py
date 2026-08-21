import http.client
import ipaddress
import re
import socket
import ssl
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, StrictInt
from sqlmodel import Session, select

from app.config import Settings
from app.ingestion.classifier import ResourceClassifier
from app.ingestion.input import extract_first_http_url
from app.ingestion.service import IngestionService
from app.models import IngestionJob, PoiCrawlRecord, SourceEvidence, TravelSource
from app.poi_sync import PoiSyncService


_PLATFORM_LABELS = {
    "youtube": "YouTube",
    "bilibili": "Bilibili",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "xiaoyuzhou": "小宇宙",
    "image": "图片资料",
}
_MEDIA_LABELS = {"video": "视频", "audio": "音频", "article": "网页", "image": "图片"}
_STATUS_LABELS = {"succeeded": "已完成", "failed": "失败", "running": "处理中", "queued": "等待中"}
_FAILURE_LABELS = {
    "caption": "字幕获取失败",
    "metadata": "元数据获取失败",
    "audio": "媒体处理失败",
    "video": "视频处理失败",
    "keyframe": "关键帧处理失败",
}
_MAX_COVER_BYTES = 5 * 1024 * 1024
_CRAWLAB_TIMEOUT = (3, 60)
_TENCENT_LOCATION_TIMEOUT = (3, 10)
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class UrlTaskRequest(BaseModel):
    url: str


class ReviewRequest(BaseModel):
    decision: str
    reason: str | None = None


class PoiLocationSuggestionRequest(BaseModel):
    keyword: str
    region: str | None = None


class PoiSearchRequest(BaseModel):
    query: str = Field(max_length=500)
    limit: StrictInt = 10


def create_admin_api_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/admin-api")

    @router.get("/tasks")
    def list_tasks(request: Request, limit: int = 20) -> dict[str, list[dict[str, object]]]:
        with Session(request.app.state.engine) as session:
            jobs = session.exec(select(IngestionJob).order_by(IngestionJob.created_at.desc()).limit(_bounded_limit(limit, 20))).all()
            source_titles = _source_titles(session, jobs)
            return {"tasks": [_task_payload(job, source_titles.get(job.source_id or "")) for job in jobs]}

    @router.post("/tasks/url", status_code=status.HTTP_202_ACCEPTED)
    def submit_url(request: Request, payload: UrlTaskRequest) -> dict[str, str]:
        descriptor = ResourceClassifier.default().classify_url(extract_first_http_url(payload.url))
        with Session(request.app.state.engine) as session:
            job = IngestionJob(
                input_type="url",
                original_url=descriptor.original_url,
                canonical_url=descriptor.canonical_url,
                source_platform=descriptor.source_platform,
                media_type=descriptor.media_type.value,
                max_attempts=request.app.state.settings.ingestion_max_attempts,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.job_id
            job_status = job.status
        if descriptor.media_type.value in {"video", "audio", "article"}:
            request.app.state.ingestion_executor.submit(request.app.state.run_ingestion, job_id)
        return {"job_id": job_id, "status": job_status}

    @router.post("/tasks/image", status_code=status.HTTP_202_ACCEPTED)
    async def submit_image(request: Request, file: UploadFile = File()) -> dict[str, str]:
        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="image upload required")
        with Session(request.app.state.engine) as session:
            job = IngestionJob(
                input_type="image",
                media_type="image",
                source_platform="image",
                max_attempts=request.app.state.settings.ingestion_max_attempts,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.job_id
            job_dir = Path(request.app.state.settings.ingestion_temp_dir) / job_id
            job_dir.mkdir(parents=True, exist_ok=False)
            suffix = Path(file.filename or "upload").suffix or ".bin"
            image_path = job_dir / f"image{suffix}"
            size = 0
            try:
                with image_path.open("wb") as output:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > request.app.state.settings.ingestion_max_upload_bytes:
                            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="image upload too large")
                        output.write(chunk)
            except Exception:
                image_path.unlink(missing_ok=True)
                job_dir.rmdir()
                session.delete(job)
                session.commit()
                raise
            job.input_path = str(image_path)
            session.add(job)
            session.commit()
            session.refresh(job)
            job_status = job.status
        request.app.state.ingestion_executor.submit(request.app.state.run_ingestion, job_id)
        return {"job_id": job_id, "status": job_status}

    @router.get("/tasks/{job_id}")
    def task_detail(request: Request, job_id: str) -> dict[str, object]:
        with Session(request.app.state.engine) as session:
            job = _get_job(session, job_id)
            source = _get_source(session, job.source_id) if job.source_id else None
            evidence = _get_evidence(session, job.source_id) if job.source_id else _job_evidence(job)
            return {
                "task": _task_payload(job, source.title if source else None),
                "source": _source_payload(source) if source else None,
                "evidence": _evidence_payload(evidence),
            }

    @router.post("/tasks/{job_id}/review")
    def review_task(request: Request, job_id: str, payload: ReviewRequest) -> dict[str, object]:
        with Session(request.app.state.engine) as session:
            _get_job(session, job_id)
            try:
                job = IngestionService(session=session, llm_client=object(), pipeline=object()).approve_review(
                    job_id,
                    decision=payload.decision,
                    reason=payload.reason,
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            source = _get_source(session, job.source_id) if job.source_id else None
            return {
                "task": _task_payload(job, source.title if source else None),
                "source": _source_payload(source) if source else None,
            }

    @router.get("/sources")
    def list_sources(request: Request, limit: int = 60) -> dict[str, list[dict[str, object]]]:
        with Session(request.app.state.engine) as session:
            sources = session.exec(select(TravelSource).order_by(TravelSource.created_at.desc()).limit(_bounded_limit(limit, 60))).all()
            return {"sources": [_source_payload(source) for source in sources]}

    @router.get("/sources/{source_id}")
    def source_detail(request: Request, source_id: str) -> dict[str, object]:
        with Session(request.app.state.engine) as session:
            source = _get_source(session, source_id)
            return {"source": _source_payload(source), "evidence": _evidence_payload(_get_evidence(session, source_id))}

    @router.get("/sources/{source_id}/cover")
    def source_cover(request: Request, source_id: str) -> Response:
        with Session(request.app.state.engine) as session:
            source = _get_source(session, source_id)
        if not source.cover_image_url:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source cover not found")
        return _fetch_safe_cover(source.cover_image_url)

    @router.post("/poi/location/suggest")
    def suggest_poi_locations(payload: PoiLocationSuggestionRequest) -> dict[str, object]:
        keyword = payload.keyword.strip()
        region = payload.region.strip() if payload.region else ""
        if not 1 <= len(keyword) <= 100:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="location keyword must be 1 to 100 characters")
        if len(region) > 100:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="location region must be at most 100 characters")
        if not settings.tencent_location_api_key or not settings.tencent_location_base_url:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Tencent location service is not configured")
        try:
            response = requests.get(
                f"{settings.tencent_location_base_url.rstrip('/')}/ws/place/v1/suggestion",
                params={"key": settings.tencent_location_api_key, "keyword": keyword, "region": region or None},
                timeout=_TENCENT_LOCATION_TIMEOUT,
            )
            response.raise_for_status()
            body = response.json()
        except requests.Timeout as exc:
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Tencent location service timed out") from exc
        except (requests.RequestException, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Tencent location service request failed") from exc
        if not isinstance(body, dict) or body.get("status") != 0:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Tencent location service request failed")
        candidates = body.get("data")
        return _poi_response(
            {
                "keyword": keyword,
                "candidates": [_normalize_poi(item) for item in candidates[:5]] if isinstance(candidates, list) else [],
            }
        )

    @router.post("/poi/crawls", status_code=status.HTTP_202_ACCEPTED)
    def submit_poi_crawl(request: Request, payload: dict[str, object]) -> dict[str, object]:
        remote = _crawlab_api(settings, "POST", "/poi-crawls", payload=payload)
        task = remote.get("data") if isinstance(remote, dict) and isinstance(remote.get("data"), dict) else remote
        crawl_task_id = str(task.get("crawlTaskId") or "") if isinstance(task, dict) else ""
        if not crawl_task_id:
            return _poi_response(task if isinstance(task, dict) else remote)
        poi = payload.get("poi") if isinstance(payload.get("poi"), dict) else {}
        poi_id = str(payload.get("poiId") or poi.get("poiId") or "").strip()
        if not poi_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="poiId is required")
        with Session(request.app.state.engine) as session:
            record = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).first()
            if record is None:
                source_urls = payload.get("sourceUrls") if isinstance(payload.get("sourceUrls"), list) else []
                record = PoiCrawlRecord(
                    crawl_task_id=crawl_task_id,
                    poi_id=poi_id,
                    poi_key=str(payload.get("poiKey") or f"tencent_map:{poi_id}"),
                    poi_name=str(poi.get("name") or "未命名地点"),
                    poi_json=poi,
                    source_urls=[str(url) for url in source_urls if isinstance(url, str)],
                    sync_status="queued",
                )
                session.add(record)
                session.commit()
                session.refresh(record)
            record_payload = _poi_record_payload(record)
        schedule = getattr(request.app.state, "schedule_poi_sync", None)
        if callable(schedule) and record_payload["syncStatus"] in {"queued", "crawling"}:
            schedule(crawl_task_id)
        task_payload = task if isinstance(task, dict) else {}
        return _poi_response({**task_payload, "localSync": record_payload})

    @router.get("/poi/crawls")
    def list_poi_crawls(
        poiKey: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, object]:
        if offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid pagination")
        params: dict[str, str | int] = {"offset": offset, "limit": limit}
        if poiKey:
            params["poiKey"] = poiKey
        if status_filter:
            params["status"] = status_filter
        return _poi_response(_crawlab_api(settings, "GET", "/poi-crawls", params=params))

    @router.get("/poi/crawls/{crawl_task_id}")
    def get_poi_crawl_status(request: Request, crawl_task_id: str) -> dict[str, object]:
        crawl_task_id = _task_identifier(crawl_task_id)
        remote = _crawlab_api(settings, "GET", f"/poi-crawls/{crawl_task_id}")
        payload = remote.get("data") if isinstance(remote, dict) and isinstance(remote.get("data"), dict) else remote
        if not isinstance(payload, dict):
            return _poi_response(remote)
        with Session(request.app.state.engine) as session:
            record = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).first()
            local_sync = _poi_record_payload(record) if record else None
        return _poi_response({**payload, **({"localSync": local_sync} if local_sync else {})})

    @router.post("/poi/crawls/{crawl_task_id}/sync", status_code=status.HTTP_202_ACCEPTED)
    def sync_poi_crawl(request: Request, crawl_task_id: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        crawl_task_id = _task_identifier(crawl_task_id)
        with Session(request.app.state.engine) as session:
            record = session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).first()
            if record is None:
                payload = payload or {}
                poi_id = str(payload.get("poiId") or "").strip()
                if not poi_id:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="poiId is required to register a historical crawl")
                poi = payload.get("poi") if isinstance(payload.get("poi"), dict) else {}
                source_urls = payload.get("sourceUrls") if isinstance(payload.get("sourceUrls"), list) else []
                record = PoiCrawlRecord(
                    crawl_task_id=crawl_task_id,
                    poi_id=poi_id,
                    poi_key=str(payload.get("poiKey") or f"tencent_map:{poi_id}"),
                    poi_name=str(poi.get("name") or "未命名地点"),
                    poi_json=poi,
                    source_urls=[str(url) for url in source_urls if isinstance(url, str)],
                    sync_status="queued",
                )
                session.add(record)
                session.commit()
                session.refresh(record)
            if record.sync_status in {"created", "creating"}:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="POI crawl cannot be safely rescheduled")
            record_payload = _poi_record_payload(record)
        schedule = getattr(request.app.state, "schedule_poi_sync", None)
        if not callable(schedule):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="POI crawl scheduling is not available")
        schedule(crawl_task_id)
        return _poi_response({"localSync": record_payload})

    @router.get("/poi/crawls/{crawl_task_id}/status", include_in_schema=False)
    def poi_crawl_status_alias(request: Request, crawl_task_id: str) -> dict[str, object]:
        return get_poi_crawl_status(request, crawl_task_id)

    @router.get("/poi/tasks/{native_task_id}/pages")
    def read_poi_pages(
        native_task_id: str,
        crawl_task_id: str = Query(alias="crawlTaskId"),
        offset: int = 0,
        limit: int = 5,
    ) -> dict[str, object]:
        if offset < 0 or not 1 <= limit <= 10:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid pagination")
        native_task_id = _task_identifier(native_task_id)
        _confirm_poi_native_task(settings, crawl_task_id, native_task_id)
        return _poi_response(
            _crawlab_api(
                settings,
                "GET",
                f"/tasks/{native_task_id}/pages",
                params={"offset": offset, "limit": limit},
            )
        )

    @router.post("/poi/tasks/{native_task_id}/search")
    def search_poi_pages(
        native_task_id: str,
        payload: PoiSearchRequest,
        crawl_task_id: str = Query(alias="crawlTaskId"),
    ) -> dict[str, object]:
        query = payload.query.strip()
        if not query:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="query must not be empty")
        if not 1 <= payload.limit <= 20:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="limit must be between 1 and 20")
        native_task_id = _task_identifier(native_task_id)
        _confirm_poi_native_task(settings, crawl_task_id, native_task_id)
        return _poi_response(
            _crawlab_api(
                settings,
                "POST",
                f"/tasks/{native_task_id}/search",
                payload={"query": query, "limit": payload.limit},
            )
        )

    @router.get("/poi/attractions")
    def list_attractions(
        cursor: str = "",
        direction: int = 0,
        page_size: int = Query(default=20, alias="pageSize"),
    ) -> dict[str, object]:
        if direction not in {-1, 0, 1} or not 1 <= page_size <= 50:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid attraction pagination")
        return _poi_response(_attraction_list_payload(
            _attraction_api(settings, "/attraction/batchGet", {"cursor": cursor, "direction": direction, "pageSize": page_size})
        ))

    @router.get("/poi/attractions/{attraction_id}")
    def get_attraction(attraction_id: str) -> dict[str, object]:
        return _poi_response(_attraction_detail_payload(
            _attraction_api(settings, "/attraction/get", {"attractionId": _task_identifier(attraction_id)})
        ))

    @router.post("/poi/attractions")
    def create_attraction(payload: dict[str, object]) -> dict[str, object]:
        poi_id = str(payload.get("poiId") or "").strip()
        attr_info = payload.get("attrInfo")
        if not poi_id or not isinstance(attr_info, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="poiId and attrInfo are required")
        return _poi_response(_attraction_api(settings, "/attraction/create", {"poiId": poi_id, "attrInfo": attr_info}))

    @router.post("/poi/attractions/{attraction_id}")
    def update_attraction(attraction_id: str, payload: dict[str, object]) -> dict[str, object]:
        attr_info = payload.get("attrInfo")
        if not isinstance(attr_info, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="attrInfo is required")
        body: dict[str, object] = {"attractionId": _task_identifier(attraction_id), "attrInfo": attr_info}
        base_info = payload.get("baseInfo")
        if isinstance(base_info, dict):
            body["baseInfo"] = base_info
        return _poi_response(_attraction_api(settings, "/attraction/update", body))

    return router


def create_poi_sync_service(*, settings: Settings, engine: object, llm_client: object) -> PoiSyncService:
    def generate_draft(*, poi: dict[str, object], pages: list[dict[str, object]]) -> object:
        method = getattr(llm_client, "generate_poi_draft", None)
        if not callable(method):
            raise RuntimeError("POI draft generation is unavailable")
        return method(poi=poi, pages=pages)

    return PoiSyncService(
        engine=engine,  # type: ignore[arg-type]
        get_crawl=lambda crawl_task_id: _crawlab_api(settings, "GET", f"/poi-crawls/{_task_identifier(crawl_task_id)}"),
        get_pages=lambda native_task_id, offset, limit: _crawlab_api(
            settings, "GET", f"/tasks/{_task_identifier(native_task_id)}/pages", params={"offset": offset, "limit": limit}
        ),
        generate_draft=generate_draft,
        create_attraction=lambda payload: _attraction_api(settings, "/attraction/create", payload),
    )


def _poi_response(payload: object) -> dict[str, object]:
    return {"ok": True, "data": payload}


def _poi_record_payload(record: PoiCrawlRecord) -> dict[str, object]:
    return {
        "crawlTaskId": record.crawl_task_id,
        "poiId": record.poi_id,
        "poiKey": record.poi_key,
        "attractionId": record.attraction_id,
        "syncStatus": record.sync_status,
        "syncError": record.sync_error,
        "draft": record.draft_json,
        "updatedAt": record.updated_at,
    }


def _attraction_list_payload(payload: object) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    items = source.get("items")
    return {
        "items": [_attraction_summary(item) for item in items] if isinstance(items, list) else [],
        "nextCursor": source.get("next_cursor", source.get("nextCursor", "")),
        "prevCursor": source.get("prev_cursor", source.get("prevCursor", "")),
        "totalCount": source.get("total_count", source.get("totalCount", 0)),
    }


def _attraction_detail_payload(payload: object) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    summary = _attraction_summary(source)
    attr_info = source.get("attr_info", source.get("attrInfo", {}))
    return {
        **summary,
        "attrInfo": attr_info if isinstance(attr_info, dict) else {},
        "baseInfo": {"status": summary["status"]} if summary["status"] is not None else {},
        "raw": source,
    }


def _attraction_summary(payload: object) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    attraction_id = source.get("attraction_id", source.get("attractionId", source.get("id", "")))
    poi_id = source.get("poi_id", source.get("poiId", ""))
    attr_info = source.get("attr_info", source.get("attrInfo", {}))
    return {
        "attractionId": str(attraction_id) if attraction_id is not None else "",
        "poiId": str(poi_id) if poi_id is not None else "",
        "name": source.get("name") or (attr_info.get("name") if isinstance(attr_info, dict) else "") or "",
        "status": source.get("status"),
    }


def _normalize_poi(item: object) -> dict[str, object]:
    source = item if isinstance(item, dict) else {}
    ad_info = source.get("ad_info") if isinstance(source.get("ad_info"), dict) else {}
    location = source.get("location") if isinstance(source.get("location"), dict) else {}
    poi_id = str(source.get("id") or "")
    return {
        "poiId": poi_id,
        "poiKey": f"tencent_map:{poi_id}",
        "name": source.get("title") or "",
        "address": source.get("address") or "",
        "province": ad_info.get("province") or "",
        "city": ad_info.get("city") or "",
        "district": ad_info.get("district") or "",
        "category": source.get("category") or "",
        "latitude": location.get("lat"),
        "longitude": location.get("lng"),
    }


def _task_identifier(task_id: str) -> str:
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid task identifier")
    return task_id


def _confirm_poi_native_task(settings: Settings, crawl_task_id: str, native_task_id: str) -> None:
    aggregate = _crawlab_api(settings, "GET", f"/poi-crawls/{_task_identifier(crawl_task_id)}")
    sources = aggregate.get("sources") if isinstance(aggregate, dict) else None
    if not isinstance(sources, list) or not any(
        isinstance(source, dict) and source.get("nativeTaskId") == native_task_id for source in sources
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="POI crawl source was not found")


def _crawlab_api(
    settings: Settings,
    method: str,
    path: str,
    *,
    payload: object | None = None,
    params: dict[str, str | int] | None = None,
) -> object:
    if not settings.crawlab_results_api_url or not settings.crawlab_api_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Crawlab API is not configured")
    kwargs: dict[str, object] = {
        "headers": {"Authorization": f"Bearer {settings.crawlab_api_token}"},
        "timeout": _CRAWLAB_TIMEOUT,
    }
    if payload is not None:
        kwargs["json"] = payload
    if params is not None:
        kwargs["params"] = params
    try:
        response = requests.request(method, f"{settings.crawlab_results_api_url.rstrip('/')}/api/v1{path}", **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.Timeout as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Crawlab API timed out") from exc
    except requests.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else status.HTTP_502_BAD_GATEWAY
        if 400 <= status_code < 500:
            raise HTTPException(status_code=status_code, detail=_crawlab_client_error_detail(response)) from exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Crawlab API request failed") from exc
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Crawlab API request failed") from exc


def _attraction_api(settings: Settings, path: str, payload: dict[str, object]) -> object:
    if not settings.attraction_api_base_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Attraction API is not configured")
    try:
        response = requests.request(
            "POST",
            f"{settings.attraction_api_base_url.rstrip('/')}{path}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=(3, 30),
        )
        response.raise_for_status()
        body = response.json()
    except requests.Timeout as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Attraction API timed out") from exc
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Attraction API request failed") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Attraction API request failed")
    if body.get("code") not in (None, 0):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Attraction API request failed")
    return body.get("data") if isinstance(body.get("data"), dict) else body


def _crawlab_client_error_detail(response: object) -> object:
    try:
        body = response.json()  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return "Crawlab API rejected request"
    if not isinstance(body, dict):
        return "Crawlab API rejected request"
    detail = body.get("detail", body)
    return detail if isinstance(detail, (dict, list)) else "Crawlab API rejected request"


def _bounded_limit(limit: int, default: int) -> int:
    return min(max(limit, 1), default)


def _get_job(session: Session, job_id: str) -> IngestionJob:
    job = session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).first()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ingestion not found")
    return job


def _get_source(session: Session, source_id: str | None) -> TravelSource | None:
    if not source_id:
        return None
    source = session.exec(select(TravelSource).where(TravelSource.source_id == source_id)).first()
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return source


def _get_evidence(session: Session, source_id: str) -> SourceEvidence | None:
    return session.exec(select(SourceEvidence).where(SourceEvidence.source_id == source_id)).first()


def _source_titles(session: Session, jobs: list[IngestionJob]) -> dict[str, str]:
    source_ids = [job.source_id for job in jobs if job.source_id]
    if not source_ids:
        return {}
    return {
        source.source_id: source.title
        for source in session.exec(select(TravelSource).where(TravelSource.source_id.in_(source_ids))).all()
    }


def _task_payload(job: IngestionJob, source_title: str | None = None) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "input_type": job.input_type,
        "original_url": job.original_url,
        "canonical_url": job.canonical_url,
        "source_platform": job.source_platform,
        "media_type": job.media_type,
        "status": job.status,
        "stage": job.stage,
        "source_id": job.source_id,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "ingest_decision": job.ingest_decision,
        "review_reason": job.review_reason,
        "created_at": job.created_at,
        "progress_percent": job.progress_percent,
        "progress_message": job.progress_message,
        "display": _task_display(job, source_title),
    }


def _source_payload(source: TravelSource) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "title": source.title,
        "body_text": source.body_text,
        "summary_text": source.summary_text,
        "original_url": source.original_url,
        "source_platform": source.source_platform,
        "cover_image_url": source.cover_image_url,
        "cover_proxy_url": f"/admin-api/sources/{source.source_id}/cover" if source.cover_image_url else None,
        "destination": source.destination,
        "category": source.category,
        "location_name": source.location_name,
        "normalized_tags": source.normalized_tags,
        "raw_tags": source.raw_tags,
        "created_at": source.created_at,
    }


def _job_evidence(job: IngestionJob) -> dict[str, object] | None:
    if not job.evidence_text:
        return None
    return {
        "origin": job.evidence_origin,
        "language": job.evidence_language,
        "full_text": job.evidence_text,
        "segments": job.evidence_segments,
        "metadata": job.evidence_metadata_json,
    }


def _evidence_payload(evidence: SourceEvidence | dict[str, object] | None) -> dict[str, object] | None:
    if evidence is None:
        return None
    if isinstance(evidence, dict):
        return evidence
    return {
        "evidence_id": evidence.evidence_id,
        "source_id": evidence.source_id,
        "kind": evidence.kind,
        "origin": evidence.origin,
        "language": evidence.language,
        "full_text": evidence.full_text,
        "segments": evidence.segments,
        "metadata": evidence.metadata_json,
        "created_at": evidence.created_at,
    }


def _task_display(job: IngestionJob, source_title: str | None = None) -> dict[str, str]:
    evidence_metadata = job.evidence_metadata_json or {}
    analysis = job.analysis_json or {}
    title = (
        _nonempty_text(analysis.get("title"))
        or _nonempty_text(evidence_metadata.get("title"))
        or _nonempty_text(source_title)
    )
    platform = _PLATFORM_LABELS.get(job.source_platform or "", job.source_platform or "网页")
    if not title:
        title = "图片资料" if job.input_type == "image" else _fallback_task_title(platform, job.original_url)
    metadata = f"{platform} · {_MEDIA_LABELS.get(job.media_type, job.media_type)} · {job.created_at.strftime('%m-%d %H:%M')}"
    failure = ""
    if job.status == "failed":
        failure = f"{_FAILURE_LABELS.get(job.failure_stage or '', '解析失败')} · {_brief_error(job.error_message)}"
    return {"title": title, "metadata": metadata, "failure": failure, "status": _STATUS_LABELS.get(job.status, job.status)}


def _fetch_safe_cover(cover_image_url: str) -> Response:
    current_url = cover_image_url
    for _ in range(6):
        try:
            upstream_status, upstream_headers, content = _read_pinned_cover_response(current_url)
        except (OSError, ValueError, ssl.SSLError, http.client.HTTPException) as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="cover image is unavailable") from exc
        if _is_redirect(upstream_status):
            location = upstream_headers.get("location")
            if not location:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="cover redirect is missing location")
            current_url = urljoin(current_url, location)
            continue
        content_type = upstream_headers.get("content-type", "").split(";", 1)[0].lower()
        if not 200 <= upstream_status < 300 or not content_type.startswith("image/"):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="cover image is unavailable")
        if len(content) > _MAX_COVER_BYTES:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="cover image exceeds 5 MiB")
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=300"},
        )
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="cover redirected too many times")


def _read_pinned_cover_response(url: str) -> tuple[int, dict[str, str], bytes]:
    parsed, address, port = _resolve_public_endpoint(url)
    raw_socket = socket.create_connection((address, port), timeout=10)
    connection: socket.socket | ssl.SSLSocket = raw_socket
    response: http.client.HTTPResponse | None = None
    try:
        if parsed.scheme == "https":
            connection = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=parsed.hostname)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {_host_header(parsed.hostname or '', port, parsed.scheme)}\r\n"
                "User-Agent: Mozilla/5.0 (compatible; TripGuard/0.1)\r\n"
                "Accept: image/avif,image/webp,image/*\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        response = http.client.HTTPResponse(connection)
        response.begin()
        content = response.read(_MAX_COVER_BYTES + 1)
        return response.status, {name.lower(): value for name, value in response.getheaders()}, content
    finally:
        if response is not None:
            response.close()
        connection.close()


def _resolve_public_endpoint(url: str) -> tuple[object, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("unsupported URL scheme")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("unable to resolve URL host") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("URL host is not publicly routable")
    return parsed, sorted(addresses)[0], port


def _host_header(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return host if port == default_port else f"{host}:{port}"


def _is_redirect(status_code: int) -> bool:
    return status_code in {301, 302, 303, 307, 308}


def _nonempty_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _fallback_task_title(platform: str, url: str | None) -> str:
    if not url:
        return f"{platform} 内容"
    parsed = urlsplit(url)
    identifier = parse_qs(parsed.query).get("v", [None])[0] or next((part for part in reversed(parsed.path.split("/")) if part), None)
    if not identifier:
        return f"{platform} 内容"
    return f"{platform} · {identifier[:14]}{'…' if len(identifier) > 14 else ''}"


def _brief_error(message: str | None) -> str:
    if not message:
        return "请查看详情"
    if "Fresh cookies" in message or "cookies are needed" in message:
        return "平台拒绝当前请求"
    return " ".join(message.split())[:72]
