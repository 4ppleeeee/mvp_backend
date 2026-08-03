from pathlib import Path
import asyncio
import threading
import time

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.llm import OllamaLlmClient, SourceAnalysis, TravelQuery, normalize_analysis
from app.main import create_app
from app.rag import RetrievedEvidence


class FakeLlmClient:
    def __init__(self) -> None:
        self.contexts: list[dict] = []

    async def analyze_source(self, *, title: str, body_text: str, url: str | None, source_platform: str | None) -> SourceAnalysis:
        if "股票" in body_text:
            return SourceAnalysis(
                is_travel_related=False,
                reason="not travel related",
                confidence=0.91,
            )
        return SourceAnalysis(
            is_travel_related=True,
            reason="travel food note",
            confidence=0.93,
            destination="东京",
            category="eat",
            location_name="表参道",
            normalized_tags=["拍照好看", "甜品", "排队"],
            raw_tags=["出片", "下午茶"],
        )

    async def parse_query(self, *, message: str) -> TravelQuery:
        return TravelQuery(
            destination="东京",
            days=3,
            categories=["eat", "play"],
            normalized_tags=["拍照好看", "甜品"],
            raw_intent=message,
            confidence=0.88,
        )

    async def recommend(self, *, message: str, query: TravelQuery, contexts: list[dict]) -> dict:
        self.contexts = contexts
        return {
            "answer": "第 1 天可以安排表参道下午茶，再去附近街区拍照。",
            "used_source_ids": [item["source_id"] for item in contexts[:2]],
        }

    async def analyze_image(self, *, image_base64: str, title_hint: str | None = None) -> SourceAnalysis:
        return SourceAnalysis(
            is_travel_related=True,
            reason="travel image note",
            confidence=0.9,
            title="上海武康路咖啡路线",
            body_text="武康路、安福路、咖啡和甜品 citywalk 路线。",
            destination="上海",
            category="eat",
            location_name="武康路",
            normalized_tags=["咖啡", "甜品", "拍照好看"],
            raw_tags=["citywalk"],
        )


class BrokenLlmClient:
    async def analyze_source(self, **_: object) -> SourceAnalysis:
        raise RuntimeError("model not available")


class RecordingRagIndex:
    def __init__(self) -> None:
        self.indexed: list[tuple[str, str, str]] = []

    def upsert_source(self, source: object, evidence: object) -> None:
        self.indexed.append((source.source_id, evidence.evidence_id, evidence.full_text))


class RetrievedEvidenceRagIndex:
    def __init__(self, text: str) -> None:
        self.text = text
        self.queries: list[tuple[str, set[str]]] = []

    def upsert_source(self, source: object, evidence: object) -> None:
        return None

    def retrieve(self, query: str, *, allowed_source_ids: set[str]) -> list[RetrievedEvidence]:
        self.queries.append((query, allowed_source_ids))
        return [
            RetrievedEvidence(
                source_id=next(iter(allowed_source_ids)),
                evidence_id="evd_test_evidence",
                text=self.text,
                score=1.0,
                segment_index=4,
                start_seconds=12.5,
                end_seconds=27.0,
            )
        ]


class MixedSourceRetrievedEvidenceRagIndex:
    def upsert_source(self, source: object, evidence: object) -> None:
        return None

    def retrieve(self, query: str, *, allowed_source_ids: set[str]) -> list[RetrievedEvidence]:
        return [
            RetrievedEvidence(
                source_id=next(iter(allowed_source_ids)),
                evidence_id="evd_valid",
                text="不应使用的有效检索片段。",
                score=1.0,
            ),
            RetrievedEvidence(
                source_id="src_not_sql_candidate",
                evidence_id="evd_invalid",
                text="不应使用的越界检索片段。",
                score=0.9,
            ),
        ]


class BlockingRetrievedEvidenceRagIndex:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.completed = threading.Event()
        self.retrieve_thread_id: int | None = None

    def retrieve(self, query: str, *, allowed_source_ids: set[str]) -> list[RetrievedEvidence]:
        self.retrieve_thread_id = threading.get_ident()
        self.entered.set()
        time.sleep(0.15)
        self.completed.set()
        return [
            RetrievedEvidence(
                source_id=next(iter(allowed_source_ids)),
                evidence_id="evd_blocking",
                text="线程池中的检索片段。",
                score=1.0,
            )
        ]


def make_app(
    tmp_path: Path,
    llm_client: object | None = None,
    rag_index: object | None = None,
) -> object:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tripguard-test.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="gemma4:latest",
    )
    return create_app(settings=settings, llm_client=llm_client or FakeLlmClient(), rag_index=rag_index)


def make_client(
    tmp_path: Path,
    llm_client: object | None = None,
    rag_index: object | None = None,
) -> TestClient:
    return TestClient(make_app(tmp_path, llm_client=llm_client, rag_index=rag_index))


def test_health_reports_backend_and_model(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "tripguard-mvp-backend",
        "llm_model": "gemma4:latest",
    }


def test_client_config_exposes_public_api_base_url(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tripguard-test.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="gemma4:latest",
        public_base_url="https://trip.aatroxli.site:1221",
    )
    app = create_app(settings=settings, llm_client=FakeLlmClient())
    client = TestClient(app)

    response = client.get("/client/config")

    assert response.status_code == 200
    assert response.json() == {
        "api_base_url": "https://trip.aatroxli.site:1221",
        "service": "tripguard-mvp-backend",
        "llm_model": "gemma4:latest",
    }


def test_analyze_source_returns_card_metadata_without_saving(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.post(
        "/sources/analyze",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/example",
            "title": "东京表参道超好吃的舒芙蕾松饼",
            "body_text": "这家店在表参道附近，很出片，适合下午茶，排队大概 30 分钟。",
            "source_platform": "xhs",
            "cover_image_url": "https://img.example/cover.jpg",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_travel_related"] is True
    assert payload["destination"] == "东京"
    assert payload["category"] == "eat"
    assert payload["location_name"] == "表参道"
    assert payload["normalized_tags"] == ["拍照好看", "甜品", "排队"]
    assert payload["raw_tags"] == ["出片", "下午茶"]
    assert client.get("/sources").json()["items"] == []


def test_analyze_source_requests_a_bounded_summary_instead_of_echoing_long_transcript(tmp_path: Path, monkeypatch) -> None:
    client = OllamaLlmClient(Settings(database_url=f"sqlite:///{tmp_path / 'tripguard-test.db'}"))
    captured: dict[str, str] = {}

    async def fake_chat_json(content: str, *, images: list[str] | None = None) -> dict[str, object]:
        captured["content"] = content
        return {
            "is_travel_related": True,
            "destination": "京都",
            "category": "play",
            "body_text": "伏见稻荷大社的交通与游玩建议。",
        }

    monkeypatch.setattr(client, "_chat_json", fake_chat_json)

    analysis = asyncio.run(
        client.analyze_source(
            title="京都伏见稻荷大社攻略",
            body_text="交通与游玩建议。" * 1000,
            url="https://www.youtube.com/watch?v=OKFijPl39bY",
            source_platform="youtube",
        )
    )

    assert analysis.is_travel_related is True
    assert "body_text 使用输入 body_text 原文" not in captured["content"]
    assert "body_text 用不超过 600 个中文字符的旅行资料摘要" in captured["content"]


def test_analyze_image_returns_ocr_card_metadata_without_saving(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.post(
        "/sources/analyze-image",
        json={
            "input_type": "image",
            "image_base64": "ZmFrZS1pbWFnZQ==",
            "title_hint": "长图分享",
            "source_platform": "image",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_travel_related"] is True
    assert payload["title"] == "上海武康路咖啡路线"
    assert payload["body_text"] == "武康路、安福路、咖啡和甜品 citywalk 路线。"
    assert payload["destination"] == "上海"
    assert payload["category"] == "eat"
    assert payload["location_name"] == "武康路"
    assert payload["normalized_tags"] == ["咖啡", "甜品", "拍照好看"]
    assert payload["raw_tags"] == ["citywalk"]
    assert client.get("/sources").json()["items"] == []


def test_source_analysis_accepts_string_tags_from_model() -> None:
    analysis = SourceAnalysis.model_validate(
        {
            "is_travel_related": True,
            "confidence": 1.0,
            "destination": "东京",
            "category": "eat",
            "normalized_tags": "拍照好看, 咖啡, 排队",
            "raw_tags": "下午茶, 舒芙蕾松饼, 表参道",
        }
    )

    assert analysis.normalized_tags == ["拍照好看", "咖啡", "排队"]
    assert analysis.raw_tags == ["下午茶", "舒芙蕾松饼", "表参道"]


def test_normalize_analysis_rejects_travel_without_destination_or_category() -> None:
    analysis = normalize_analysis(
        SourceAnalysis(
            is_travel_related=True,
            reason="looks like travel but no destination",
            confidence=0.8,
            destination="未知",
            category="unknown",
            normalized_tags=["拍照好看"],
        )
    )

    assert analysis.is_travel_related is False
    assert analysis.reason == "missing destination or category"


def test_normalize_analysis_maps_unknown_scenic_category_to_play() -> None:
    analysis = normalize_analysis(
        SourceAnalysis(
            is_travel_related=True,
            reason="park travel note",
            confidence=0.98,
            destination="北京",
            category="unknown",
            location_name="北海公园",
            normalized_tags=["公园", "拍照好看", "情侣", "朋友"],
            raw_tags=["花海", "园林游玩"],
        )
    )

    assert analysis.is_travel_related is True
    assert analysis.destination == "北京"
    assert analysis.category == "play"
    assert analysis.normalized_tags == ["公园", "拍照好看", "情侣", "朋友"]


def test_collect_travel_source_creates_card_without_exposing_body_text(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/example",
            "title": "东京表参道超好吃的舒芙蕾松饼",
            "body_text": "这家店在表参道附近，很出片，适合下午茶，排队大概 30 分钟。",
            "source_platform": "xhs",
            "cover_image_url": "https://img.example/cover.jpg",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["saved"] is True
    assert payload["source"]["title"] == "东京表参道超好吃的舒芙蕾松饼"
    assert payload["source"]["destination"] == "东京"
    assert payload["source"]["category"] == "eat"
    assert payload["source"]["normalized_tags"] == ["拍照好看", "甜品", "排队"]
    assert "body_text" not in payload["source"]

    list_response = client.get("/sources")
    assert list_response.status_code == 200
    assert len(list_response.json()["items"]) == 1


def test_collect_image_source_creates_backend_card(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.post(
        "/sources/collect-image",
        json={
            "input_type": "image",
            "image_base64": "ZmFrZS1pbWFnZQ==",
            "title_hint": "长图分享",
            "source_platform": "image",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["saved"] is True
    assert payload["source"]["title"] == "上海武康路咖啡路线"
    assert payload["source"]["original_url"] is None
    assert payload["source"]["source_platform"] == "image"
    assert payload["source"]["destination"] == "上海"
    assert payload["source"]["category"] == "eat"
    assert payload["source"]["location_name"] == "武康路"
    assert payload["source"]["normalized_tags"] == ["咖啡", "甜品", "拍照好看"]
    assert payload["source"]["raw_tags"] == ["citywalk"]

    list_response = client.get("/sources")
    assert list_response.status_code == 200
    assert len(list_response.json()["items"]) == 1
    source_id = payload["source"]["source_id"]
    detail_response = client.get(f"/sources/{source_id}")
    assert detail_response.status_code == 200
    assert detail_response.json()["body_text"] == "武康路、安福路、咖啡和甜品 citywalk 路线。"


def test_collect_non_travel_source_does_not_save(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://example.com/stock",
            "title": "今日股票复盘",
            "body_text": "这篇内容讨论股票交易和量化策略。",
            "source_platform": "web",
        },
    )

    assert response.status_code == 200
    assert response.json()["saved"] is False
    assert response.json()["reason"] == "not travel related"
    assert client.get("/sources").json()["items"] == []


def test_collect_returns_503_when_llm_is_unavailable(tmp_path: Path) -> None:
    client = make_client(tmp_path, BrokenLlmClient())

    response = client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/example",
            "title": "东京表参道超好吃的舒芙蕾松饼",
            "body_text": "这家店在表参道附近，很出片，适合下午茶。",
            "source_platform": "xhs",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "llm unavailable"


def test_recommend_uses_saved_sources_as_trusted_citations(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/example",
            "title": "东京表参道超好吃的舒芙蕾松饼",
            "body_text": "这家店在表参道附近，很出片，适合下午茶，排队大概 30 分钟。",
            "source_platform": "xhs",
            "cover_image_url": "https://img.example/cover.jpg",
        },
    )

    response = client.post("/chat/recommend", json={"message": "东京 3 天想逛吃拍照，别太游客"})

    assert response.status_code == 200
    payload = response.json()
    assert "表参道下午茶" in payload["answer"]
    assert payload["used_sources"][0]["title"] == "东京表参道超好吃的舒芙蕾松饼"
    assert payload["used_sources"][0]["original_url"] == "https://xhslink.com/example"
    assert payload["used_sources"][0]["source_id"]


def test_chat_returns_itinerary_place_and_evidence_events(tmp_path: Path) -> None:
    rag_index = RetrievedEvidenceRagIndex(text="表参道下午茶需要排队。")
    client = make_client(tmp_path, rag_index=rag_index)
    saved = client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/example",
            "title": "东京表参道咖啡攻略",
            "body_text": "这是一篇表参道下午茶攻略。",
            "source_platform": "xhs",
        },
    )
    source_id = saved.json()["source"]["source_id"]

    response = client.post("/chat", json={"message": "东京下午茶怎么安排"})

    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["type"] for event in events] == [
        "assistant_text",
        "itinerary_card",
        "place_card",
        "evidence_card",
    ]
    assert events[1]["grounding"]["kind"] == "suggestion"
    assert events[1]["actions"][0] == {
        "action_id": "add_itinerary",
        "label": "加入当前行程",
        "kind": "local",
        "payload": {"slot_ids": [events[1]["slots"][0]["slot_id"]]},
    }
    assert events[2]["grounding"] == {
        "kind": "knowledge_base",
        "source_id": source_id,
        "evidence_id": "evd_test_evidence",
        "segment_index": 4,
        "start_seconds": 12.5,
        "end_seconds": 27.0,
    }
    assert events[3]["excerpt"] == "表参道下午茶需要排队。"


def test_chat_refresh_action_excludes_the_current_place(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    first = client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/first",
            "title": "东京第一家咖啡店",
            "body_text": "第一家表参道咖啡店。",
            "source_platform": "xhs",
        },
    ).json()["source"]
    second = client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/second",
            "title": "东京第二家咖啡店",
            "body_text": "第二家表参道咖啡店。",
            "source_platform": "xhs",
        },
    ).json()["source"]

    response = client.post(
        "/chat/action",
        json={
            "message": "东京下午茶怎么安排",
            "event_id": f"place:{first['source_id']}",
            "action_id": "refresh_places",
            "payload": {"exclude_event_ids": [f"place:{first['source_id']}"]},
        },
    )

    assert response.status_code == 200
    place_events = [event for event in response.json()["events"] if event["type"] == "place_card"]
    assert [event["event_id"] for event in place_events] == [f"place:{second['source_id']}"]


def test_recommend_passes_retrieved_evidence_not_whole_source(tmp_path: Path) -> None:
    llm = FakeLlmClient()
    rag_index = RetrievedEvidenceRagIndex(text="表参道下午茶需要排队。")
    client = make_client(tmp_path, llm_client=llm, rag_index=rag_index)
    saved = client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/example",
            "title": "东京表参道咖啡攻略",
            "body_text": "这是不应传给推荐模型的完整原始资料。",
            "source_platform": "xhs",
        },
    )
    source_id = saved.json()["source"]["source_id"]

    response = client.post("/chat/recommend", json={"message": "东京下午茶"})

    assert response.status_code == 200
    assert rag_index.queries == [("东京下午茶", {source_id})]
    assert llm.contexts[0]["body_text"] == "表参道下午茶需要排队。"
    assert llm.contexts[0]["segment_index"] == 4
    assert llm.contexts[0]["start_seconds"] == 12.5
    assert llm.contexts[0]["end_seconds"] == 27.0
    assert response.json()["used_sources"][0]["source_id"] == source_id


def test_recommend_runs_blocking_rag_retrieval_off_the_event_loop(tmp_path: Path) -> None:
    async def exercise() -> None:
        llm = FakeLlmClient()
        app = make_app(tmp_path, llm_client=llm)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            saved = await client.post(
                "/sources/collect",
                json={
                    "input_type": "url",
                    "url": "https://xhslink.com/example",
                    "title": "东京表参道咖啡攻略",
                    "body_text": "SQL 候选资料的完整正文。",
                    "source_platform": "xhs",
                },
            )
            assert saved.status_code == 201

            rag_index = BlockingRetrievedEvidenceRagIndex()
            app.state.rag_index = rag_index
            event_loop_progressed = asyncio.Event()

            async def observe_event_loop() -> None:
                while not rag_index.entered.is_set():
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
                event_loop_progressed.set()

            response_task = asyncio.create_task(client.post("/chat/recommend", json={"message": "东京下午茶"}))
            sentinel_task = asyncio.create_task(observe_event_loop())
            await asyncio.to_thread(rag_index.entered.wait)
            await event_loop_progressed.wait()

            assert not rag_index.completed.is_set()
            response = await response_task
            await sentinel_task
            assert response.status_code == 200
            assert rag_index.retrieve_thread_id != threading.get_ident()

    asyncio.run(exercise())


def test_recommend_falls_back_when_retrieval_contains_outside_sql_candidate(tmp_path: Path) -> None:
    llm = FakeLlmClient()
    client = make_client(tmp_path, llm_client=llm, rag_index=MixedSourceRetrievedEvidenceRagIndex())
    saved = client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/example",
            "title": "东京表参道咖啡攻略",
            "body_text": "SQL 候选资料的完整正文。",
            "source_platform": "xhs",
        },
    )

    response = client.post("/chat/recommend", json={"message": "东京下午茶"})

    assert response.status_code == 200
    assert llm.contexts[0]["body_text"] == "SQL 候选资料的完整正文。"
    assert response.json()["used_sources"][0]["source_id"] == saved.json()["source"]["source_id"]


def test_collect_syncs_saved_source_to_rag(tmp_path: Path) -> None:
    rag_index = RecordingRagIndex()
    client = make_client(tmp_path, rag_index=rag_index)

    response = client.post(
        "/sources/collect",
        json={
            "input_type": "url",
            "url": "https://xhslink.com/example",
            "title": "东京表参道咖啡攻略",
            "body_text": "表参道的咖啡店下午需要排队。",
            "source_platform": "xhs",
        },
    )

    assert response.status_code == 201
    source_id = response.json()["source"]["source_id"]
    assert len(rag_index.indexed) == 1
    assert rag_index.indexed[0][0] == source_id
    assert rag_index.indexed[0][2] == "表参道的咖啡店下午需要排队。"


def test_ollama_client_bounds_json_generation(monkeypatch, tmp_path: Path) -> None:
    captured_payloads: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"message": {"content": "{\"ok\": true}"}}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, json: dict) -> FakeResponse:
            captured_payloads.append(json)
            return FakeResponse()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tripguard-test.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="gemma4:latest",
    )

    asyncio.run(OllamaLlmClient(settings)._chat_json("输出 JSON"))

    payload = captured_payloads[0]
    assert payload["think"] is False
    assert payload["stream"] is False
    assert payload["format"] == "json"
    assert payload["options"] == {
        "temperature": 0.1,
        "num_predict": 220,
    }


def test_ollama_client_uses_native_chat_and_disables_thinking(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"message": {"content": '```json\n{"ok": true}\n```'}}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, json: dict) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tripguard-test.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        llm_base_url="http://127.0.0.1:11434",
        llm_model="gemma4:latest",
    )

    result = asyncio.run(OllamaLlmClient(settings)._chat_json("输出 JSON"))

    assert result == {"ok": True}
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["json"] == {
        "model": "gemma4:latest",
        "messages": [
            {"role": "system", "content": "只输出严格 JSON，不要输出 Markdown。"},
            {"role": "user", "content": "输出 JSON"},
        ],
        "stream": False,
        "think": False,
        "format": "json",
        "options": {
            "temperature": 0.1,
            "num_predict": 220,
        },
    }


def test_ollama_client_retries_invalid_json_and_returns_parsed_object(monkeypatch, tmp_path: Path) -> None:
    responses = iter([
        '{"title": "截断',
        '{"title": "重试成功"}',
    ])
    captured_contents: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"message": {"content": next(responses)}}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, json: dict) -> FakeResponse:
            captured_contents.append(json["messages"][-1]["content"])
            return FakeResponse()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tripguard-test.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        llm_base_url="http://127.0.0.1:11434",
        llm_model="gemma4:latest",
    )

    result = asyncio.run(OllamaLlmClient(settings)._chat_json("输出 JSON"))

    assert result == {"title": "重试成功"}
    assert len(captured_contents) == 2
    assert "只输出一个完整、合法且可解析的 JSON 对象" in captured_contents[1]
