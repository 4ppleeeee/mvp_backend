from dataclasses import dataclass
from pathlib import Path
from llama_index.core import Document, StorageContext, VectorStoreIndex, load_index_from_storage
from llama_index.core.embeddings import BaseEmbedding, MockEmbedding
from llama_index.core.schema import MetadataMode
from llama_index.embeddings.ollama import OllamaEmbedding
from sqlmodel import Session, select

from app.config import Settings
from app.models import SourceEvidence, TravelSource


@dataclass(frozen=True)
class RetrievedEvidence:
    source_id: str
    evidence_id: str
    text: str
    score: float


class RagIndex:
    def __init__(self, *, persist_dir: Path, embedding_model: BaseEmbedding, top_k: int) -> None:
        self._persist_dir = persist_dir
        self._embedding_model = embedding_model
        self._top_k = top_k

    @classmethod
    def from_settings(cls, settings: Settings) -> "RagIndex":
        return cls(
            persist_dir=Path(settings.rag_persist_dir),
            embedding_model=OllamaEmbedding(
                model_name=settings.rag_embedding_model,
                base_url=settings.llm_base_url,
            ),
            top_k=settings.rag_top_k,
        )

    @classmethod
    def for_test(cls, persist_dir: Path) -> "RagIndex":
        return cls(
            persist_dir=persist_dir,
            embedding_model=MockEmbedding(embed_dim=8),
            top_k=6,
        )

    def upsert_source(self, source: TravelSource, evidence: SourceEvidence) -> None:
        index = self._load_index()
        try:
            index.delete_ref_doc(source.source_id, delete_from_docstore=True)
        except ValueError:
            pass
        index.insert(build_source_document(source, evidence))
        self._persist(index)

    def retrieve(self, query: str, *, allowed_source_ids: set[str]) -> list[RetrievedEvidence]:
        if not allowed_source_ids or not self._has_persisted_index():
            return []
        index = self._load_index()
        nodes = index.as_retriever(similarity_top_k=max(self._top_k, len(allowed_source_ids))).retrieve(query)
        return [
            RetrievedEvidence(
                source_id=node.node.metadata["source_id"],
                evidence_id=node.node.metadata["evidence_id"],
                text=node.node.get_content(metadata_mode=MetadataMode.NONE),
                score=float(node.score or 0),
            )
            for node in nodes
            if node.node.metadata.get("source_id") in allowed_source_ids
        ][: self._top_k]

    def _load_index(self) -> VectorStoreIndex:
        if self._has_persisted_index():
            return load_index_from_storage(
                StorageContext.from_defaults(persist_dir=str(self._persist_dir)),
                embed_model=self._embedding_model,
            )
        return VectorStoreIndex([], embed_model=self._embedding_model)

    def _persist(self, index: VectorStoreIndex) -> None:
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        index.storage_context.persist(persist_dir=str(self._persist_dir))

    def _has_persisted_index(self) -> bool:
        return (self._persist_dir / "index_store.json").is_file()


def backfill_sources(session: Session, index: RagIndex) -> int:
    indexed = 0
    sources = session.exec(select(TravelSource).order_by(TravelSource.created_at)).all()
    for source in sources:
        evidence = _get_or_create_evidence(session, source)
        index.upsert_source(source, evidence)
        indexed += 1
    return indexed


def sync_source(session: Session, index: RagIndex, source_id: str) -> bool:
    source = session.exec(select(TravelSource).where(TravelSource.source_id == source_id)).first()
    if source is None:
        return False
    index.upsert_source(source, _get_or_create_evidence(session, source))
    return True


def _get_or_create_evidence(session: Session, source: TravelSource) -> SourceEvidence:
    evidence = session.exec(
        select(SourceEvidence)
        .where(SourceEvidence.source_id == source.source_id)
        .order_by(SourceEvidence.created_at.desc())
    ).first()
    if evidence is not None:
        return evidence
    evidence = SourceEvidence(
        source_id=source.source_id,
        kind="source_body",
        origin="source_body",
        full_text=source.summary_text or source.body_text,
    )
    session.add(evidence)
    session.flush()
    return evidence


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
