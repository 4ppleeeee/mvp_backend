# Grounded Chat UI v1 Design

## Goal

Replace the current text-only recommendation response with a constrained event protocol that lets the TripGuard app render an itinerary-first chat response containing itinerary, place, and evidence cards.

## Scope

This version delivers one request/response turn and session-local card interactions.

- The backend keeps knowledge-base RAG as the preferred retrieval source.
- The model may produce a suggestion when no knowledge-base source covers part of an answer.
- The app owns the temporary “joined itinerary” state. There is no account, server-side itinerary, or cross-device persistence.
- The app renderer accepts only protocol-defined event and action types. A model never emits arbitrary Compose code or executable UI.
- Existing `POST /chat/recommend` remains unchanged for the current Android view-based page and other callers.

Out of scope: live Web/maps/policy tools, persisted user plans, streaming transport, arbitrary component trees, and multi-agent orchestration.

## Data Flow

```text
user message
  -> TravelQuery + SQL candidate gate
  -> RAG evidence retrieval for eligible knowledge-base sources
  -> structured ChatUiResponse
  -> Android Compose ChatEventRenderer
  -> local action or POST /chat/action
  -> updated ChatUiResponse
```

The RAG result remains a preference, not an exclusive boundary. A response item states its grounding explicitly:

- `knowledge_base`: a private TripGuard source/evidence supports the item.
- `external`: reserved for a future validated tool result; v1 does not produce it.
- `suggestion`: a model-generated planning suggestion. It must not claim a private source or external fact.

If an event identifies an evidence citation, the backend validates that `source_id` and `evidence_id` belong to evidence retrieved for the current request. Events marked `suggestion` do not require a citation.

## HTTP Contract

### `POST /chat`

Request:

```json
{
  "message": "东京两天，想拍照和吃甜品",
  "limit": 8
}
```

Response:

```json
{
  "message_id": "msg_01",
  "events": [
    {
      "event_id": "evt_text_01",
      "type": "assistant_text",
      "text": "我先安排一条步行友好的路线。"
    },
    {
      "event_id": "evt_itinerary_01",
      "type": "itinerary_card",
      "grounding": {"kind": "suggestion"},
      "title": "DAY 1 · 表参道到涩谷",
      "slots": [
        {"slot_id": "slot_01", "time_label": "10:00", "title": "表参道咖啡", "subtitle": "甜品和拍照集中"}
      ],
      "actions": [
        {"action_id": "add_itinerary", "label": "加入当前行程", "kind": "local", "payload": {"slot_ids": ["slot_01"]}}
      ]
    },
    {
      "event_id": "evt_place_01",
      "type": "place_card",
      "grounding": {"kind": "knowledge_base", "source_id": "src_tokyo", "evidence_id": "evd_asakusa", "segment_index": 4, "start_seconds": 12.5, "end_seconds": 27.0},
      "title": "表参道咖啡店",
      "summary": "周末建议提前取号。",
      "tags": ["甜品", "拍照好看"],
      "actions": [
        {"action_id": "add_slot", "label": "加入第 1 天", "kind": "local", "payload": {"slot_id": "slot_01"}},
        {"action_id": "refresh_places", "label": "换一批", "kind": "remote", "payload": {"exclude_event_ids": ["evt_place_01"]}}
      ]
    },
    {
      "event_id": "evt_evidence_01",
      "type": "evidence_card",
      "grounding": {"kind": "knowledge_base", "source_id": "src_tokyo", "evidence_id": "evd_asakusa", "segment_index": 4, "start_seconds": 12.5, "end_seconds": 27.0},
      "label": "我的收藏 · 视频 00:12–00:27",
      "excerpt": "周末建议提前取号。",
      "actions": [{"action_id": "toggle_evidence", "label": "查看证据", "kind": "local", "payload": {}}]
    }
  ]
}
```

### `POST /chat/action`

Only `remote` actions use this endpoint. The request carries the original message, the event/action identity, and opaque action payload. The backend re-runs the candidate gate and returns a complete `ChatUiResponse`; it does not persist a user itinerary.

```json
{
  "message": "东京两天，想拍照和吃甜品",
  "event_id": "evt_place_01",
  "action_id": "refresh_places",
  "payload": {"exclude_event_ids": ["evt_place_01"]}
}
```

Unknown event types, unknown action ids, malformed payloads, and citations outside the current retrieval/tool result are rejected before returning a response.

## Backend Responsibilities

1. Add Pydantic models for the response, event union, grounding union, slots, and actions.
2. Add a structured LLM method that returns a validated intermediate recommendation plan rather than Markdown or UI source code.
3. Convert that plan into whitelisted events, populate knowledge-base evidence from `RetrievedEvidence`, and downgrade ungrounded generated content to `suggestion`.
4. Implement `POST /chat` and `POST /chat/action` while preserving the current `/chat/recommend` contract.
5. Test the three grounding kinds, citation validation, remote refresh behavior, and old endpoint compatibility.

## Android Responsibilities

1. Add a JSON API model and `TripGuardApiClient.chat()` / `chatAction()` client calls for the new endpoints.
2. Add a Compose chat page and one renderer registry with exactly `assistant_text`, `itinerary_card`, `place_card`, and `evidence_card` branches.
3. Keep a page-local `SnapshotStateList` of itinerary slots. `add_itinerary` and `add_slot` update it locally; `toggle_evidence` expands/collapses evidence; `refresh_places` calls the backend and replaces the relevant remote events.
4. Render grounding visibly: private source, future external source, or suggestion. Suggestions do not expose a fake citation.
5. Preserve the existing `DemoActivity` launcher until the Compose chat page has the same request and source-list entry capability. Then update `mvp_demo` `master` with the validated Compose demo.

## Acceptance Criteria

- A RAG-backed answer returns a place card and matching evidence card with the same `source_id` / `evidence_id`.
- A planning-only item is marked `suggestion` and has no evidence id.
- A user can add an itinerary or place slot and see it in the current chat page without a network write.
- “换一批” produces a new server response; invalid actions return a controlled 4xx response.
- The app cannot render an event type outside the renderer registry.
- `/chat/recommend` remains available and its existing tests remain green.
- Android demo renderer and backend protocol tests pass before the app worktree is merged into `master`.
