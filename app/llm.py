import json
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.schemas import Category, STANDARD_TAGS


class SourceAnalysis(BaseModel):
    is_travel_related: bool
    reason: str | None = None
    confidence: float = 0.0
    destination: str = "未知"
    category: str = Category.unknown.value
    location_name: str | None = None
    normalized_tags: list[str] = Field(default_factory=list)
    raw_tags: list[str] = Field(default_factory=list)


class TravelQuery(BaseModel):
    destination: str | None = None
    days: int | None = None
    categories: list[str] = Field(default_factory=list)
    normalized_tags: list[str] = Field(default_factory=list)
    raw_intent: str | None = None
    confidence: float = 0.0


class LmStudioLlmClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def analyze_source(self, *, title: str, body_text: str, url: str | None, source_platform: str | None) -> SourceAnalysis:
        content = (
            "请判断下面收藏是否和旅行相关，并按 JSON 输出。"
            "category 只能从 eat, drink, play, entertainment, stay, transport, unknown 中选择。"
            f"normalized_tags 只能从这个列表选择：{sorted(STANDARD_TAGS)}。"
            "不认识但有价值的标签放 raw_tags。"
            "\n\n"
            f"title: {title}\nurl: {url or ''}\nsource_platform: {source_platform or ''}\nbody_text: {body_text}"
        )
        data = await self._chat_json(content)
        return _coerce_model(SourceAnalysis, data, fallback=SourceAnalysis(is_travel_related=False, reason="llm parse failed"))

    async def parse_query(self, *, message: str) -> TravelQuery:
        content = (
            "请把用户旅行问题解析成 JSON 检索条件。"
            "categories 只能从 eat, drink, play, entertainment, stay, transport 中选择。"
            f"normalized_tags 只能从这个列表选择：{sorted(STANDARD_TAGS)}。"
            "\n\n"
            f"用户问题：{message}"
        )
        data = await self._chat_json(content)
        return _coerce_model(TravelQuery, data, fallback=TravelQuery(raw_intent=message))

    async def recommend(self, *, message: str, query: TravelQuery, contexts: list[dict]) -> dict:
        content = (
            "你是旅行规划助手。只能基于给定收藏资料生成推荐。"
            "返回 JSON：answer 为中文推荐文本，used_source_ids 为实际使用的 source_id 数组。"
            "\n\n"
            f"用户问题：{message}\n解析条件：{query.model_dump_json(ensure_ascii=False)}\n收藏资料：{json.dumps(contexts, ensure_ascii=False)}"
        )
        data = await self._chat_json(content)
        if not isinstance(data, dict):
            return {"answer": "暂时没有足够资料生成推荐。", "used_source_ids": []}
        return data

    async def _chat_json(self, content: str) -> Any:
        url = f"{self._settings.llm_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": "只输出严格 JSON，不要输出 Markdown。"},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
            "max_tokens": self._settings.llm_max_tokens,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
        return json.loads(_strip_json_fence(text))


def normalize_analysis(analysis: SourceAnalysis) -> SourceAnalysis:
    category = analysis.category if analysis.category in {item.value for item in Category} else Category.unknown.value
    normalized_tags = [tag for tag in analysis.normalized_tags if tag in STANDARD_TAGS]
    raw_tags = list(dict.fromkeys([*analysis.raw_tags, *[tag for tag in analysis.normalized_tags if tag not in STANDARD_TAGS]]))
    destination = analysis.destination.strip() if analysis.destination else "未知"
    return analysis.model_copy(
        update={
            "category": category,
            "normalized_tags": list(dict.fromkeys(normalized_tags)),
            "raw_tags": raw_tags,
            "destination": destination or "未知",
        }
    )


def normalize_query(query: TravelQuery) -> TravelQuery:
    valid_categories = {item.value for item in Category if item is not Category.unknown}
    return query.model_copy(
        update={
            "categories": [item for item in query.categories if item in valid_categories],
            "normalized_tags": [tag for tag in query.normalized_tags if tag in STANDARD_TAGS],
            "destination": query.destination.strip() if query.destination else None,
        }
    )


def _coerce_model(model_type: type[BaseModel], data: Any, *, fallback: BaseModel) -> Any:
    try:
        return model_type.model_validate(data)
    except ValidationError:
        return fallback


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped
