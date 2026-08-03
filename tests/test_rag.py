from sqlmodel import Session

from app.config import Settings
from app.db import create_db_engine, init_db
from app.models import SourceEvidence, TravelSource
from app.rag import RagIndex, backfill_sources, build_source_document


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
