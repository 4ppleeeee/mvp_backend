# TripGuard 管理后台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a password-protected management console for URL/image ingestion, live job status and persisted results.

**Architecture:** FastAPI serves Jinja templates and static assets. A signed server session guards `/admin`; HTML forms create the same ingestion jobs as API clients; lightweight JavaScript polls a safe HTML status fragment until the job reaches a terminal state.

**Tech Stack:** FastAPI, Starlette SessionMiddleware, Jinja2, pwdlib Argon2, SQLModel/SQLite, pytest.

---

### Task 1: Add administrator configuration and authentication guard

**Files:**
- Modify: `app/config.py`
- Create: `app/admin_auth.py`
- Modify: `app/main.py`
- Modify: `pyproject.toml`
- Test: `tests/test_admin.py`

- [ ] Write failing tests for unauthenticated `/admin` redirect, rejected bad login, accepted valid Argon2 hash, and missing-config 503.
- [ ] Run `pytest tests/test_admin.py -v`; expect route-missing failures.
- [ ] Add `admin_username`, `admin_password_hash`, `admin_session_secret` settings; use `pwdlib.PasswordHash.recommended()` and constant username comparison. Add SessionMiddleware only when configured and redirect unauthenticated users to `/admin/login`.
- [ ] Run `pytest tests/test_admin.py tests/test_api.py -v`; expect PASS.
- [ ] Commit: `feat: protect admin console with login`.

### Task 2: Add image ingestion worker and public multipart endpoint

**Files:**
- Modify: `app/config.py`
- Create: `app/ingestion/image_service.py`
- Modify: `app/main.py`
- Modify: `app/schemas.py`
- Test: `tests/test_ingestion_image.py`

- [ ] Write failing tests for accepted image upload, media/size rejection, queued job and successful OCR evidence persistence with fake executor/Ollama.
- [ ] Run `pytest tests/test_ingestion_image.py -v`; expect endpoint-missing failures.
- [ ] Stream upload to a job-local temporary directory, execute `OllamaLlmClient.analyze_image` in the worker, save `TravelSource` and `SourceEvidence(origin=ocr)`, and always remove the job directory.
- [ ] Run image, service and legacy API tests; expect PASS.
- [ ] Commit: `feat: ingest images asynchronously`.

### Task 3: Render management pages and status fragment

**Files:**
- Create: `app/admin_routes.py`
- Create: `app/templates/admin/login.html`
- Create: `app/templates/admin/dashboard.html`
- Create: `app/templates/admin/job.html`
- Create: `app/templates/admin/job_fragment.html`
- Create: `app/static/admin.css`
- Modify: `app/main.py`
- Test: `tests/test_admin.py`

- [ ] Write failing tests for URL submission redirect, dashboard task list, image form action, fragment task stage and success result/evidence rendering.
- [ ] Run `pytest tests/test_admin.py -v`; expect missing templates/routes.
- [ ] Implement authenticated routes and templates; browser polls only the fragment endpoint every two seconds and stops for `succeeded`/`failed`.
- [ ] Run `pytest tests/test_admin.py tests/test_ingestion_*.py -v`; expect PASS.
- [ ] Commit: `feat: add ingestion management console`.

### Task 4: Package, test and deploy

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] Add Jinja2 and pwdlib[argon2]; document environment-variable provisioning without recording secrets.
- [ ] Run `pytest -v`, `docker compose build backend`, and a local container smoke test for health plus unauthenticated admin redirect.
- [ ] Deploy the committed branch to claw, provision the three authentication environment variables there, and verify login, URL/image job submission and terminal result pages.
- [ ] Commit packaging/doc changes and report deployed URL without credentials.
