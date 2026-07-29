from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.admin_auth import AdminAuthenticator
from app.config import Settings
from app.ingestion.classifier import ResourceClassifier
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

    @router.post("/ingestions/url")
    def submit_url(request: Request, url: str = Form()):
        ensure_logged_in(request)
        descriptor = ResourceClassifier.default().classify_url(url)
        with Session(request.app.state.engine) as session:
            job = IngestionJob(
                input_type="url", original_url=descriptor.original_url, canonical_url=descriptor.canonical_url,
                source_platform=descriptor.source_platform, media_type=descriptor.media_type.value,
                max_attempts=request.app.state.settings.ingestion_max_attempts,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
        if descriptor.media_type.value == "video":
            request.app.state.ingestion_executor.submit(request.app.state.run_ingestion, job.job_id)
        return RedirectResponse(f"/admin/ingestions/{job.job_id}", status_code=status.HTTP_303_SEE_OTHER)

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
