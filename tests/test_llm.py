from app.llm import PoiDraftContent


def test_poi_draft_coerces_common_llm_json_types() -> None:
    draft = PoiDraftContent.model_validate(
        {
            "city_name": "北京",
            "tags": "历史景点、博物馆",
            "is_free": False,
            "ticket_price": "40-60",
            "warnings": "票价分淡旺季，需人工确认",
        }
    )

    assert draft.tags == ["历史景点", "博物馆"]
    assert draft.is_free == 0
    assert draft.ticket_price is None
    assert draft.warnings == ["票价分淡旺季，需人工确认"]
