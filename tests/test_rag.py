import pytest
from llama_index.core.embeddings import BaseEmbedding
from sqlmodel import Session

from app.config import Settings
from app.db import create_db_engine, init_db
from app.models import SourceEvidence, TravelSource
from app.rag import RagIndex, backfill_sources, build_source_document
from app import rag_backfill


class CandidateRankingEmbedding(BaseEmbedding):
    embed_dim: int

    def __init__(self) -> None:
        super().__init__(embed_dim=2)

    @classmethod
    def class_name(cls) -> str:
        return "CandidateRankingEmbedding"

    def _get_query_embedding(self, query: str) -> list[float]:
        return [1.0, 0.0]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return [1.0, 0.0] if "非候选" in text else [0.0, 1.0]

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)


def test_document_keeps_source_and_evidence_provenance() -> None:
    source = TravelSource(
        source_id="src_tokyo",
        title="浅草早餐攻略",
        body_text="浅草寺附近的早餐店。",
        original_url="https://example.com/asakusa-breakfast",
        source_platform="xhs",
        destination="东京",
        category="eat",
        normalized_tags=["早餐", "排队"],
        raw_tags=["早起"],
    )
    evidence = SourceEvidence(
        evidence_id="evd_asakusa",
        source_id="src_tokyo",
        origin="article",
        language="zh",
        full_text="浅草寺附近早上八点开始排队。",
        segments=[{"index": 0, "text": "浅草寺附近早上八点开始排队。"}],
    )

    document = build_source_document(source, evidence)

    assert document.id_ == "src_tokyo"
    assert document.text == "浅草寺附近早上八点开始排队。"
    assert document.metadata == {
        "source_id": "src_tokyo",
        "evidence_id": "evd_asakusa",
        "title": "浅草早餐攻略",
        "original_url": "https://example.com/asakusa-breakfast",
        "destination": "东京",
        "category": "eat",
        "normalized_tags": ["早餐", "排队"],
        "origin": "article",
        "language": "zh",
        "segment_count": 1,
    }


def test_upsert_replaces_existing_source_nodes_and_survives_reload(tmp_path) -> None:
    source = TravelSource(
        source_id="src_tokyo",
        title="浅草早餐攻略",
        body_text="早餐资料",
        destination="东京",
        category="eat",
    )
    old_evidence = SourceEvidence(
        evidence_id="evd_old",
        source_id="src_tokyo",
        origin="article",
        full_text="旧内容。",
    )
    new_evidence = SourceEvidence(
        evidence_id="evd_new",
        source_id="src_tokyo",
        origin="article",
        full_text="新内容。",
    )
    index = RagIndex.for_test(tmp_path)

    index.upsert_source(source, old_evidence)
    index.upsert_source(source, new_evidence)

    reloaded = RagIndex.for_test(tmp_path)
    results = reloaded.retrieve("新内容", allowed_source_ids={"src_tokyo"})

    assert [(item.evidence_id, item.text) for item in results] == [("evd_new", "新内容。")]


def test_retrieve_filters_sql_candidates_before_top_k_ranking(tmp_path) -> None:
    index = RagIndex(
        persist_dir=tmp_path,
        embedding_model=CandidateRankingEmbedding(),
        top_k=1,
    )
    non_candidate = TravelSource(
        source_id="src_non_candidate",
        title="非候选资料",
        body_text="非候选资料",
        destination="东京",
        category="eat",
    )
    candidate = TravelSource(
        source_id="src_candidate",
        title="候选资料",
        body_text="候选资料",
        destination="东京",
        category="eat",
    )
    index.upsert_source(
        non_candidate,
        SourceEvidence(
            evidence_id="evd_non_candidate",
            source_id=non_candidate.source_id,
            origin="article",
            full_text="非候选资料的高相关内容。",
        ),
    )
    index.upsert_source(
        candidate,
        SourceEvidence(
            evidence_id="evd_candidate",
            source_id=candidate.source_id,
            origin="article",
            full_text="候选资料的较低相关内容。",
        ),
    )

    results = index.retrieve("东京餐厅", allowed_source_ids={candidate.source_id})

    assert [(item.source_id, item.evidence_id) for item in results] == [
        ("src_candidate", "evd_candidate"),
    ]


def test_backfill_creates_evidence_for_legacy_source_and_indexes_it(tmp_path) -> None:
    engine = create_db_engine(Settings(database_url=f"sqlite:///{tmp_path / 'tripguard.db'}"))
    init_db(engine)
    index = RagIndex.for_test(tmp_path / "rag")
    with Session(engine) as session:
        source = TravelSource(
            source_id="src_legacy",
            title="东京咖啡攻略",
            body_text="表参道的咖啡店下午需要排队。",
            destination="东京",
            category="drink",
        )
        session.add(source)
        session.commit()

        indexed = backfill_sources(session, index)
        session.commit()

    assert indexed == 1
    results = index.retrieve("东京咖啡排队", allowed_source_ids={"src_legacy"})
    assert [(item.source_id, item.text) for item in results] == [
        ("src_legacy", "表参道的咖啡店下午需要排队。"),
    ]


def test_backfill_sqlite_index_survives_fresh_index_reload(tmp_path) -> None:
    engine = create_db_engine(Settings(database_url=f"sqlite:///{tmp_path / 'tripguard.db'}"))
    init_db(engine)
    persist_dir = tmp_path / "rag"
    with Session(engine) as session:
        source = TravelSource(
            source_id="src_osaka",
            title="大阪夜市攻略",
            body_text="黑门市场傍晚六点前适合购买寿司。",
            destination="大阪",
            category="eat",
        )
        session.add(source)
        session.commit()

        assert backfill_sources(session, RagIndex.for_test(persist_dir)) == 1
        session.commit()

    reloaded_index = RagIndex.for_test(persist_dir)
    results = reloaded_index.retrieve("大阪黑门市场寿司", allowed_source_ids={"src_osaka"})

    assert [(item.source_id, item.text) for item in results] == [
        ("src_osaka", "黑门市场傍晚六点前适合购买寿司。"),
    ]


def test_backfill_help_does_not_construct_runtime_settings(monkeypatch, capsys) -> None:
    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("--help must not load runtime settings")

    monkeypatch.setattr(rag_backfill, "Settings", fail_if_constructed)

    with pytest.raises(SystemExit) as exc_info:
        rag_backfill.main(["--help"])

    assert exc_info.value.code == 0
    assert "Backfill TripGuard evidence RAG index" in capsys.readouterr().out
