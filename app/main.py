from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi import Depends, FastAPI, HTTPException, Response, UploadFile, status
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import Settings
from app.db import create_db_engine, init_db
from app.llm import OllamaLlmClient, normalize_analysis, normalize_query
from app.models import IngestionJob, TravelSource
from app.repository import search_sources, to_card, to_used_source
from app.schemas import (
    AnalyzeSourceResponse,
    AnalyzeImageRequest,
    ChatRecommendRequest,
    ChatRecommendResponse,
    ClientConfigResponse,
    CollectSourceRequest,
    CollectSourceResponse,
    CreateIngestionRequest,
    IngestionAcceptedResponse,
    IngestionStatusResponse,
    SourceListResponse,
)
from app.ingestion.classifier import ResourceClassifier
from app.ingestion.adapters import default_video_adapters
from app.ingestion.pipeline import VideoPipeline
from app.ingestion.service import IngestionService
from app.ingestion.transcriber import BiliNoteWhisperTranscriber
from app.ingestion.image_service import ImageIngestionService
from app.admin_routes import create_admin_router


def create_app(settings: Settings | None = None, llm_client: object | None = None) -> FastAPI:
    app_settings = settings or Settings()
    Path(app_settings.uploads_dir).mkdir(parents=True, exist_ok=True)
    engine = create_db_engine(app_settings)
    client = llm_client or OllamaLlmClient(app_settings)
    init_db(engine)

    app = FastAPI(title="TripGuard MVP Backend", version="0.1.0")
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.add_middleware(SessionMiddleware, secret_key=app_settings.admin_session_secret or "admin-auth-unconfigured")
    app.state.settings = app_settings
    app.state.engine = engine
    app.state.llm_client = client
    app.state.ingestion_executor = ThreadPoolExecutor(max_workers=1)
    app.include_router(create_admin_router(app_settings))

    def get_session() -> Session:
        with Session(engine) as session:
            yield session

    def run_ingestion(job_id: str) -> None:
        with Session(engine) as session:
            job = session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
            if job.input_type == "image":
                ImageIngestionService(session=session, llm_client=client).run(job_id)
                return
            adapter = next(adapter for adapter in default_video_adapters() if adapter.platform == job.source_platform)
            pipeline = VideoPipeline(
                adapter=adapter,
                transcriber=BiliNoteWhisperTranscriber(
                    model_size=app_settings.whisper_model,
                    device=app_settings.whisper_device,
                    compute_type=app_settings.whisper_compute_type,
                ),
                temp_root=Path(app_settings.ingestion_temp_dir),
            )
            IngestionService(session=session, llm_client=client, pipeline=pipeline).run(job_id)

    app.state.run_ingestion = run_ingestion

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": app_settings.service_name,
            "llm_model": app_settings.llm_model,
        }

    @app.get("/client/config", response_model=ClientConfigResponse)
    def client_config() -> ClientConfigResponse:
        return ClientConfigResponse(
            api_base_url=app_settings.public_base_url,
            service=app_settings.service_name,
            llm_model=app_settings.llm_model,
        )

    @app.post("/ingestions", response_model=IngestionAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
    def create_ingestion(request: CreateIngestionRequest, session: Session = Depends(get_session)) -> IngestionAcceptedResponse:
        descriptor = ResourceClassifier.default().classify_url(request.url)
        job = IngestionJob(
            input_type=request.input_type,
            original_url=descriptor.original_url,
            canonical_url=descriptor.canonical_url,
            source_platform=descriptor.source_platform,
            media_type=descriptor.media_type.value,
            max_attempts=app_settings.ingestion_max_attempts,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        if descriptor.media_type.value != "video":
            job.status = "failed"
            job.stage = "failed"
            job.error_code = "unsupported_media_type"
            job.error_message = "this ingestion endpoint currently supports video URLs"
            session.add(job)
            session.commit()
            session.refresh(job)
        else:
            app.state.ingestion_executor.submit(run_ingestion, job.job_id)
        return IngestionAcceptedResponse(job_id=job.job_id, status=job.status)

    @app.get("/ingestions/{job_id}", response_model=IngestionStatusResponse)
    def get_ingestion(job_id: str, session: Session = Depends(get_session)) -> IngestionStatusResponse:
        job = session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).first()
        if job is None:
            raise HTTPException(status_code=404, detail="ingestion not found")
        return IngestionStatusResponse(
            job_id=job.job_id,
            status=job.status,
            stage=job.stage,
            source_id=job.source_id,
            error_code=job.error_code,
            error_message=job.error_message,
        )

    @app.post("/ingestions/image", response_model=IngestionAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
    async def create_image_ingestion(file: UploadFile, session: Session = Depends(get_session)) -> IngestionAcceptedResponse:
        if not (file.content_type or "").startswith("image/"):
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="image upload required")
        job = IngestionJob(input_type="image", media_type="image", source_platform="image", max_attempts=app_settings.ingestion_max_attempts)
        session.add(job)
        session.commit()
        session.refresh(job)
        job_dir = Path(app_settings.ingestion_temp_dir) / job.job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        suffix = Path(file.filename or "upload").suffix or ".bin"
        image_path = job_dir / f"image{suffix}"
        size = 0
        try:
            with image_path.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > app_settings.ingestion_max_upload_bytes:
                        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="image upload too large")
                    output.write(chunk)
        except Exception:
            image_path.unlink(missing_ok=True)
            job_dir.rmdir()
            raise
        job.input_path = str(image_path)
        session.add(job)
        session.commit()
        session.refresh(job)
        app.state.ingestion_executor.submit(run_ingestion, job.job_id)
        return IngestionAcceptedResponse(job_id=job.job_id, status=job.status)

    async def analyze_request(request: CollectSourceRequest) -> AnalyzeSourceResponse:
        try:
            analysis = await client.analyze_source(
                title=request.title,
                body_text=request.body_text,
                url=request.url,
                source_platform=request.source_platform,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="llm unavailable") from exc
        analysis = normalize_analysis(analysis)
        return AnalyzeSourceResponse(
            is_travel_related=analysis.is_travel_related,
            reason=analysis.reason,
            confidence=analysis.confidence,
            title=analysis.title,
            body_text=analysis.body_text,
            destination=analysis.destination,
            category=analysis.category,
            location_name=analysis.location_name,
            normalized_tags=analysis.normalized_tags,
            raw_tags=analysis.raw_tags,
        )

    @app.post("/sources/analyze", response_model=AnalyzeSourceResponse)
    async def analyze_source(request: CollectSourceRequest) -> AnalyzeSourceResponse:
        return await analyze_request(request)

    @app.post("/sources/analyze-image", response_model=AnalyzeSourceResponse)
    async def analyze_image(request: AnalyzeImageRequest) -> AnalyzeSourceResponse:
        return await analyze_image_request(request)

    async def analyze_image_request(request: AnalyzeImageRequest) -> AnalyzeSourceResponse:
        try:
            analysis = await client.analyze_image(
                image_base64=request.image_base64,
                title_hint=request.title_hint,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="llm unavailable") from exc
        analysis = normalize_analysis(analysis)
        return AnalyzeSourceResponse(
            is_travel_related=analysis.is_travel_related,
            reason=analysis.reason,
            confidence=analysis.confidence,
            title=analysis.title,
            body_text=analysis.body_text,
            destination=analysis.destination,
            category=analysis.category,
            location_name=analysis.location_name,
            normalized_tags=analysis.normalized_tags,
            raw_tags=analysis.raw_tags,
        )

    @app.post("/sources/collect-image", response_model=CollectSourceResponse, status_code=status.HTTP_201_CREATED)
    async def collect_image_source(
        request: AnalyzeImageRequest,
        response: Response,
        session: Session = Depends(get_session),
    ) -> CollectSourceResponse:
        analysis = await analyze_image_request(request)
        if not analysis.is_travel_related:
            response.status_code = status.HTTP_200_OK
            return CollectSourceResponse(saved=False, reason=analysis.reason or "not travel related")

        source = TravelSource(
            title=analysis.title or request.title_hint or "长图旅行资料",
            body_text=analysis.body_text or analysis.title or request.title_hint or "长图旅行资料",
            original_url=None,
            source_platform=request.source_platform or "image",
            cover_image_url=None,
            destination=analysis.destination,
            category=analysis.category,
            location_name=analysis.location_name,
            normalized_tags=analysis.normalized_tags,
            raw_tags=analysis.raw_tags,
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        return CollectSourceResponse(saved=True, source=to_card(source))

    @app.post("/sources/collect", response_model=CollectSourceResponse, status_code=status.HTTP_201_CREATED)
    async def collect_source(
        request: CollectSourceRequest,
        response: Response,
        session: Session = Depends(get_session),
    ) -> CollectSourceResponse:
        analysis = await analyze_request(request)
        if not analysis.is_travel_related:
            response.status_code = status.HTTP_200_OK
            return CollectSourceResponse(saved=False, reason=analysis.reason or "not travel related")

        source = TravelSource(
            title=request.title,
            body_text=request.body_text,
            original_url=request.url,
            source_platform=request.source_platform,
            cover_image_url=request.cover_image_url,
            destination=analysis.destination,
            category=analysis.category,
            location_name=analysis.location_name,
            normalized_tags=analysis.normalized_tags,
            raw_tags=analysis.raw_tags,
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        return CollectSourceResponse(saved=True, source=to_card(source))

    @app.get("/sources", response_model=SourceListResponse)
    def list_sources(session: Session = Depends(get_session)) -> SourceListResponse:
        items = session.exec(select(TravelSource).order_by(TravelSource.created_at.desc())).all()
        return SourceListResponse(items=[to_card(item) for item in items])

    @app.get("/sources/{source_id}")
    def get_source(source_id: str, session: Session = Depends(get_session)) -> dict:
        source = session.exec(select(TravelSource).where(TravelSource.source_id == source_id)).first()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        card = to_card(source).model_dump()
        card["body_text"] = source.body_text
        return card

    @app.post("/chat/recommend", response_model=ChatRecommendResponse)
    async def recommend(request: ChatRecommendRequest, session: Session = Depends(get_session)) -> ChatRecommendResponse:
        try:
            query = normalize_query(await client.parse_query(message=request.message))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="llm unavailable") from exc
        sources = search_sources(
            session,
            destination=query.destination,
            categories=query.categories,
            normalized_tags=query.normalized_tags,
            limit=request.limit,
        )
        contexts = [
            {
                "source_id": source.source_id,
                "title": source.title,
                "body_text": source.body_text,
                "original_url": source.original_url,
                "destination": source.destination,
                "category": source.category,
                "normalized_tags": source.normalized_tags,
            }
            for source in sources
        ]
        try:
            result = await client.recommend(message=request.message, query=query, contexts=contexts)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="llm unavailable") from exc
        used_ids = result.get("used_source_ids") or result.get("usedSourceIds") or []
        by_id = {source.source_id: source for source in sources}
        used_sources = [to_used_source(by_id[source_id]) for source_id in used_ids if source_id in by_id]
        if not used_sources:
            used_sources = [to_used_source(source) for source in sources[:3]]
        return ChatRecommendResponse(answer=str(result.get("answer", "")), used_sources=used_sources)

    return app


app = create_app()
