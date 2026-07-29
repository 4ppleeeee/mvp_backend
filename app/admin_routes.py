from pathlib import Path
from urllib.parse import urljoin, urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
import requests

from app.admin_auth import AdminAuthenticator
from app.config import Settings
from app.ingestion.classifier import ResourceClassifier
from app.ingestion.input import extract_first_http_url
from app.ingestion.article import SafeHtmlFetcher
from app.ingestion.service import IngestionService
from app.models import IngestionJob, SourceEvidence, TravelSource


def create_admin_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/admin")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "admin"))
    authenticator = AdminAuthenticator(settings)

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
        return templates.TemplateResponse(request, "dashboard.html", {"jobs": jobs})

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
                IngestionService(session=session, llm_client=object(), pipeline=object()).approve_review(
                    job_id, decision=decision, reviewer=request.session.get("admin_username"), reason=reason
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
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
