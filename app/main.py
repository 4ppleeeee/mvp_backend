from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response, status
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import Settings
from app.db import create_db_engine, init_db
from app.llm import LmStudioLlmClient, normalize_analysis, normalize_query
from app.models import TravelSource
from app.repository import search_sources, to_card, to_used_source
from app.schemas import (
    AnalyzeSourceResponse,
    ChatRecommendRequest,
    ChatRecommendResponse,
    ClientConfigResponse,
    CollectSourceRequest,
    CollectSourceResponse,
    SourceListResponse,
)


def create_app(settings: Settings | None = None, llm_client: object | None = None) -> FastAPI:
    app_settings = settings or Settings()
    Path(app_settings.uploads_dir).mkdir(parents=True, exist_ok=True)
    engine = create_db_engine(app_settings)
    client = llm_client or LmStudioLlmClient(app_settings)
    init_db(engine)

    app = FastAPI(title="TripGuard MVP Backend", version="0.1.0")
    app.state.settings = app_settings
    app.state.engine = engine
    app.state.llm_client = client

    def get_session() -> Session:
        with Session(engine) as session:
            yield session

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
            destination=analysis.destination,
            category=analysis.category,
            location_name=analysis.location_name,
            normalized_tags=analysis.normalized_tags,
            raw_tags=analysis.raw_tags,
        )

    @app.post("/sources/analyze", response_model=AnalyzeSourceResponse)
    async def analyze_source(request: CollectSourceRequest) -> AnalyzeSourceResponse:
        return await analyze_request(request)

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
