from pathlib import Path

from sqlmodel import Session, select

from app.config import Settings
from app.db import create_db_engine, init_db
from app.llm import PoiDraftContent
from app.main import create_app
from app.models import PoiCrawlRecord
from app.poi_sync import PoiSyncService


def make_engine(tmp_path: Path):
    engine = create_db_engine(Settings(database_url=f"sqlite:///{tmp_path / 'poi-sync.db'}"))
    init_db(engine)
    return engine


def add_record(engine, *, crawl_task_id: str = "crawl-ok", sync_status: str = "queued") -> PoiCrawlRecord:
    with Session(engine) as session:
        record = PoiCrawlRecord(
            crawl_task_id=crawl_task_id,
            poi_id="123",
            poi_key="tencent_map:123",
            poi_name="故宫博物院",
            poi_json={"name": "故宫博物院", "city": "北京"},
            source_urls=["https://example.test/forbidden-city"],
            sync_status=sync_status,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


def get_record(engine, crawl_task_id: str) -> PoiCrawlRecord:
    with Session(engine) as session:
        return session.exec(select(PoiCrawlRecord).where(PoiCrawlRecord.crawl_task_id == crawl_task_id)).one()


def test_sync_creates_attraction_from_readable_completed_pages(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine)
    create_payloads: list[dict[str, object]] = []

    async def generate_draft(*, poi: dict[str, object], pages: list[dict[str, object]]) -> PoiDraftContent:
        assert poi["name"] == "故宫博物院"
        assert len(pages) == 1
        return PoiDraftContent(description="有证据的介绍", tags=["历史建筑"])

    def create_attraction(payload: dict[str, object]) -> dict[str, object]:
        create_payloads.append(payload)
        return {"attractionId": "attr-1"}

    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: {"sources": [{"nativeTaskId": "native-ok", "status": "succeeded"}]},
        get_pages=lambda _, offset, limit: {"pages": [{"title": "故宫", "url": "https://example.test/page", "markdown": "故宫是明清皇宫。"}]},
        generate_draft=generate_draft,
        create_attraction=create_attraction,
    )

    assert sync.run("crawl-ok") == "created"
    saved = get_record(engine, "crawl-ok")
    assert saved.sync_status == "created"
    assert saved.attraction_id == "attr-1"
    assert saved.draft_json["description"] == "有证据的介绍"
    assert create_payloads == [{
        "poiId": "123",
        "attrInfo": {
            "name": "故宫博物院",
            "cityName": "北京",
            "countryName": "中国",
            "currencyCode": "CNY",
            "description": "有证据的介绍",
            "tags": ["历史建筑"],
        },
    }]


def test_sync_persists_snake_case_attraction_id_from_upstream(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="crawl-snake")
    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: {"sources": [{"nativeTaskId": "native-ok", "status": "succeeded"}]},
        get_pages=lambda _, offset, limit: {"pages": [{"markdown": "已抓到资料"}]},
        generate_draft=lambda **_: PoiDraftContent(),
        create_attraction=lambda _: {"attraction_id": "attr-snake"},
    )

    assert sync.run("crawl-snake") == "created"
    assert get_record(engine, "crawl-snake").attraction_id == "attr-snake"


def test_sync_marks_zero_page_crawl_failed_without_creating_attraction(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="crawl-empty")
    generated: list[object] = []
    created: list[object] = []

    def generate_draft(**_: object) -> PoiDraftContent:
        generated.append(True)
        return PoiDraftContent()

    def create_attraction(payload: dict[str, object]) -> dict[str, object]:
        created.append(payload)
        return {"attractionId": "must-not-exist"}

    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: {"status": "failed", "sources": [{"nativeTaskId": "native-empty", "status": "failed"}]},
        get_pages=lambda _, offset, limit: {"pages": []},
        generate_draft=generate_draft,
        create_attraction=create_attraction,
    )

    assert sync.run("crawl-empty") == "failed"
    assert get_record(engine, "crawl-empty").sync_status == "failed"
    assert generated == []
    assert created == []


def test_sync_does_not_recreate_an_already_created_record(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="crawl-created", sync_status="created")
    create_calls: list[object] = []

    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: (_ for _ in ()).throw(AssertionError("Crawlab must not be called")),
        get_pages=lambda _, offset, limit: (_ for _ in ()).throw(AssertionError("pages must not be read")),
        generate_draft=lambda **_: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
        create_attraction=lambda payload: create_calls.append(payload),
    )

    assert sync.run("crawl-created") == "created"
    assert create_calls == []


def test_pending_crawl_ids_exclude_created_and_unknown_create_outcomes(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="crawl-queued", sync_status="queued")
    add_record(engine, crawl_task_id="crawl-crawling", sync_status="crawling")
    add_record(engine, crawl_task_id="crawl-creating", sync_status="creating")
    add_record(engine, crawl_task_id="crawl-created", sync_status="created")
    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: {},
        get_pages=lambda _, offset, limit: {},
        generate_draft=lambda **_: PoiDraftContent(),
        create_attraction=lambda _: {},
    )

    assert sync.pending_crawl_ids() == ["crawl-queued", "crawl-crawling"]


def test_sync_keeps_running_crawl_pending_when_no_native_result_exists(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="crawl-running")
    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: {"status": "running", "sources": []},
        get_pages=lambda _, offset, limit: (_ for _ in ()).throw(AssertionError("pages must not be read")),
        generate_draft=lambda **_: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
        create_attraction=lambda _: (_ for _ in ()).throw(AssertionError("create must not be called")),
    )

    assert sync.run("crawl-running") == "crawling"
    assert get_record(engine, "crawl-running").sync_status == "crawling"


def test_sync_does_not_retry_when_create_result_is_unknown(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="crawl-unknown")
    create_calls: list[object] = []

    def unknown_create(payload: dict[str, object]) -> object:
        create_calls.append(payload)
        raise TimeoutError("upstream timed out")

    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: {"sources": [{"nativeTaskId": "native-ok", "status": "succeeded"}]},
        get_pages=lambda _, offset, limit: {"pages": [{"markdown": "已抓到资料"}]},
        generate_draft=lambda **_: PoiDraftContent(description="初稿"),
        create_attraction=unknown_create,
    )

    assert sync.run("crawl-unknown") == "creating"
    saved = get_record(engine, "crawl-unknown")
    assert saved.sync_status == "creating"
    assert "unknown" in (saved.sync_error or "").lower()
    assert sync.run("crawl-unknown") == "creating"
    assert len(create_calls) == 1


def test_sync_persists_a_generation_failure_without_creating_attraction(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="crawl-llm-failed")
    creates: list[object] = []
    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: {"sources": [{"nativeTaskId": "native-ok", "status": "succeeded"}]},
        get_pages=lambda _, offset, limit: {"pages": [{"markdown": "已抓到资料"}]},
        generate_draft=lambda **_: (_ for _ in ()).throw(RuntimeError("Ollama unavailable")),
        create_attraction=lambda payload: creates.append(payload),
    )

    assert sync.run("crawl-llm-failed") == "failed"
    saved = get_record(engine, "crawl-llm-failed")
    assert saved.sync_status == "failed"
    assert saved.sync_error == "Ollama unavailable"
    assert creates == []


def test_sync_uses_readable_pages_from_every_native_source(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="crawl-multiple")
    loaded: list[dict[str, object]] = []

    async def generate_draft(*, poi: dict[str, object], pages: list[dict[str, object]]) -> PoiDraftContent:
        loaded.extend(pages)
        return PoiDraftContent(description="综合两份资料")

    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: {"status": "succeeded", "sources": [
            {"nativeTaskId": "native-a", "status": "failed"},
            {"nativeTaskId": "native-b", "status": "succeeded"},
        ]},
        get_pages=lambda native_task_id, offset, limit: {"pages": [{"markdown": f"{native_task_id} 的资料"}]},
        generate_draft=generate_draft,
        create_attraction=lambda _: {"attractionId": "attr-multiple"},
    )

    assert sync.run("crawl-multiple") == "created"
    assert [page["markdown"] for page in loaded] == ["native-a 的资料", "native-b 的资料"]


def test_sync_reads_all_result_pages_in_batches_of_ten(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="crawl-paged")
    loaded: list[dict[str, object]] = []

    async def generate_draft(*, poi: dict[str, object], pages: list[dict[str, object]]) -> PoiDraftContent:
        loaded.extend(pages)
        return PoiDraftContent()

    def get_pages(native_task_id: str, offset: int, limit: int) -> dict[str, object]:
        assert native_task_id == "native-paged"
        assert limit == 10
        return {"pages": [{"markdown": f"第 {index} 页"} for index in range(offset, 10 if offset == 0 else 11)]}

    sync = PoiSyncService(
        engine=engine,
        get_crawl=lambda _: {"sources": [{"nativeTaskId": "native-paged"}]},
        get_pages=get_pages,
        generate_draft=generate_draft,
        create_attraction=lambda _: {"attractionId": "attr-paged"},
    )

    assert sync.run("crawl-paged") == "created"
    assert len(loaded) == 11


def test_app_resumes_only_queued_and_crawling_poi_records(tmp_path: Path) -> None:
    app = create_app(Settings(
        database_url=f"sqlite:///{tmp_path / 'lifecycle.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        ingestion_temp_dir=str(tmp_path / "ingestion"),
        admin_api_enabled=True,
        crawlab_results_api_url="https://crawlab.internal",
        crawlab_api_token="crawlab-test-token",
        attraction_api_base_url="https://attraction.internal",
    ))
    add_record(app.state.engine, crawl_task_id="crawl-queued", sync_status="queued")
    add_record(app.state.engine, crawl_task_id="crawl-crawling", sync_status="crawling")
    add_record(app.state.engine, crawl_task_id="crawl-creating", sync_status="creating")
    calls: list[tuple[object, tuple[object, ...]]] = []

    class RecordingExecutor:
        def submit(self, function: object, *args: object) -> None:
            calls.append((function, args))

    app.state.poi_sync_executor = RecordingExecutor()
    app.state.resume_poi_syncs()

    assert [args for _, args in calls] == [("crawl-queued",), ("crawl-crawling",)]
