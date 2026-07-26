from pathlib import Path
import asyncio

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import LmStudioLlmClient, SourceAnalysis, TravelQuery
from app.main import create_app


class FakeLlmClient:
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
        return {
            "answer": "第 1 天可以安排表参道下午茶，再去附近街区拍照。",
            "used_source_ids": [item["source_id"] for item in contexts[:2]],
        }


class BrokenLlmClient:
    async def analyze_source(self, **_: object) -> SourceAnalysis:
        raise RuntimeError("model not available")


def make_client(tmp_path: Path, llm_client: object | None = None) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'tripguard-test.db'}",
        uploads_dir=str(tmp_path / "uploads"),
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="gemma4:latest",
    )
    app = create_app(settings=settings, llm_client=llm_client or FakeLlmClient())
    return TestClient(app)


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


def test_lm_studio_client_bounds_json_generation(monkeypatch, tmp_path: Path) -> None:
    captured_payloads: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "{\"ok\": true}"}}]}

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

    asyncio.run(LmStudioLlmClient(settings)._chat_json("输出 JSON"))

    payload = captured_payloads[0]
    assert payload["max_tokens"] == 220
    assert payload["temperature"] == 0.1
    assert payload["stream"] is False


def test_lm_studio_client_uses_openai_chat_completions(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

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
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="gemma4:latest",
    )

    result = asyncio.run(LmStudioLlmClient(settings)._chat_json("输出 JSON"))

    assert result == {"ok": True}
    assert captured["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert captured["json"] == {
        "model": "gemma4:latest",
        "messages": [
            {"role": "system", "content": "只输出严格 JSON，不要输出 Markdown。"},
            {"role": "user", "content": "输出 JSON"},
        ],
        "temperature": 0.1,
        "max_tokens": 220,
        "stream": False,
    }
