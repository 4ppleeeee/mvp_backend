# Capability-Oriented Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor ingestion dispatch around source adapters and capabilities, add public Xiaoyuzhou audio ingestion, and preserve existing platform behavior.

**Architecture:** Add a source registry, probe/result types, capability declarations, and a planner that executes common evidence steps through source-specific adapters. Keep current persisted fields and compatibility APIs while moving dispatch out of the `article`/`video` conditional.

**Tech Stack:** Python 3.12, FastAPI, SQLModel, yt-dlp, requests, FFmpeg, faster-whisper, pytest.

---

### Task 1: Add source and capability domain types

**Files:**
- Create: `app/ingestion/capabilities.py`
- Modify: `app/ingestion/domain.py`
- Test: `tests/test_ingestion_capabilities.py`

- [ ] **Step 1: Write failing tests** for `ResourceKind`, `Capability`, `SourceProbe`, and deterministic capability ordering.
- [ ] **Step 2: Run** `pytest -q tests/test_ingestion_capabilities.py` and confirm the new imports fail.
- [ ] **Step 3: Implement** string enums and immutable probe/plan data classes. Keep `MediaType` as a compatibility alias at API boundaries.
- [ ] **Step 4: Run** the focused tests and confirm they pass.
- [ ] **Step 5: Run** `git diff --check`.

### Task 2: Define the source adapter boundary and registry

**Files:**
- Create: `app/ingestion/sources.py`
- Modify: `app/ingestion/adapters/base.py`
- Modify: `app/ingestion/adapters/__init__.py`
- Test: `tests/test_source_registry.py`

- [ ] **Step 1: Write failing tests** showing registry lookup by URL host, a source probe returning resource kind/capabilities, and unsupported operations raising a typed extraction error.
- [ ] **Step 2: Run** the focused tests and confirm failure because the registry and probe methods do not exist.
- [ ] **Step 3: Implement** a `SourceAdapter` protocol with `matches`, `probe`, `fetch_metadata`, `fetch_caption`, `acquire_audio`, and optional `acquire_video`; add a registry that preserves current public video adapters and allows source-specific adapters.
- [ ] **Step 4: Run** focused registry tests and existing adapter tests.
- [ ] **Step 5: Commit** as `refactor: add capability-oriented source boundary`.

### Task 3: Implement public Xiaoyuzhou audio source

**Files:**
- Create: `app/ingestion/adapters/xiaoyuzhou.py`
- Modify: `app/ingestion/sources.py`
- Test: `tests/test_xiaoyuzhou_ingestion.py`
- Add fixture: `tests/fixtures/xiaoyuzhou_episode.html`

- [ ] **Step 1: Add a fixture-based failing test** for the supplied episode shape: extract title, author, duration, and an HTTPS `.m4a` URL; report `resource_kind=audio`, `metadata`, `audio`, and `transcription` capabilities.
- [ ] **Step 2: Run** `pytest -q tests/test_xiaoyuzhou_ingestion.py` and confirm the source module is missing.
- [ ] **Step 3: Implement** public HTML/embedded-state parsing, strict HTTPS media-host validation, bounded streaming download into the task directory, and `fetch_caption()` returning `None` because no public caption endpoint is available.
- [ ] **Step 4: Run** fixture tests and a read-only probe against `https://www.xiaoyuzhoufm.com/episode/6a5f441fa3fec224d5a10e23`; do not persist the downloaded audio.
- [ ] **Step 5: Run** `git diff --check` and commit as `feat: support Xiaoyuzhou audio sources`.

### Task 4: Add capability planner and migrate VideoPipeline behavior

**Files:**
- Create: `app/ingestion/planner.py`
- Modify: `app/ingestion/pipeline.py`
- Modify: `app/ingestion/service.py`
- Test: `tests/test_ingestion_planner.py`
- Test: `tests/test_ingestion_pipeline.py`

- [ ] **Step 1: Write failing tests** for audio-only execution, caption-first execution, optional keyframes only when the capability exists, and a clear unsupported-capability error.
- [ ] **Step 2: Run** focused tests and confirm failure against the current video-only protocol.
- [ ] **Step 3: Implement** a planner that requests metadata, tries captions when declared, downloads audio for ASR when needed, and only requests video/keyframes when explicitly enabled and supported. Keep temporary cleanup and `MediaExtractionError` fallback semantics.
- [ ] **Step 4: Run** focused pipeline/planner tests and the existing media egress tests.
- [ ] **Step 5: Commit** as `refactor: plan ingestion from source capabilities`.

### Task 5: Switch API and admin execution to source resolution plus planning

**Files:**
- Modify: `app/main.py`
- Modify: `app/admin_routes.py`
- Modify: `app/ingestion/classifier.py`
- Modify: `app/schemas.py`
- Test: `tests/test_ingestion_api.py`
- Test: `tests/test_admin.py`

- [ ] **Step 1: Write failing tests** asserting the supplied Xiaoyuzhou URL is classified as `source_platform=xiaoyuzhou`, `resource_kind=audio`, and queued without being labeled `video`; retain regression tests for Bilibili and ordinary Xiaohongshu article URLs.
- [ ] **Step 2: Run** focused API/admin tests and confirm current classification cannot represent Xiaoyuzhou audio.
- [ ] **Step 3: Implement** source resolution and probe before plan creation. Populate legacy `media_type` only at the compatibility boundary and expose `resource_kind` where schemas already permit additive fields.
- [ ] **Step 4: Run** all API/admin tests and verify the background executor receives the planned source operation.
- [ ] **Step 5: Commit** as `refactor: dispatch ingestion through source plans`.

### Task 6: Verify in Python 3.12 and update handoff

**Files:**
- Modify: `HANDOFF.md`
- Modify: `docs/HANDOFF.md`
- Test: all existing tests

- [ ] **Step 1: Run** `python3 -m compileall -q app tests` and `git diff --check`.
- [ ] **Step 2: Run** in the claw Python 3.12 container: `pip install -q pytest && pytest -q`.
- [ ] **Step 3: Run** a read-only Xiaoyuzhou probe and a small public audio smoke test if the supplied file size is safe; record that no Cookie or browser automation is used.
- [ ] **Step 4: Update** both handoff entry points with the new source/capability model, the Xiaoyuzhou route, and the exact verification result.
- [ ] **Step 5: Commit** as `docs: update capability ingestion handoff` and push only after the user requests deployment.
