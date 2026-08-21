# POI Crawl Auto-sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist POI crawl submissions and automatically create one attraction only after Crawlab has produced evidence-backed pages and Ollama has generated an initial draft.

**Architecture:** Keep HTTP route validation and response shaping in `app/admin_api.py`.  Add `app/poi_sync.py` for the resumable state machine, page loading, LLM draft conversion, and remote create transaction boundaries.  `app/main.py` owns a small executor and only resumes states that have not entered an unknown remote-create outcome.

**Tech Stack:** FastAPI, SQLModel/SQLite, requests, existing Ollama async client, pytest/FastAPI TestClient.

---

## File structure

- Create `app/poi_sync.py`: synchronisation state machine and serialisable status payload.
- Modify `app/admin_api.py`: store a `PoiCrawlRecord` when Crawlab accepts a job; expose persisted sync state and explicit scheduling routes.
- Modify `app/main.py`: initialise and schedule the POI executor without blocking an HTTP handler.
- Modify `app/config.py`: keep exactly one default definition for each external integration URL.
- Modify `tests/test_admin_api.py`: route and persistence regression tests.
- Create `tests/test_poi_sync.py`: isolated state-machine tests using fake Crawlab, LLM, and attraction callables.

### Task 1: Define a testable POI synchronisation service

**Files:**

- Create: `app/poi_sync.py`
- Create: `tests/test_poi_sync.py`

- [ ] **Step 1: Write the failing service tests**

```python
def test_sync_creates_attraction_from_readable_completed_pages(tmp_path: Path) -> None:
    engine = create_db_engine(Settings(database_url=f"sqlite:///{tmp_path / 'sync.db'}"))
    init_db(engine)
    record = add_record(engine, crawl_task_id="crawl-ok", poi_id="123")
    sync = PoiSyncService(
        engine=engine,
        crawlab=lambda path, **_: status_with_page("native-ok") if path.endswith("crawl-ok") else pages_with_markdown(),
        generate_draft=lambda **_: PoiDraftContent(description="有证据的介绍", tags=["历史建筑"]),
        create_attraction=lambda payload: {"attractionId": "attr-1", **payload},
    )

    assert sync.run("crawl-ok") == "created"
    saved = get_record(engine, "crawl-ok")
    assert saved.sync_status == "created"
    assert saved.attraction_id == "attr-1"
    assert saved.draft_json["description"] == "有证据的介绍"
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `.venv/bin/pytest tests/test_poi_sync.py::test_sync_creates_attraction_from_readable_completed_pages -q`

Expected: FAIL because `app.poi_sync` does not exist.

- [ ] **Step 3: Add the minimal state machine**

```python
class PoiSyncService:
    def run(self, crawl_task_id: str) -> str:
        record = self._record(crawl_task_id)
        if record.sync_status == "created":
            return "created"
        status_payload = self._crawlab(f"/poi-crawls/{crawl_task_id}")
        pages = self._read_completed_pages(status_payload)
        if not pages:
            self._save(record, status="failed", error="Crawlab did not produce readable pages")
            return "failed"
        self._save(record, status="generating", error=None)
        draft = _model_dump(self._generate_draft(poi=record.poi_json, pages=pages))
        draft.update(_draft_metadata(record, crawl_task_id, len(pages)))
        self._save(record, status="creating", draft=draft, error=None)
        created = self._create_attraction({"poiId": record.poi_id, "attrInfo": draft_to_attr_info(draft, record)})
        self._save(record, status="created", attraction_id=extract_attraction_id(created), draft=draft, error=None)
        return "created"
```

Use `asyncio.run()` only inside the worker thread to resolve the existing asynchronous `generate_poi_draft()` method.  Catch upstream/LLM exceptions in `run()`, write `failed`, and re-raise only when the caller needs the error for logs.

- [ ] **Step 4: Run the focused service test to verify it passes**

Run: `.venv/bin/pytest tests/test_poi_sync.py::test_sync_creates_attraction_from_readable_completed_pages -q`

Expected: PASS.

- [ ] **Step 5: Add failure and duplicate-prevention tests before extending the implementation**

```python
def test_sync_marks_zero_page_crawl_failed_without_calling_llm_or_create(...):
    assert sync.run("crawl-empty") == "failed"
    assert generated == []
    assert creates == []

def test_sync_does_not_recreate_a_record_already_marked_created(...):
    assert sync.run("crawl-created") == "created"
    assert creates == []

def test_resume_candidates_exclude_creating_and_created(...):
    assert pending_crawl_ids(engine) == ["queued-id", "crawling-id"]
```

- [ ] **Step 6: Run all service tests to verify the new cases fail, then implement the minimal guarded transitions and rerun**

Run: `.venv/bin/pytest tests/test_poi_sync.py -q`

Expected before implementation: failures for zero-page handling and pending selection.  Expected after implementation: PASS.

- [ ] **Step 7: Commit the isolated service**

```bash
git add app/poi_sync.py tests/test_poi_sync.py
git commit -m "feat: add resumable POI crawl synchronizer"
```

### Task 2: Persist submissions and expose safe sync controls

**Files:**

- Modify: `app/admin_api.py:1-330`
- Modify: `tests/test_admin_api.py:1-220`

- [ ] **Step 1: Write failing route tests**

```python
def test_poi_submit_persists_returned_crawl_record_and_queues_sync(tmp_path: Path, monkeypatch) -> None:
    client = make_poi_client(tmp_path)
    monkeypatch.setattr("requests.request", crawlab_accepting_request)

    response = client.post("/admin-api/poi/crawls", json={
        "poiId": "123", "poiKey": "tencent_map:123",
        "poi": {"name": "故宫", "city": "北京"},
        "sourceUrls": ["https://example.test/forbidden-city"],
    })

    assert response.status_code == 202
    assert response.json()["data"]["localSync"]["syncStatus"] == "queued"
    assert record_for(client, "crawl-1").poi_name == "故宫"

def test_explicit_sync_rejects_created_or_creating_records(tmp_path: Path) -> None:
    response = client.post("/admin-api/poi/crawls/crawl-created/sync")
    assert response.status_code == 409
```

- [ ] **Step 2: Run the selected tests to verify they fail**

Run: `.venv/bin/pytest tests/test_admin_api.py -k 'persist or explicit_sync' -q`

Expected: FAIL because the response has no `localSync` field and the sync route is absent.

- [ ] **Step 3: Implement persistence and route handlers**

```python
@router.post("/poi/crawls", status_code=status.HTTP_202_ACCEPTED)
def submit_poi_crawl(request: Request, payload: dict[str, object]) -> dict[str, object]:
    remote = _crawlab_api(settings, "POST", "/poi-crawls", payload=payload)
    task = _mapping(remote)
    crawl_task_id = _require_crawl_task_id(task)
    record = _create_or_get_poi_record(request.app.state.engine, crawl_task_id, payload)
    request.app.state.schedule_poi_sync(crawl_task_id)
    return _poi_response({**task, "localSync": _poi_record_payload(record)})

@router.post("/poi/crawls/{crawl_task_id}/sync", status_code=status.HTTP_202_ACCEPTED)
def sync_poi_crawl(request: Request, crawl_task_id: str) -> dict[str, object]:
    record = _get_poi_record(request.app.state.engine, _task_identifier(crawl_task_id))
    if record.sync_status in {"created", "creating"}:
        raise HTTPException(status_code=409, detail="POI crawl cannot be safely rescheduled")
    request.app.state.schedule_poi_sync(record.crawl_task_id)
    return _poi_response({"localSync": _poi_record_payload(record)})
```

Preserve existing public `/poi/crawls` status/page routes.  Do not accept missing `poiId` or a missing logical `crawlTaskId`; return `502` for malformed Crawlab success payloads rather than adding unusable records.

- [ ] **Step 4: Run the focused route tests to verify they pass**

Run: `.venv/bin/pytest tests/test_admin_api.py -k 'persist or explicit_sync' -q`

Expected: PASS.

- [ ] **Step 5: Commit the API boundary**

```bash
git add app/admin_api.py tests/test_admin_api.py
git commit -m "feat: persist and schedule POI crawl syncs"
```

### Task 3: Wire lifecycle scheduling and configuration

**Files:**

- Modify: `app/main.py:40-100`
- Modify: `app/config.py:4-45`
- Modify: `tests/test_poi_sync.py`

- [ ] **Step 1: Write a failing startup-resume test**

```python
def test_app_startup_schedules_only_queued_and_crawling_poi_records(tmp_path: Path) -> None:
    app = create_app(settings=admin_settings(tmp_path))
    add_records(app.state.engine, {"queued": "queued", "crawling": "crawling", "creating": "creating"})
    scheduler = RecordingExecutor()
    app.state.poi_sync_executor = scheduler

    app.state.resume_poi_syncs()

    assert [args for _, args in scheduler.calls] == [("queued",), ("crawling",)]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_poi_sync.py::test_app_startup_schedules_only_queued_and_crawling_poi_records -q`

Expected: FAIL because `resume_poi_syncs` is absent.

- [ ] **Step 3: Add bounded scheduling and remove duplicate Settings declarations**

```python
app.state.poi_sync_executor = ThreadPoolExecutor(max_workers=1)
app.state.poi_sync_service = PoiSyncService.from_app(engine=engine, settings=app_settings, llm_client=client)

def schedule_poi_sync(crawl_task_id: str) -> None:
    app.state.poi_sync_executor.submit(app.state.poi_sync_service.run, crawl_task_id)

def resume_poi_syncs() -> None:
    for crawl_task_id in app.state.poi_sync_service.pending_crawl_ids():
        schedule_poi_sync(crawl_task_id)
```

Call `resume_poi_syncs()` only when `admin_api_enabled` is true.  Keep `attraction_api_base_url` as exactly one setting with the approved default `https://x.inews.qq.com/travel/v1/admin`; remove later duplicate optional declarations that overwrite it with `None`.

- [ ] **Step 4: Run the lifecycle test and configuration tests**

Run: `.venv/bin/pytest tests/test_poi_sync.py::test_app_startup_schedules_only_queued_and_crawling_poi_records tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit lifecycle wiring**

```bash
git add app/main.py app/config.py tests/test_poi_sync.py
git commit -m "feat: resume safe pending POI syncs"
```

### Task 4: Regressions and production handoff

**Files:**

- Modify: `docs/superpowers/specs/2026-08-21-poi-auto-sync-design.md` only if implementation differs from the approved design.

- [ ] **Step 1: Run the focused regression suites**

Run: `.venv/bin/pytest tests/test_poi_sync.py tests/test_admin_api.py -q`

Expected: PASS with no failed tests.

- [ ] **Step 2: Run the full backend suite**

Run: `.venv/bin/pytest -q`

Expected: PASS with no failed tests.

- [ ] **Step 3: Inspect the final diff and commit any final tested adjustments**

```bash
git diff --check
git status --short
git add app/ tests/ docs/superpowers/
git commit -m "test: cover POI crawl auto-sync workflow"
```

- [ ] **Step 4: Deploy after the branch is reviewed and merged**

On `claw`, update the regular `/home/aatroxli/tripguard` checkout from its remote branch, rebuild/restart only the backend service, and check the real health endpoint without printing configuration values.  Confirm the attraction base URL is configured by making a harmless authenticated route request that reaches the upstream only after a deliberately invalid payload is rejected locally.

- [ ] **Step 5: Execute the approved historical replay exactly once**

Create the missing local record from the successful aggregate task `tencent_map:16881613956274024706`, invoke its explicit sync endpoint once, and wait for persisted `created` plus a returned attraction ID.  Inspect the failed aggregate task `tencent_map:13165873531834571479`; verify it has no readable pages and no attraction create request.  Do not print environment values, tokens, or full raw crawl content.

- [ ] **Step 6: Publish the existing FUE Test deployment only after the backend flow is live**

The admin web is static and already calls the BFF.  Trigger a FUE Test deployment through available tooling or the signed-in browser if the FUE MCP is not exposed, then open the deployed review page and verify the persisted local sync state is visible through the BFF.
