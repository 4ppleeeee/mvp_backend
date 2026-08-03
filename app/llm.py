import json
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import Settings
from app.schemas import Category, ChatUiResponse, STANDARD_TAGS


class SourceAnalysis(BaseModel):
    is_travel_related: bool
    reason: str | None = None
    confidence: float = 0.0
    title: str | None = None
    body_text: str | None = None
    destination: str = "未知"
    category: str = Category.unknown.value
    location_name: str | None = None
    normalized_tags: list[str] = Field(default_factory=list)
    raw_tags: list[str] = Field(default_factory=list)

    @field_validator("normalized_tags", "raw_tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            normalized = value
            for separator in ["，", "、", ";", "；", "\n"]:
                normalized = normalized.replace(separator, ",")
            return [item.strip() for item in normalized.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return value


class TravelQuery(BaseModel):
    destination: str | None = None
    days: int | None = None
    categories: list[str] = Field(default_factory=list)
    normalized_tags: list[str] = Field(default_factory=list)
    raw_intent: str | None = None
    confidence: float = 0.0


class OllamaLlmClient:
    _JSON_RETRY_INSTRUCTION = (
        "\n\n上一次响应不是合法 JSON。请重新生成，只输出一个完整、合法且可解析的 JSON 对象，"
        "不要截断，不要 Markdown，不要任何额外文字。"
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def analyze_source(self, *, title: str, body_text: str, url: str | None, source_platform: str | None) -> SourceAnalysis:
        content = (
            "请判断下面收藏是否和旅行相关，并只输出一个 JSON 对象。"
            "必须包含字段：is_travel_related, reason, confidence, title, body_text, destination, category, location_name, normalized_tags, raw_tags。"
            "destination 必须尽力从标题、正文、链接平台里提取城市/国家/地区；确实无法判断才填 未知。"
            "location_name 填店名、景点名、商圈或街区名；没有则填 null。"
            "category 只能从 eat, drink, play, entertainment, stay, transport, unknown 中选择；公园、景点、citywalk、路线、拍照打卡、展览、寺庙、博物馆归为 play。"
            f"normalized_tags 只能从这个列表选择：{sorted(STANDARD_TAGS)}。"
            "raw_tags 放模型生成的有价值标签，比如菜系、口味、玩法、商圈、人群、季节。"
            "title 使用输入 title 原文；body_text 用不超过 600 个中文字符的旅行资料摘要，"
            "保留目的地、地点、交通、路线、时间、预约、避坑和推荐理由等可用于后续推荐的关键信息，不要逐字复述原文。"
            "\n\n"
            f"title: {title}\nurl: {url or ''}\nsource_platform: {source_platform or ''}\nbody_text: {body_text}"
        )
        data = await self._chat_json(content)
        return _coerce_model(SourceAnalysis, data, fallback=SourceAnalysis(is_travel_related=False, reason="llm parse failed"))

    async def analyze_image(self, *, image_base64: str, title_hint: str | None = None) -> SourceAnalysis:
        content = (
            "请识别这张长图是否和旅行相关，并只输出一个 JSON 对象。"
            "必须包含字段：is_travel_related, reason, confidence, title, body_text, destination, category, location_name, normalized_tags, raw_tags。"
            "title 必须使用图片里的原标题；如果没有明确标题，使用最像标题的一行文字。"
            "body_text 填 OCR 识别到的正文全文。"
            "destination 必须尽力提取城市/国家/地区；无法提取则 is_travel_related=false。"
            "category 只能从 eat, drink, play, entertainment, stay, transport, unknown 中选择；公园、景点、citywalk、路线、拍照打卡、展览、寺庙、博物馆归为 play。"
            f"normalized_tags 只能从这个列表选择：{sorted(STANDARD_TAGS)}。"
            "raw_tags 放模型生成的有价值标签，比如菜系、口味、玩法、商圈、人群、季节。"
            f"\n\ntitle_hint: {title_hint or ''}"
        )
        data = await self._chat_json(content, images=[image_base64])
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

    async def generate_chat_ui(
        self,
        *,
        message: str,
        answer: str,
        contexts: list[dict],
    ) -> ChatUiResponse:
        content = (
            "你是 TripGuard 的旅行 UI Agent。请直接生成给手机客户端的最终 ChatUiResponse JSON。"
            "不要输出 Markdown、解释文字或代码。"
            "当前开放的 Catalog 只有 assistant_text 和 place_card；不得生成任何其他 type、action 或 grounding。"
            "assistant_text 用于简短自然语言回答；place_card 是地点信息卡片。"
            "events 的第一项必须是 assistant_text。只要给出了资料上下文，就必须生成一张对应的 place_card。"
            "后端只会提供与用户目的地匹配的资料；绝不能虚构上下文之外的地点。"
            "place_card 必须填写 title（使用资料上下文里的 title）和 summary。"
            "只能使用资料上下文中出现的 source_id 和 evidence_id；"
            "没有 evidence_id 时，不得声称 grounding.kind 是 knowledge_base。"
            "action 只能使用：itinerary_card 的 add_itinerary，place_card 的 add_slot 或 refresh_places，"
            "evidence_card 的 toggle_evidence。不得虚构地点、价格、营业时间或引用。"
            "event_id 必须在本次响应内唯一。"
            "\n\n"
            f"用户问题：{message}\n"
            f"已有旅行回答：{answer}\n"
            f"可用资料上下文：{json.dumps(contexts, ensure_ascii=False)}"
        )
        data = await self._chat_json(
            content,
            response_format=ChatUiResponse.model_json_schema(),
            max_tokens=max(self._settings.llm_max_tokens, 500),
        )
        return _coerce_model(
            ChatUiResponse,
            data,
            fallback=ChatUiResponse(message_id="", events=[]),
        )

    async def _chat_json(
        self,
        content: str,
        *,
        images: list[str] | None = None,
        response_format: object = "json",
        max_tokens: int | None = None,
    ) -> Any:
        url = f"{self._settings.llm_base_url.rstrip('/')}/api/chat"
        user_message: dict[str, Any] = {"role": "user", "content": content}
        if images:
            user_message["images"] = images
        payload = {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": "只输出严格 JSON，不要输出 Markdown。"},
                user_message,
            ],
            "stream": False,
            "think": False,
            "format": response_format,
            "options": {
                "temperature": 0.1,
                "num_predict": max_tokens or self._settings.llm_max_tokens,
            },
        }
        async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
            for attempt in range(2):
                response = await client.post(url, json=payload)
                response.raise_for_status()
                text = response.json()["message"]["content"]
                try:
                    return json.loads(_extract_json_object(text))
                except json.JSONDecodeError:
                    if attempt == 1:
                        return {}
                    retry_message = dict(user_message)
                    retry_message["content"] = content + self._JSON_RETRY_INSTRUCTION
                    payload["messages"][-1] = retry_message
        return {}


def normalize_analysis(analysis: SourceAnalysis) -> SourceAnalysis:
    category = analysis.category if analysis.category in {item.value for item in Category} else Category.unknown.value
    normalized_tags = [tag for tag in analysis.normalized_tags if tag in STANDARD_TAGS]
    raw_tags = list(dict.fromkeys([*analysis.raw_tags, *[tag for tag in analysis.normalized_tags if tag not in STANDARD_TAGS]]))
    destination = analysis.destination.strip() if analysis.destination else "未知"
    if category == Category.unknown.value and _looks_like_scenic_play(analysis, normalized_tags, raw_tags):
        category = Category.play.value
    has_required_card_fields = _has_specific_destination(destination) and category != Category.unknown.value
    is_travel_related = analysis.is_travel_related and has_required_card_fields
    return analysis.model_copy(
        update={
            "is_travel_related": is_travel_related,
            "reason": analysis.reason if is_travel_related or not analysis.is_travel_related else "missing destination or category",
            "category": category,
            "title": analysis.title.strip() if analysis.title else None,
            "body_text": analysis.body_text.strip() if analysis.body_text else None,
            "normalized_tags": list(dict.fromkeys(normalized_tags)),
            "raw_tags": raw_tags,
            "destination": destination or "未知",
        }
    )


def decide_ingestion(analysis: SourceAnalysis) -> str:
    """Return the system admission decision without treating the LLM flag as final."""
    if analysis.is_travel_related:
        return "accept"
    has_destination = _has_specific_destination(analysis.destination)
    has_category = analysis.category != Category.unknown.value
    return "review" if has_destination and has_category else "reject"


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


def _extract_json_object(text: str) -> str:
    stripped = _strip_json_fence(text)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return stripped
    return stripped[start : end + 1]


def _has_specific_destination(destination: str) -> bool:
    return destination.strip().lower() not in {"", "未知", "unknown", "null", "none", "不确定", "无法判断"}


def _looks_like_scenic_play(analysis: SourceAnalysis, normalized_tags: list[str], raw_tags: list[str]) -> bool:
    text = " ".join(
        [
            analysis.title or "",
            analysis.body_text or "",
            analysis.location_name or "",
            *normalized_tags,
            *raw_tags,
        ]
    ).lower()
    scenic_keywords = {
        "公园",
        "景点",
        "路线",
        "citywalk",
        "city walk",
        "博物馆",
        "寺庙",
        "海边",
        "夜景",
        "花海",
        "园林",
        "拍照",
        "游玩",
        "徒步",
    }
    return any(keyword in text for keyword in scenic_keywords)


LmStudioLlmClient = OllamaLlmClient
