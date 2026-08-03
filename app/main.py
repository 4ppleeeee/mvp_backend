from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import logging

from fastapi import Depends, FastAPI, HTTPException, Response, UploadFile, status
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import Settings
from app.db import create_db_engine, init_db
from app.llm import OllamaLlmClient, normalize_analysis, normalize_query
from app.models import IngestionJob, TravelSource
from app.repository import search_sources, to_card, to_used_source
from app.rag import RagIndex, sync_source
from app.schemas import (
    AnalyzeSourceResponse,
    AnalyzeImageRequest,
    ChatAction,
    ChatActionRequest,
    ChatGrounding,
    ChatRecommendRequest,
    ChatRecommendResponse,
    ChatUiEvent,
    ChatUiResponse,
    ClientConfigResponse,
    CollectSourceRequest,
    CollectSourceResponse,
    CreateIngestionRequest,
    IngestionAcceptedResponse,
    IngestionStatusResponse,
    ItinerarySlot,
    SourceListResponse,
)
from app.ingestion.classifier import ResourceClassifier
from app.ingestion.article import ArticleContentParser, ArticlePipeline
from app.ingestion.input import extract_first_http_url
from app.ingestion.pipeline import MediaPipeline
from app.ingestion.media import MediaEgressPolicy
from app.ingestion.planner import IngestionPlan
from app.ingestion.service import IngestionService
from app.ingestion.sources import SourceRegistry
from app.ingestion.transcriber import BiliNoteWhisperTranscriber
from app.ingestion.image_service import ImageIngestionService
from app.admin_routes import create_admin_router


logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    llm_client: object | None = None,
    rag_index: object | None = None,
) -> FastAPI:
    app_settings = settings or Settings()
    Path(app_settings.uploads_dir).mkdir(parents=True, exist_ok=True)
    engine = create_db_engine(app_settings)
    client = llm_client or OllamaLlmClient(app_settings)
    index = rag_index or (RagIndex.from_settings(app_settings) if app_settings.rag_enabled else None)
    init_db(engine)

    app = FastAPI(title="TripGuard MVP Backend", version="0.1.0")
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.add_middleware(SessionMiddleware, secret_key=app_settings.admin_session_secret or "admin-auth-unconfigured")
    app.state.settings = app_settings
    app.state.engine = engine
    app.state.llm_client = client
    app.state.rag_index = index
    app.state.ingestion_executor = ThreadPoolExecutor(max_workers=1)
    app.include_router(create_admin_router(app_settings))

    def get_session() -> Session:
        with Session(engine) as session:
            yield session

    def sync_rag_source(source_id_or_session: str | Session, source_id: str | None = None) -> None:
        """Synchronize a committed source using a Session owned by this worker.

        Admin review historically passes its request Session as the first argument;
        accept that shape while deliberately never using the Session off its owner
        thread. New callers pass only the source id.
        """
        resolved_source_id = source_id if isinstance(source_id, str) else (
            source_id_or_session if isinstance(source_id_or_session, str) else None
        )
        if index is None:
            return
        if resolved_source_id is None:
            logger.warning("RAG source sync skipped without a source id")
            return
        try:
            with Session(engine) as rag_session:
                try:
                    if sync_source(rag_session, index, resolved_source_id):
                        rag_session.commit()
                except Exception:
                    rag_session.rollback()
                    raise
        except Exception:
            logger.exception("RAG source sync failed", extra={"source_id": resolved_source_id})

    def sync_completed_job_source(session: Session, job: IngestionJob) -> None:
        session.refresh(job)
        if job.status == "succeeded" and job.source_id:
            sync_rag_source(job.source_id)

    app.state.sync_rag_source = sync_rag_source

    def run_ingestion(job_id: str) -> None:
        with Session(engine) as session:
            job = session.exec(select(IngestionJob).where(IngestionJob.job_id == job_id)).one()
            if job.input_type == "image":
                ImageIngestionService(session=session, llm_client=client).run(job_id)
                sync_completed_job_source(session, job)
                return
            if job.media_type == "article" and job.source_platform == "xiaohongshu":
                if ArticleContentParser.is_xhs_video_url(job.original_url or ""):
                    job.media_type = "video"
                    session.add(job)
                    session.commit()
            if job.media_type == "article":
                IngestionService(session=session, llm_client=client, pipeline=ArticlePipeline()).run(job_id)
                sync_completed_job_source(session, job)
                return
            default_policy = MediaEgressPolicy()
            proxy_policy = MediaEgressPolicy(app_settings.media_proxy_url)
            primary_registry = SourceRegistry.default(default_policy, include_xiaohongshu=True)
            primary_adapter = primary_registry.resolve(job.original_url or "")
            if primary_adapter is None:
                raise ValueError(f"no source adapter for platform: {job.source_platform}")
            primary_plan = IngestionPlan.from_probe(primary_adapter.probe(job.original_url or ""))
            pipeline = MediaPipeline(
                adapter=primary_adapter,
                transcriber=BiliNoteWhisperTranscriber(
                    model_size=app_settings.whisper_model,
                    device=app_settings.whisper_device,
                    compute_type=app_settings.whisper_compute_type,
                ),
                temp_root=Path(app_settings.ingestion_temp_dir),
                keyframe_enabled=app_settings.video_keyframes_enabled,
                frame_interval_seconds=app_settings.video_frame_interval_seconds,
                grid_size=(app_settings.video_grid_columns, app_settings.video_grid_rows),
                plan=primary_plan,
            )
            fallback_pipeline = None
            if app_settings.media_proxy_url:
                fallback_registry = SourceRegistry.default(proxy_policy, include_xiaohongshu=True)
                fallback_adapter = fallback_registry.resolve(job.original_url or "")
                if fallback_adapter is None:
                    raise ValueError(f"no fallback source adapter for platform: {job.source_platform}")
                fallback_pipeline = MediaPipeline(
                    adapter=fallback_adapter,
                    transcriber=BiliNoteWhisperTranscriber(
                        model_size=app_settings.whisper_model,
                        device=app_settings.whisper_device,
                        compute_type=app_settings.whisper_compute_type,
                    ),
                    temp_root=Path(app_settings.ingestion_temp_dir),
                    keyframe_enabled=app_settings.video_keyframes_enabled,
                    frame_interval_seconds=app_settings.video_frame_interval_seconds,
                    grid_size=(app_settings.video_grid_columns, app_settings.video_grid_rows),
                    plan=IngestionPlan.from_probe(fallback_adapter.probe(job.original_url or "")),
                )
            IngestionService(
                session=session, llm_client=client, pipeline=pipeline, fallback_pipeline=fallback_pipeline
            ).run(job_id)
            sync_completed_job_source(session, job)

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
        descriptor = ResourceClassifier.default().classify_url(extract_first_http_url(request.url))
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
        if descriptor.media_type.value not in {"video", "audio", "article"}:
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
            media_egress=job.media_egress,
            failure_stage=job.failure_stage,
            progress_percent=job.progress_percent,
            progress_message=job.progress_message,
            progress_updated_at=job.progress_updated_at,
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
        await run_in_threadpool(sync_rag_source, source.source_id)
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
        await run_in_threadpool(sync_rag_source, source.source_id)
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
        card["summary_text"] = source.summary_text
        return card

    async def build_recommendation(
        request: ChatRecommendRequest,
        session: Session,
        *,
        excluded_source_ids: set[str] | None = None,
    ) -> tuple[ChatRecommendResponse, dict[str, TravelSource], dict[str, object]]:
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
        if excluded_source_ids:
            sources = [source for source in sources if source.source_id not in excluded_source_ids]
        by_id = {source.source_id: source for source in sources}
        contexts = [
            {
                "source_id": source.source_id,
                "title": source.title,
                "body_text": source.summary_text or source.body_text,
                "original_url": source.original_url,
                "destination": source.destination,
                "category": source.category,
                "normalized_tags": source.normalized_tags,
            }
            for source in sources
        ]
        retrieved_by_source: dict[str, object] = {}
        rag_index = app.state.rag_index
        if rag_index is not None and by_id:
            try:
                retrieved_evidence = await run_in_threadpool(
                    rag_index.retrieve,
                    request.message,
                    allowed_source_ids=set(by_id),
                )
                if retrieved_evidence and all(evidence.source_id in by_id for evidence in retrieved_evidence):
                    retrieved_by_source = {
                        evidence.source_id: evidence
                        for evidence in retrieved_evidence
                    }
                    contexts = [
                        {
                            "source_id": evidence.source_id,
                            "evidence_id": evidence.evidence_id,
                            "segment_index": evidence.segment_index,
                            "start_seconds": evidence.start_seconds,
                            "end_seconds": evidence.end_seconds,
                            "title": by_id[evidence.source_id].title,
                            "body_text": evidence.text,
                            "original_url": by_id[evidence.source_id].original_url,
                            "destination": by_id[evidence.source_id].destination,
                            "category": by_id[evidence.source_id].category,
                            "normalized_tags": by_id[evidence.source_id].normalized_tags,
                        }
                        for evidence in retrieved_evidence
                    ]
            except Exception:
                logger.exception("RAG recommendation retrieval failed")
        try:
            result = await client.recommend(message=request.message, query=query, contexts=contexts)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="llm unavailable") from exc
        used_ids = result.get("used_source_ids") or result.get("usedSourceIds") or []
        used_sources = [to_used_source(by_id[source_id]) for source_id in used_ids if source_id in by_id]
        if not used_sources:
            used_sources = [to_used_source(source) for source in sources[:3]]
        return ChatRecommendResponse(answer=str(result.get("answer", "")), used_sources=used_sources), by_id, retrieved_by_source

    def build_chat_events(
        recommendation: ChatRecommendResponse,
        sources_by_id: dict[str, TravelSource],
        retrieved_by_source: dict[str, object],
    ) -> list[ChatUiEvent]:
        events = [
            ChatUiEvent(
                event_id="assistant:0",
                type="assistant_text",
                text=recommendation.answer or "暂时没有足够资料生成推荐。",
            )
        ]
        if not recommendation.used_sources:
            return events

        first_source = recommendation.used_sources[0]
        first_slot = ItinerarySlot(
            slot_id=f"slot:{first_source.source_id}",
            time_label="DAY 1",
            title=first_source.title,
            subtitle=f"{first_source.destination} · {first_source.category}",
        )
        events.append(
            ChatUiEvent(
                event_id=f"itinerary:{first_source.source_id}",
                type="itinerary_card",
                grounding=ChatGrounding(kind="suggestion"),
                title="DAY 1 · 推荐路线",
                slots=[first_slot],
                actions=[
                    ChatAction(
                        action_id="add_itinerary",
                        label="加入当前行程",
                        kind="local",
                        payload={"slot_ids": [first_slot.slot_id]},
                    )
                ],
            )
        )
        for used_source in recommendation.used_sources:
            source = sources_by_id[used_source.source_id]
            evidence = retrieved_by_source.get(source.source_id)
            if evidence is not None:
                grounding = ChatGrounding(
                    kind="knowledge_base",
                    source_id=source.source_id,
                    evidence_id=evidence.evidence_id,
                    segment_index=evidence.segment_index,
                    start_seconds=evidence.start_seconds,
                    end_seconds=evidence.end_seconds,
                )
                summary = evidence.text
            else:
                grounding = ChatGrounding(kind="suggestion")
                summary = source.summary_text or source.body_text
            slot_id = f"slot:{source.source_id}"
            events.append(
                ChatUiEvent(
                    event_id=f"place:{source.source_id}",
                    type="place_card",
                    grounding=grounding,
                    title=source.title,
                    summary=summary,
                    tags=source.normalized_tags,
                    actions=[
                        ChatAction(
                            action_id="add_slot",
                            label="加入第 1 天",
                            kind="local",
                            payload={"slot_id": slot_id},
                        ),
                        ChatAction(
                            action_id="refresh_places",
                            label="换一批",
                            kind="remote",
                            payload={"exclude_event_ids": [f"place:{source.source_id}"]},
                        ),
                    ],
                )
            )
            if evidence is not None:
                time_label = (
                    f" · 视频 {int(evidence.start_seconds // 60):02d}:{int(evidence.start_seconds % 60):02d}"
                    if evidence.start_seconds is not None
                    else ""
                )
                events.append(
                    ChatUiEvent(
                        event_id=f"evidence:{evidence.evidence_id}",
                        type="evidence_card",
                        grounding=grounding,
                        label=f"我的收藏{time_label}",
                        excerpt=evidence.text,
                        actions=[
                            ChatAction(
                                action_id="toggle_evidence",
                                label="查看证据",
                                kind="local",
                            )
                        ],
                    )
                )
        return events

    async def build_model_chat_ui(
        request: ChatRecommendRequest,
        recommendation: ChatRecommendResponse,
        sources_by_id: dict[str, TravelSource],
        retrieved_by_source: dict[str, object],
    ) -> ChatUiResponse | None:
        generate_ui = getattr(client, "generate_chat_ui", None)
        if not callable(generate_ui):
            return None
        contexts = []
        evidence_ids_by_source: dict[str, set[str]] = {}
        matched_sources = {
            source_id: source
            for source_id, source in sources_by_id.items()
            if source.destination and source.destination in request.message
        }
        for source_id, source in matched_sources.items():
            evidence = retrieved_by_source.get(source_id)
            if evidence is not None:
                evidence_ids_by_source[source_id] = {str(evidence.evidence_id)}
            contexts.append(
                {
                    "source_id": source_id,
                    "title": source.title,
                    "destination": source.destination,
                    "category": source.category,
                    "normalized_tags": source.normalized_tags,
                    "evidence_id": getattr(evidence, "evidence_id", None),
                    "evidence_text": getattr(evidence, "text", None),
                }
            )
        try:
            candidate = await generate_ui(
                message=request.message,
                answer=recommendation.answer,
                contexts=contexts,
            )
        except Exception:
            logger.exception("model-generated chat UI failed")
            return None
        if not isinstance(candidate, ChatUiResponse) or not candidate.message_id or not candidate.events:
            return None
        # The first Catalog is deliberately read-only. Model-proposed actions are
        # data from an untrusted generator and are removed until the Compose
        # action reducer has explicitly registered those capabilities.
        candidate = candidate.model_copy(
            update={
                "events": [event.model_copy(update={"actions": []}) for event in candidate.events],
            }
        )
        if len({event.event_id for event in candidate.events}) != len(candidate.events):
            return None
        allowed_place_titles = {source.title for source in matched_sources.values()}
        allowed_actions = {
            "itinerary_card": {"add_itinerary"},
            "place_card": {"add_slot", "refresh_places"},
            "evidence_card": {"toggle_evidence"},
            "assistant_text": set(),
        }
        for event in candidate.events:
            if not event.event_id or event.type not in allowed_actions:
                return None
            if event.type == "assistant_text" and not (event.text or "").strip():
                return None
            if event.type == "itinerary_card" and (
                not (event.title or "").strip()
                or not event.slots
                or any(not slot.slot_id or not slot.time_label or not slot.title for slot in event.slots)
            ):
                return None
            if event.type == "place_card" and not (event.title or "").strip():
                return None
            if event.type == "place_card" and event.title not in allowed_place_titles:
                return None
            if event.type == "evidence_card" and (
                not (event.label or "").strip() or not (event.excerpt or "").strip()
            ):
                return None
            if any(action.action_id not in allowed_actions[event.type] for action in event.actions):
                return None
            grounding = event.grounding
            if grounding is None:
                continue
            if grounding.kind == "knowledge_base":
                if grounding.source_id not in sources_by_id:
                    return None
                if grounding.evidence_id not in evidence_ids_by_source.get(grounding.source_id, set()):
                    return None
            elif grounding.source_id is not None or grounding.evidence_id is not None:
                return None
        return candidate

    @app.post("/chat/recommend", response_model=ChatRecommendResponse)
    async def recommend(request: ChatRecommendRequest, session: Session = Depends(get_session)) -> ChatRecommendResponse:
        recommendation, _, _ = await build_recommendation(request, session)
        return recommendation

    @app.post("/chat", response_model=ChatUiResponse, response_model_exclude_none=True)
    async def chat(request: ChatRecommendRequest, session: Session = Depends(get_session)) -> ChatUiResponse:
        recommendation, sources_by_id, retrieved_by_source = await build_recommendation(request, session)
        model_ui = await build_model_chat_ui(request, recommendation, sources_by_id, retrieved_by_source)
        if model_ui is not None:
            return model_ui
        return ChatUiResponse(
            message_id="msg_current",
            events=build_chat_events(recommendation, sources_by_id, retrieved_by_source),
        )

    @app.post("/chat/action", response_model=ChatUiResponse, response_model_exclude_none=True)
    async def chat_action(request: ChatActionRequest, session: Session = Depends(get_session)) -> ChatUiResponse:
        if request.action_id != "refresh_places":
            raise HTTPException(status_code=400, detail="unsupported chat action")
        excluded_source_ids = {
            event_id.removeprefix("place:")
            for event_id in request.payload.get("exclude_event_ids", [])
            if isinstance(event_id, str) and event_id.startswith("place:")
        }
        recommendation, sources_by_id, retrieved_by_source = await build_recommendation(
            request,
            session,
            excluded_source_ids=excluded_source_ids,
        )
        return ChatUiResponse(
            message_id="msg_current",
            events=build_chat_events(recommendation, sources_by_id, retrieved_by_source),
        )

    return app


app = create_app()
