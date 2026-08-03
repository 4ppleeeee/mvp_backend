from app.models import SourceEvidence, TravelSource
from app.rag import build_source_document


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
