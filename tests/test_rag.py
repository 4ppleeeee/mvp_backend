import os
from contextlib import contextmanager
from multiprocessing import get_context
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Thread

import pytest
from llama_index.core.embeddings import BaseEmbedding, MockEmbedding
from sqlmodel import Session

from app.config import Settings
from app.db import create_db_engine, init_db
from app.models import SourceEvidence, TravelSource
from app.rag import RagIndex, backfill_sources, build_source_document, build_source_nodes
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


def test_dense_transcript_segments_are_compacted_with_first_timecode_provenance() -> None:
    source = TravelSource(
        source_id="src_dense_video",
        title="高密度视频转写",
        body_text="完整转写。",
        destination="东京",
        category="play",
    )
    evidence = SourceEvidence(
        evidence_id="evd_dense_video",
        source_id=source.source_id,
        full_text="完整转写。",
        segments=[
            {
                "start_seconds": index * 2,
                "end_seconds": index * 2 + 2,
                "text": f"第 {index} 段旅行信息。" * 20,
            }
            for index in range(65)
        ],
    )

    nodes = build_source_nodes(source, evidence)

    assert len(nodes) < 65
    assert nodes[0].metadata["segment_index"] == 0
    assert nodes[0].metadata["start_seconds"] == 0
    assert nodes[-1].metadata["end_seconds"] == 130
    assert "第 0 段旅行信息。" in nodes[0].text
    assert "第 64 段旅行信息。" in nodes[-1].text


class BlockingQueryEmbedding(BaseEmbedding):
    """Test-only embedding that holds the retriever after its snapshot is loaded."""

    embed_dim: int

    def __init__(self, *, query_started: Event, allow_query_to_finish: Event) -> None:
        super().__init__(embed_dim=2)
        self._query_started = query_started
        self._allow_query_to_finish = allow_query_to_finish

    @classmethod
    def class_name(cls) -> str:
        return "BlockingQueryEmbedding"

    def _get_query_embedding(self, query: str) -> list[float]:
        self._query_started.set()
        if not self._allow_query_to_finish.wait(timeout=5):
            raise TimeoutError("test did not release query embedding")
        return [1.0, 0.0]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return [1.0, 0.0]

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)


class ProcessLoadBarrierRagIndex(RagIndex):
    """Test-only index that makes unsafe concurrent snapshot loads deterministic."""

    def __init__(self, *, load_barrier, use_file_lock: bool, **kwargs) -> None:
        super().__init__(**kwargs)
        self._load_barrier = load_barrier
        self._use_file_lock = use_file_lock

    def _load_index(self):
        snapshot = super()._load_index()
        try:
            self._load_barrier.wait(timeout=0.2)
        except BrokenBarrierError:
            # A process-wide lock prevents the second writer reaching this
            # test-only gate; continuing is then the expected safe path.
            pass
        return snapshot

    @contextmanager
    def _locked(self):
        if self._use_file_lock:
            with super()._locked():
                yield
        else:
            # Used only by the explicit red-proof run below; the normal test
            # delegates to production's process and thread locking.
            with self._persist_lock:
                yield


def upsert_source_in_process(
    persist_dir: str,
    source_id: str,
    evidence_id: str,
    text: str,
    load_barrier,
    ready,
    start,
) -> None:
    index = ProcessLoadBarrierRagIndex(
        persist_dir=Path(persist_dir),
        embedding_model=MockEmbedding(embed_dim=8),
        top_k=6,
        load_barrier=load_barrier,
        use_file_lock=os.environ.get("TRIPGUARD_TEST_DISABLE_FILE_LOCK") != "1",
    )
    source = TravelSource(
        source_id=source_id,
        title=source_id,
        body_text=text,
        destination="东京",
        category="eat",
    )
    evidence = SourceEvidence(
        evidence_id=evidence_id,
        source_id=source_id,
        origin="article",
        full_text=text,
    )
    ready.set()
    if not start.wait(timeout=5):
        raise RuntimeError("parent did not release process")
    index.upsert_source(source, evidence)


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


def test_legacy_evidence_with_null_segments_falls_back_to_full_text(tmp_path) -> None:
    source = TravelSource(
        source_id="src_legacy_null_segments",
        title="旧资料",
        body_text="旧资料完整正文。",
        destination="东京",
        category="eat",
    )
    evidence = SourceEvidence(
        evidence_id="evd_legacy_null_segments",
        source_id=source.source_id,
        origin="article",
        full_text="旧资料完整正文。",
    )
    evidence.segments = None

    index = RagIndex.for_test(tmp_path)
    index.upsert_source(source, evidence)

    results = index.retrieve("旧资料", allowed_source_ids={source.source_id})

    assert [(item.evidence_id, item.text, item.segment_index) for item in results] == [
        ("evd_legacy_null_segments", "旧资料完整正文。", None),
    ]


def test_segment_nodes_keep_timecode_provenance_replace_source_and_survive_reload(tmp_path) -> None:
    source = TravelSource(
        source_id="src_video_tokyo",
        title="东京咖啡探店视频",
        body_text="完整视频转录。",
        destination="东京",
        category="drink",
    )
    old_evidence = SourceEvidence(
        evidence_id="evd_video_old",
        source_id=source.source_id,
        origin="asr",
        language="zh",
        full_text="完整视频转录。",
        segments=[
            {"start_seconds": 12.5, "end_seconds": 18.0, "text": "表参道咖啡店十点开门。"},
            {"start_seconds": 18.0, "end_seconds": 25.25, "text": "周末建议提前取号。"},
            {"start_seconds": 25.25, "end_seconds": 26.0, "text": "   "},
        ],
    )
    replacement_evidence = SourceEvidence(
        evidence_id="evd_video_new",
        source_id=source.source_id,
        origin="asr",
        language="zh",
        full_text="更新后的完整视频转录。",
        segments=[
            {"start_seconds": 30.0, "end_seconds": 36.0, "text": "更新后只保留这一段。"},
        ],
    )
    index = RagIndex.for_test(tmp_path)

    index.upsert_source(source, old_evidence)

    retrieved = index.retrieve("东京咖啡排队", allowed_source_ids={source.source_id})
    assert {
        (
            item.source_id,
            item.evidence_id,
            item.text,
            item.segment_index,
            item.start_seconds,
            item.end_seconds,
        )
        for item in retrieved
    } == {
        (source.source_id, old_evidence.evidence_id, "表参道咖啡店十点开门。", 0, 12.5, 18.0),
        (source.source_id, old_evidence.evidence_id, "周末建议提前取号。", 1, 18.0, 25.25),
    }

    index.upsert_source(source, replacement_evidence)

    reloaded = RagIndex.for_test(tmp_path)
    replacement_results = reloaded.retrieve("东京咖啡排队", allowed_source_ids={source.source_id})
    assert [
        (
            item.evidence_id,
            item.text,
            item.segment_index,
            item.start_seconds,
            item.end_seconds,
        )
        for item in replacement_results
    ] == [(replacement_evidence.evidence_id, "更新后只保留这一段。", 0, 30.0, 36.0)]


def test_concurrent_upserts_preserve_all_sources_after_reload(tmp_path, monkeypatch) -> None:
    """Two writers that load the same snapshot must not overwrite one another."""
    index = RagIndex.for_test(tmp_path)
    source_a = TravelSource(
        source_id="src_concurrent_a",
        title="并发资料 A",
        body_text="A",
        destination="东京",
        category="eat",
    )
    source_b = TravelSource(
        source_id="src_concurrent_b",
        title="并发资料 B",
        body_text="B",
        destination="东京",
        category="eat",
    )
    evidence_a = SourceEvidence(
        evidence_id="evd_concurrent_a",
        source_id=source_a.source_id,
        origin="article",
        full_text="并发写入资料 A。",
    )
    evidence_b = SourceEvidence(
        evidence_id="evd_concurrent_b",
        source_id=source_b.source_id,
        origin="article",
        full_text="并发写入资料 B。",
    )
    both_writers_ready = Barrier(2)
    original_load_index = index._load_index

    def synchronized_load_index():
        try:
            both_writers_ready.wait(timeout=0.2)
        except BrokenBarrierError:
            # With the lock in place, the second writer cannot enter this hook
            # until the first transaction has persisted. That is the expected
            # serialized path, so let the timed-out first writer proceed.
            pass
        return original_load_index()

    monkeypatch.setattr(index, "_load_index", synchronized_load_index)
    errors: list[BaseException] = []
    writers_ready = [Event(), Event()]
    start_writers = Event()

    def upsert(source: TravelSource, evidence: SourceEvidence, ready: Event) -> None:
        try:
            ready.set()
            assert start_writers.wait(timeout=1)
            index.upsert_source(source, evidence)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    writers = [
        Thread(target=upsert, args=(source_a, evidence_a, writers_ready[0])),
        Thread(target=upsert, args=(source_b, evidence_b, writers_ready[1])),
    ]
    for writer in writers:
        writer.start()
    assert all(ready.wait(timeout=1) for ready in writers_ready)
    start_writers.set()
    for writer in writers:
        writer.join(timeout=5)

    assert not any(writer.is_alive() for writer in writers)
    assert errors == []

    reloaded = RagIndex.for_test(tmp_path)
    results = reloaded.retrieve(
        "并发写入资料",
        allowed_source_ids={source_a.source_id, source_b.source_id},
    )

    assert {(item.source_id, item.evidence_id, item.text) for item in results} == {
        (source_a.source_id, evidence_a.evidence_id, evidence_a.full_text),
        (source_b.source_id, evidence_b.evidence_id, evidence_b.full_text),
    }


def test_multiprocess_upserts_preserve_all_sources_after_reload(tmp_path) -> None:
    context = get_context("spawn")
    load_barrier = context.Barrier(2)
    start = context.Event()
    ready_events = [context.Event(), context.Event()]
    sources = [
        ("src_process_a", "evd_process_a", "跨进程写入资料 A。"),
        ("src_process_b", "evd_process_b", "跨进程写入资料 B。"),
    ]
    workers = [
        context.Process(
            target=upsert_source_in_process,
            args=(str(tmp_path), *source, load_barrier, ready, start),
        )
        for source, ready in zip(sources, ready_events, strict=True)
    ]
    for worker in workers:
        worker.start()
    assert all(ready.wait(timeout=5) for ready in ready_events)
    start.set()
    for worker in workers:
        worker.join(timeout=10)

    assert not any(worker.is_alive() for worker in workers)
    assert [worker.exitcode for worker in workers] == [0, 0]

    reloaded = RagIndex.for_test(tmp_path)
    results = reloaded.retrieve(
        "跨进程写入资料",
        allowed_source_ids={source_id for source_id, _, _ in sources},
    )

    assert {(item.source_id, item.evidence_id, item.text) for item in results} == set(sources)


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


def test_retrieve_releases_persist_lock_before_expensive_query_embedding(tmp_path) -> None:
    query_started = Event()
    allow_query_to_finish = Event()
    index = RagIndex(
        persist_dir=tmp_path,
        embedding_model=BlockingQueryEmbedding(
            query_started=query_started,
            allow_query_to_finish=allow_query_to_finish,
        ),
        top_k=6,
    )
    existing_source = TravelSource(
        source_id="src_snapshot",
        title="检索快照资料",
        body_text="初始内容",
        destination="东京",
        category="eat",
    )
    existing_evidence = SourceEvidence(
        evidence_id="evd_snapshot",
        source_id=existing_source.source_id,
        origin="article",
        full_text="检索期间仍应返回的快照内容。",
    )
    index.upsert_source(existing_source, existing_evidence)

    retrieved: list = []
    retrieval_errors: list[BaseException] = []

    def retrieve() -> None:
        try:
            retrieved.extend(index.retrieve("检索快照", allowed_source_ids={existing_source.source_id}))
        except BaseException as error:  # pragma: no cover - asserted below
            retrieval_errors.append(error)

    reader = Thread(target=retrieve)
    reader.start()
    assert query_started.wait(timeout=1)

    new_source = TravelSource(
        source_id="src_writer",
        title="并行写入资料",
        body_text="新增内容",
        destination="东京",
        category="eat",
    )
    new_evidence = SourceEvidence(
        evidence_id="evd_writer",
        source_id=new_source.source_id,
        origin="article",
        full_text="检索嵌入缓慢时也必须完成的写入。",
    )
    write_finished = Event()
    write_errors: list[BaseException] = []

    def upsert() -> None:
        try:
            index.upsert_source(new_source, new_evidence)
            write_finished.set()
        except BaseException as error:  # pragma: no cover - asserted below
            write_errors.append(error)

    writer = Thread(target=upsert)
    writer.start()

    # The old implementation kept this lock while `_get_query_embedding` waited.
    assert write_finished.wait(timeout=1)

    allow_query_to_finish.set()
    reader.join(timeout=5)
    writer.join(timeout=5)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert retrieval_errors == []
    assert write_errors == []
    assert [(item.source_id, item.evidence_id, item.text) for item in retrieved] == [
        (existing_source.source_id, existing_evidence.evidence_id, existing_evidence.full_text),
    ]

    reloaded = RagIndex(
        persist_dir=tmp_path,
        embedding_model=MockEmbedding(embed_dim=2),
        top_k=6,
    )
    persisted = reloaded.retrieve(
        "并行写入资料",
        allowed_source_ids={existing_source.source_id, new_source.source_id},
    )
    assert {item.source_id for item in persisted} == {existing_source.source_id, new_source.source_id}


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
