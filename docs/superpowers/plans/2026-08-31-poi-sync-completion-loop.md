# POI Sync Completion Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably turn terminal Crawlab POI crawl results into a single attraction-review record without requiring a backend restart or operator action.

**Architecture:** Keep `PoiSyncService.run()` as the idempotent one-task synchronizer. Add a single in-process daemon loop to the FastAPI app that periodically schedules only persisted `queued` or `crawling` records; `run()` observes the Crawlab aggregate status and creates an attraction only after readable source pages exist. Failed/cancelled terminal crawls become `failed` with an operator-visible reason, while `created` and unknown `creating` outcomes are never retried.

**Tech Stack:** FastAPI lifespan hooks, `ThreadPoolExecutor`, SQLModel/SQLite, pytest.

---

### Task 1: Cover the durable completion scan

**Files:**
- Modify: `tests/test_poi_sync.py`
- Modify: `app/poi_sync.py`

- [ ] **Step 1: Write failing tests**

```python
def test_pending_crawl_ids_keeps_queued_and_crawling_records_only(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="queued", sync_status="queued")
    add_record(engine, crawl_task_id="crawling", sync_status="crawling")
    add_record(engine, crawl_task_id="created", sync_status="created")
    add_record(engine, crawl_task_id="creating", sync_status="creating")
    sync = make_sync(engine)
    assert sync.pending_crawl_ids() == ["queued", "crawling"]
```

```python
def test_sync_marks_terminal_crawl_without_readable_pages_failed(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    add_record(engine, crawl_task_id="empty", sync_status="crawling")
    sync = make_sync(engine, get_crawl=lambda _: {"status": "failed", "sources": []})
    assert sync.run("empty") == "failed"
    assert get_record(engine, "empty").sync_error == "Crawlab did not produce readable pages"
```

- [ ] **Step 2: Run focused tests and confirm the new scan behavior fails before app scheduling exists**

Run: `python3 -m pytest tests/test_poi_sync.py -q`

Expected: existing unit tests pass; the app-level durable scheduler test added in Task 2 fails because there is no periodic loop.

- [ ] **Step 3: Keep the service selection restricted to `queued` and `crawling` records**

```python
def pending_crawl_ids(self) -> list[str]:
    with Session(self._engine) as session:
        records = session.exec(
            select(PoiCrawlRecord)
            .where(PoiCrawlRecord.sync_status.in_(("queued", "crawling")))
            .order_by(PoiCrawlRecord.created_at)
        ).all()
    return [record.crawl_task_id for record in records]
```

- [ ] **Step 4: Re-run service tests**

Run: `python3 -m pytest tests/test_poi_sync.py -q`

Expected: PASS.

### Task 2: Add one bounded background rescan loop

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `tests/test_poi_sync.py`

- [ ] **Step 1: Write a failing app test for periodic rescheduling**

```python
def test_app_periodically_reschedules_pending_poi_syncs(tmp_path: Path) -> None:
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'app.db'}", admin_api_enabled=True))
    calls: list[str] = []
    app.state.schedule_poi_sync = calls.append
    add_record(app.state.engine, crawl_task_id="queued", sync_status="queued")
    add_record(app.state.engine, crawl_task_id="created", sync_status="created")
    app.state.resume_poi_syncs()
    assert calls == ["queued"]
```

- [ ] **Step 2: Run the focused app test and observe failure for the absent scheduling helper**

Run: `python3 -m pytest tests/test_poi_sync.py::test_app_periodically_reschedules_pending_poi_syncs -q`

Expected: FAIL until the app exposes a rescan-capable scheduler.

- [ ] **Step 3: Implement a configurable daemon loop with lifecycle shutdown**

```python
poi_sync_stop = Event()

def poi_sync_loop() -> None:
    while not poi_sync_stop.wait(app_settings.poi_sync_poll_interval_seconds):
        resume_poi_syncs()

app.state.poi_sync_stop = poi_sync_stop
app.state.poi_sync_poll_thread = Thread(target=poi_sync_loop, daemon=True)
app.state.poi_sync_poll_thread.start()
```

Add `poi_sync_poll_interval_seconds: float = 15.0` to `Settings`, reject values below one second, and stop/join the loop plus shut down the executor in the FastAPI shutdown hook.

- [ ] **Step 4: Run focused sync tests**

Run: `python3 -m pytest tests/test_poi_sync.py tests/test_admin_api.py -q`

Expected: PASS.

### Task 3: Verify and deliver

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Expose the safe polling interval configuration**

```yaml
TRIPGUARD_POI_SYNC_POLL_INTERVAL_SECONDS: ${TRIPGUARD_POI_SYNC_POLL_INTERVAL_SECONDS:-15}
```

- [ ] **Step 2: Run full backend verification**

Run: `python3 -m pytest -q`

Expected: PASS.

- [ ] **Step 3: Build the production container**

Run: `docker build -t tripguard-mvp-backend:poi-sync-loop .`

Expected: successful image build.

- [ ] **Step 4: Commit the feature branch**

```bash
git add app/config.py app/main.py docker-compose.yml tests/test_poi_sync.py docs/superpowers/plans/2026-08-31-poi-sync-completion-loop.md
git commit -m "fix: rescan pending POI crawl syncs"
```
