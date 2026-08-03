from dataclasses import dataclass

from llama_index.core import Document

from app.models import SourceEvidence, TravelSource


@dataclass(frozen=True)
class RetrievedEvidence:
    source_id: str
    evidence_id: str
    text: str
    score: float


def build_source_document(source: TravelSource, evidence: SourceEvidence) -> Document:
    return Document(
        id_=source.source_id,
        text=evidence.full_text,
        metadata={
            "source_id": source.source_id,
            "evidence_id": evidence.evidence_id,
            "title": source.title,
            "original_url": source.original_url,
            "destination": source.destination,
            "category": source.category,
            "normalized_tags": source.normalized_tags,
            "origin": evidence.origin,
            "language": evidence.language,
            "segment_count": len(evidence.segments),
        },
    )
