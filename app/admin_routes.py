from pathlib import Path
import asyncio
import json
from urllib.parse import parse_qs, urljoin, urlsplit

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
import requests

from app.admin_auth import AdminAuthenticator
from app.config import Settings
from app.llm import normalize_analysis
from app.ingestion.classifier import ResourceClassifier
from app.ingestion.input import extract_first_http_url
from app.ingestion.article import SafeHtmlFetcher
from app.ingestion.service import IngestionService
from app.models import IngestionJob, SourceEvidence, TravelSource


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


def create_admin_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/admin")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "admin"))
    authenticator = AdminAuthenticator(settings)

    def task_display(job: IngestionJob, source_title: str | None = None) -> dict[str, str]:
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
            failure_label = _FAILURE_LABELS.get(job.failure_stage or "", "解析失败")
            failure = f"{failure_label} · {_brief_error(job.error_message)}"
        return {
            "title": title,
            "metadata": metadata,
            "failure": failure,
            "status": _STATUS_LABELS.get(job.status, job.status),
        }

    def ensure_configured() -> None:
        if not authenticator.configured:
            raise HTTPException(status_code=503, detail="admin authentication is not configured")

    def is_logged_in(request: Request) -> bool:
        ensure_configured()
        try:
            authenticator.require(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return False
            raise
        return True

    def ensure_logged_in(request: Request) -> None:
        if not is_logged_in(request):
            raise HTTPException(status_code=401, detail="admin login required")

    def display_cover_url(source: TravelSource) -> str | None:
        if not source.cover_image_url:
            return None
        parsed = urlsplit(source.cover_image_url)
        if (
            source.source_platform == "youtube"
            and parsed.scheme == "https"
            and parsed.hostname in {"i.ytimg.com", "i3.ytimg.com"}
        ):
            return source.cover_image_url
        return f"/admin/sources/{source.source_id}/cover"

    def crawlab_tasks() -> list[dict[str, object]]:
        try:
            response = requests.get(f"{settings.crawlab_results_api_url.rstrip('/')}/api/v1/tasks", timeout=(3, 15))
            response.raise_for_status()
            tasks = response.json().get("tasks", [])
        except (requests.RequestException, ValueError):
            return []
        return [task for task in tasks if isinstance(task, dict) and isinstance(task.get("task_id"), str)]

    @router.get("/login")
    def login_form(request: Request):
        ensure_configured()
        return templates.TemplateResponse(request, "login.html", {"error": request.query_params.get("error") == "1"})

    @router.post("/login")
    def login(request: Request, username: str = Form(), password: str = Form()):
        ensure_configured()
        if not authenticator.verify(username, password):
            return RedirectResponse("/admin/login?error=1", status_code=status.HTTP_303_SEE_OTHER)
        request.session["admin_username"] = username
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("")
    @router.get("/")
    def dashboard(request: Request):
        if not is_logged_in(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        with Session(request.app.state.engine) as session:
            jobs = session.exec(select(IngestionJob).order_by(IngestionJob.created_at.desc()).limit(20)).all()
            source_ids = [job.source_id for job in jobs if job.source_id]
            source_titles = {
                source.source_id: source.title
                for source in session.exec(select(TravelSource).where(TravelSource.source_id.in_(source_ids))).all()
            } if source_ids else {}
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "jobs": jobs,
                "task_displays": {
                    job.job_id: task_display(job, source_titles.get(job.source_id or ""))
                    for job in jobs
                },
            },
        )

    @router.get("/crawlab")
    def crawlab_results(request: Request):
        ensure_logged_in(request)
        tasks = crawlab_tasks()
        return templates.TemplateResponse(
            request,
            "crawlab.html",
            {
                "tasks": tasks,
                "page_count": sum(int(task.get("page_count", 0)) for task in tasks),
                "sync": request.app.state.crawlab_sync,
            },
        )

    def sync_all_crawlab(app: FastAPI) -> None:
        sync = app.state.crawlab_sync
        sync.update(status="running", processed=0, synced=0, skipped=0, message="正在读取 Crawlab 抓取结果…")
        try:
            for task in crawlab_tasks():
                task_id = task["task_id"]
                response = requests.get(
                    f"{settings.crawlab_results_api_url.rstrip('/')}/api/v1/tasks/{task_id}/data.jsonl",
                    timeout=(3, 60), stream=True,
                )
                response.raise_for_status()
                for page_index, raw_line in enumerate(response.iter_lines(decode_unicode=True)):
                    if not raw_line:
                        continue
                    sync["processed"] += 1
                    try:
                        page = json.loads(raw_line)
                    except json.JSONDecodeError:
                        sync["skipped"] += 1
                        continue
                    if not isinstance(page, dict) or not isinstance(page.get("markdown"), str) or not page["markdown"].strip():
                        sync["skipped"] += 1
                        continue
                    page_url = page.get("url") if isinstance(page.get("url"), str) else f"crawlab://{task_id}/{page_index}"
                    with Session(app.state.engine) as session:
                        existing = session.exec(select(TravelSource).where(TravelSource.original_url == page_url)).first()
                        evidence = (
                            session.exec(
                                select(SourceEvidence)
                                .where(SourceEvidence.source_id == existing.source_id)
                                .order_by(SourceEvidence.created_at.desc())
                            ).first()
                            if existing is not None
                            else None
                        )
                        if existing is not None:
                            if evidence is not None and evidence.metadata_json.get("rag_synced") is True:
                                sync["skipped"] += 1
                                continue
                            sync_rag_source = getattr(app.state, "sync_rag_source", None)
                            if callable(sync_rag_source) and sync_rag_source(existing.source_id):
                                if evidence is not None:
                                    evidence.metadata_json = {**evidence.metadata_json, "rag_synced": True}
                                    session.add(evidence)
                                    session.commit()
                                sync["synced"] += 1
                            else:
                                sync["skipped"] += 1
                            continue
                        analysis = normalize_analysis(asyncio.run(app.state.llm_client.analyze_source(
                            title=page.get("title") if isinstance(page.get("title"), str) else page_url,
                            body_text=page["markdown"], url=page_url, source_platform="crawlab",
                        )))
                        if not analysis.is_travel_related:
                            sync["skipped"] += 1
                            continue
                        source = TravelSource(
                            title=analysis.title or page.get("title") or page_url, body_text=analysis.body_text or page["markdown"],
                            original_url=page_url, source_platform="crawlab", destination=analysis.destination, category=analysis.category,
                            location_name=analysis.location_name, normalized_tags=analysis.normalized_tags, raw_tags=analysis.raw_tags,
                        )
                        session.add(source); session.flush()
                        evidence = SourceEvidence(source_id=source.source_id, kind="crawlab_page", origin="crawlab", full_text=page["markdown"], metadata_json={"crawlab_task_id": task_id, "page_file": page.get("file")})
                        session.add(evidence)
                        session.commit()
                        sync_rag_source = getattr(app.state, "sync_rag_source", None)
                        if callable(sync_rag_source) and sync_rag_source(source.source_id):
                            evidence.metadata_json = {**evidence.metadata_json, "rag_synced": True}
                            session.add(evidence)
                            session.commit()
                            sync["synced"] += 1
                        else:
                            sync["skipped"] += 1
                response.close()
            sync.update(status="succeeded", message="同步完成")
        except Exception as exc:
            sync.update(status="failed", message=f"同步失败：{_brief_error(str(exc))}")

    @router.post("/crawlab/sync")
    def sync_crawlab(request: Request):
        ensure_logged_in(request)
        sync = request.app.state.crawlab_sync
        if sync["status"] not in {"queued", "running"}:
            sync.update(status="queued", message="等待同步任务开始…")
            request.app.state.ingestion_executor.submit(sync_all_crawlab, request.app)
        return RedirectResponse("/admin/crawlab", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/sources")
    def sources(request: Request, source_id: str | None = None):
        ensure_logged_in(request)
        with Session(request.app.state.engine) as session:
            items = session.exec(select(TravelSource).order_by(TravelSource.created_at.desc()).limit(60)).all()
            selected = next((item for item in items if item.source_id == source_id), items[0] if items else None)
            evidence = session.exec(select(SourceEvidence).where(SourceEvidence.source_id == selected.source_id)).first() if selected else None
        return templates.TemplateResponse(
            request,
            "sources.html",
            {"sources": items, "selected": selected, "evidence": evidence, "display_cover_url": display_cover_url},
        )

    @router.get("/sources/{source_id}")
    def source_detail(request: Request, source_id: str):
        ensure_logged_in(request)
        with Session(request.app.state.engine) as session:
            source = session.exec(select(TravelSource).where(TravelSource.source_id == source_id)).first()
            if source is None:
                raise HTTPException(status_code=404, detail="source not found")
            evidence = session.exec(select(SourceEvidence).where(SourceEvidence.source_id == source_id)).first()
        return templates.TemplateResponse(
            request,
            "source_detail.html",
            {"source": source, "evidence": evidence, "display_cover_url": display_cover_url},
        )

    @router.get("/sources/{source_id}/cover")
    def source_cover(request: Request, source_id: str):
        ensure_logged_in(request)
        with Session(request.app.state.engine) as session:
            source = session.exec(select(TravelSource).where(TravelSource.source_id == source_id)).first()
        if source is None or not source.cover_image_url:
            raise HTTPException(status_code=404, detail="source cover not found")
        current_url = source.cover_image_url
        for _ in range(6):
            SafeHtmlFetcher._require_public_url(current_url)
            upstream = requests.get(
                current_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; TripGuard/0.1)", "Accept": "image/avif,image/webp,image/*"},
                timeout=(10, 20),
                allow_redirects=False,
                stream=True,
            )
            try:
                if upstream.is_redirect:
                    location = upstream.headers.get("Location")
                    if not location:
                        raise HTTPException(status_code=502, detail="cover redirect is missing location")
                    current_url = urljoin(current_url, location)
                    continue
                content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if not 200 <= upstream.status_code < 300 or not content_type.startswith("image/"):
                    raise HTTPException(status_code=502, detail="cover image is unavailable")
                chunks: list[bytes] = []
                total = 0
                for chunk in upstream.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > 5 * 1024 * 1024:
                        raise HTTPException(status_code=502, detail="cover image exceeds 5 MiB")
                    chunks.append(chunk)
                return Response(
                    content=b"".join(chunks),
                    media_type=content_type,
                    headers={"Cache-Control": "private, max-age=300"},
                )
            finally:
                upstream.close()
        raise HTTPException(status_code=502, detail="cover redirected too many times")

    @router.post("/ingestions/url")
    def submit_url(request: Request, url: str = Form()):
        ensure_logged_in(request)
        descriptor = ResourceClassifier.default().classify_url(extract_first_http_url(url))
        with Session(request.app.state.engine) as session:
            job = IngestionJob(
                input_type="url", original_url=descriptor.original_url, canonical_url=descriptor.canonical_url,
                source_platform=descriptor.source_platform, media_type=descriptor.media_type.value,
                max_attempts=request.app.state.settings.ingestion_max_attempts,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            job_id = job.job_id
        if descriptor.media_type.value in {"video", "audio", "article"}:
            request.app.state.ingestion_executor.submit(request.app.state.run_ingestion, job_id)
        return RedirectResponse(f"/admin/ingestions/{job_id}", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/ingestions/image")
    async def submit_image(request: Request, file: UploadFile = File()):
        ensure_logged_in(request)
        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(status_code=415, detail="image upload required")
        with Session(request.app.state.engine) as session:
            job = IngestionJob(input_type="image", media_type="image", source_platform="image")
            session.add(job); session.commit(); session.refresh(job)
            job_id = job.job_id
            path = Path(request.app.state.settings.ingestion_temp_dir) / job_id / "image.bin"
            path.parent.mkdir(parents=True, exist_ok=False)
            path.write_bytes(await file.read())
            job.input_path = str(path)
            session.add(job); session.commit()
        request.app.state.ingestion_executor.submit(request.app.state.run_ingestion, job_id)
        return RedirectResponse(f"/admin/ingestions/{job_id}", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/ingestions/{job_id}")
    def job_detail(request: Request, job_id: str):
        ensure_logged_in(request)
        return templates.TemplateResponse(request, "job.html", {"job_id": job_id})

    @router.post("/ingestions/{job_id}/review")
    def review_ingestion(request: Request, job_id: str, decision: str = Form(), reason: str | None = Form(default=None)):
        ensure_logged_in(request)
        with Session(request.app.state.engine) as session:
            try:
                job = IngestionService(session=session, llm_client=object(), pipeline=object()).approve_review(
                    job_id, decision=decision, reviewer=request.session.get("admin_username"), reason=reason
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            sync_rag_source = getattr(request.app.state, "sync_rag_source", None)
            if job.source_id and callable(sync_rag_source):
                sync_rag_source(session, job.source_id)
        return RedirectResponse(f"/admin/ingestions/{job_id}", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/ingestions/{job_id}/fragment")
    def job_fragment(request: Request, job_id: str):
        ensure_logged_in(request)
        with Session(request.app.state.engine) as session:
            job = session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).first()
            if job is None:
                raise HTTPException(status_code=404, detail="ingestion not found")
            source = session.exec(select(TravelSource).where(TravelSource.source_id == job.source_id)).first() if job.source_id else None
            evidence = session.exec(select(SourceEvidence).where(SourceEvidence.source_id == job.source_id)).first() if job.source_id else None
        return templates.TemplateResponse(request, "job_fragment.html", {"job": job, "source": source, "evidence": evidence})

    return router


def _nonempty_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _fallback_task_title(platform: str, url: str | None) -> str:
    if not url:
        return f"{platform} 内容"
    parsed = urlsplit(url)
    identifier = parse_qs(parsed.query).get("v", [None])[0] or next(
        (part for part in reversed(parsed.path.split("/")) if part),
        None,
    )
    if not identifier:
        return f"{platform} 内容"
    return f"{platform} · {identifier[:14]}{'…' if len(identifier) > 14 else ''}"


def _brief_error(message: str | None) -> str:
    if not message:
        return "请查看详情"
    if "Fresh cookies" in message or "cookies are needed" in message:
        return "平台拒绝当前请求"
    return " ".join(message.split())[:72]
