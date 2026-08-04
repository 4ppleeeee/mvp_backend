from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock

try:
    import fcntl
except ImportError:  # pragma: no cover - the deployed and supported dev hosts are Unix.
    fcntl = None

from llama_index.core import Document, StorageContext, VectorStoreIndex, load_index_from_storage
from llama_index.core.embeddings import BaseEmbedding, MockEmbedding
from llama_index.core.schema import MetadataMode, NodeRelationship, RelatedNodeInfo, TextNode
from llama_index.core.vector_stores import FilterOperator, MetadataFilter, MetadataFilters
from llama_index.embeddings.ollama import OllamaEmbedding
from sqlmodel import Session, select

from app.config import Settings
from app.models import SourceEvidence, TravelSource


_DENSE_SEGMENT_THRESHOLD = 64
_DENSE_SEGMENT_CHARS = 1_000


@dataclass(frozen=True)
class RetrievedEvidence:
    source_id: str
    evidence_id: str
    text: str
    score: float
    segment_index: int | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None


class RagIndex:
    _persist_locks_guard = Lock()
    _persist_locks: dict[Path, object] = {}

    def __init__(self, *, persist_dir: Path, embedding_model: BaseEmbedding, top_k: int) -> None:
        self._persist_dir = persist_dir.resolve()
        self._embedding_model = embedding_model
        self._top_k = top_k
        self._persist_lock = self._lock_for_persist_dir(self._persist_dir)
        self._advisory_lock_path = self._persist_dir.parent / f".{self._persist_dir.name}.lock"

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
        with self._locked():
            index = self._load_index()
            try:
                index.delete_ref_doc(source.source_id, delete_from_docstore=True)
            except ValueError:
                pass
            index.insert_nodes(build_source_nodes(source, evidence))
            self._persist(index)

    def retrieve(self, query: str, *, allowed_source_ids: set[str]) -> list[RetrievedEvidence]:
        if not allowed_source_ids:
            return []
        with self._locked():
            if not self._has_persisted_index():
                return []
            index = self._load_index()
        filters = MetadataFilters(
            filters=[
                MetadataFilter(
                    key="source_id",
                    value=sorted(allowed_source_ids),
                    operator=FilterOperator.IN,
                )
            ]
        )
        nodes = index.as_retriever(similarity_top_k=self._top_k, filters=filters).retrieve(query)
        return [
            RetrievedEvidence(
                source_id=node.node.metadata["source_id"],
                evidence_id=node.node.metadata["evidence_id"],
                text=node.node.get_content(metadata_mode=MetadataMode.NONE),
                score=float(node.score or 0),
                segment_index=node.node.metadata.get("segment_index"),
                start_seconds=node.node.metadata.get("start_seconds"),
                end_seconds=node.node.metadata.get("end_seconds"),
            )
            for node in nodes
        ][: self._top_k]

    @classmethod
    def _lock_for_persist_dir(cls, persist_dir: Path) -> object:
        with cls._persist_locks_guard:
            lock = cls._persist_locks.get(persist_dir)
            if lock is None:
                lock = RLock()
                cls._persist_locks[persist_dir] = lock
            return lock

    @contextmanager
    def _locked(self):
        with self._persist_lock:
            if fcntl is None:
                raise RuntimeError("RAG persistent indexes require Unix advisory file locking")
            self._advisory_lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self._advisory_lock_path.open("a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
            "segment_count": len(evidence.segments or []),
        },
    )


def build_source_nodes(source: TravelSource, evidence: SourceEvidence) -> list[TextNode]:
    metadata = _source_metadata(source, evidence)
    segments = [
        (segment_index, text, segment.get("start_seconds"), segment.get("end_seconds"))
        for segment_index, segment in enumerate(evidence.segments or [])
        if isinstance(segment.get("text"), str) and (text := segment["text"].strip())
    ]
    if len(segments) > _DENSE_SEGMENT_THRESHOLD:
        return _build_compacted_segment_nodes(source, evidence, metadata, segments)
    nodes = [
        _source_node(
            source.source_id,
            f"{source.source_id}:{evidence.evidence_id}:segment:{segment_index}",
            text,
            {
                **metadata,
                "segment_index": segment_index,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
            },
        )
        for segment_index, text, start_seconds, end_seconds in segments
    ]
    if nodes:
        return nodes
    return [
        _source_node(
            source.source_id,
            f"{source.source_id}:{evidence.evidence_id}:full",
            evidence.full_text,
            metadata,
        )
    ]


def _build_compacted_segment_nodes(
    source: TravelSource,
    evidence: SourceEvidence,
    metadata: dict[str, object],
    segments: list[tuple[int, str, object, object]],
) -> list[TextNode]:
    nodes: list[TextNode] = []
    chunk: list[str] = []
    chunk_start_index: int | None = None
    chunk_start_seconds: object = None
    chunk_end_seconds: object = None
    chunk_chars = 0

    def flush() -> None:
        nonlocal chunk, chunk_start_index, chunk_start_seconds, chunk_end_seconds, chunk_chars
        if not chunk or chunk_start_index is None:
            return
        nodes.append(
            _source_node(
                source.source_id,
                f"{source.source_id}:{evidence.evidence_id}:chunk:{chunk_start_index}",
                "\n".join(chunk),
                {
                    **metadata,
                    "segment_index": chunk_start_index,
                    "start_seconds": chunk_start_seconds,
                    "end_seconds": chunk_end_seconds,
                },
            )
        )
        chunk = []
        chunk_start_index = None
        chunk_start_seconds = None
        chunk_end_seconds = None
        chunk_chars = 0

    for segment_index, text, start_seconds, end_seconds in segments:
        if chunk and chunk_chars + len(text) + 1 > _DENSE_SEGMENT_CHARS:
            flush()
        if not chunk:
            chunk_start_index = segment_index
            chunk_start_seconds = start_seconds
        chunk.append(text)
        chunk_chars += len(text) + (1 if len(chunk) > 1 else 0)
        chunk_end_seconds = end_seconds
    flush()
    return nodes


def _source_node(source_id: str, node_id: str, text: str, metadata: dict[str, object]) -> TextNode:
    return TextNode(
        id_=node_id,
        text=text,
        metadata=metadata,
        relationships={NodeRelationship.SOURCE: RelatedNodeInfo(node_id=source_id)},
    )


def _source_metadata(source: TravelSource, evidence: SourceEvidence) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "evidence_id": evidence.evidence_id,
        "title": source.title,
        "original_url": source.original_url,
        "destination": source.destination,
        "category": source.category,
        "normalized_tags": source.normalized_tags,
        "origin": evidence.origin,
        "language": evidence.language,
        "segment_count": len(evidence.segments or []),
    }
