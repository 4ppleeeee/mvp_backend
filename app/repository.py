from sqlmodel import Session, col, select

from app.models import TravelSource
from app.schemas import TravelSourceCard, UsedSource


def to_card(source: TravelSource) -> TravelSourceCard:
    return TravelSourceCard(
        source_id=source.source_id,
        title=source.title,
        original_url=source.original_url,
        source_platform=source.source_platform,
        cover_image_url=source.cover_image_url,
        destination=source.destination,
        category=source.category,
        location_name=source.location_name,
        normalized_tags=source.normalized_tags,
        raw_tags=source.raw_tags,
        created_at=source.created_at,
    )


def to_used_source(source: TravelSource) -> UsedSource:
    return UsedSource(
        source_id=source.source_id,
        title=source.title,
        original_url=source.original_url,
        cover_image_url=source.cover_image_url,
        source_platform=source.source_platform,
        destination=source.destination,
        category=source.category,
        normalized_tags=source.normalized_tags,
    )


def search_sources(
    session: Session,
    *,
    destination: str | None,
    categories: list[str],
    normalized_tags: list[str],
    limit: int,
) -> list[TravelSource]:
    statement = select(TravelSource)
    if destination:
        statement = statement.where(col(TravelSource.destination).contains(destination))
    if categories:
        statement = statement.where(col(TravelSource.category).in_(categories))
    statement = statement.order_by(TravelSource.created_at.desc()).limit(limit * 3)
    candidates = list(session.exec(statement))
    if normalized_tags:
        tagged = [
            source
            for source in candidates
            if set(source.normalized_tags).intersection(normalized_tags)
        ]
        if tagged:
            candidates = tagged
    return candidates[:limit]

