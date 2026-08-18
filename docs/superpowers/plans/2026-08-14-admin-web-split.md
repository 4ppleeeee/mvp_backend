# TripGuard Admin Web Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the TripGuard management UI into a new React/Vite Gongfeng repository and replace FastAPI's embedded pages with the JSON APIs needed by `/admin` and `/admin/poi`.

**Architecture:** FastAPI remains the private BFF for ingestion state, uploads, Tencent map suggestion and Crawlab operations. The static React application owns presentation and browser routing; it never receives service credentials. Temporary no-login test access is bounded by an explicit CORS origin allowlist and must later be protected by FUE gateway authentication.

**Tech Stack:** Python 3.12, FastAPI, SQLModel, pytest, React, TypeScript, Vite, React Router, Vitest.

---

### Task 1: Replace embedded admin routes with tested JSON BFF routes

**Files:**
- Create: `app/admin_api.py`
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `tests/test_admin_api.py`

- [ ] **Step 1: Write failing API tests**

Add tests showing unauthenticated-in-the-application (no legacy session) calls can list title-first tasks, submit URL/image jobs, obtain task/source detail, review a task, and receive safe cover proxy responses.

- [ ] **Step 2: Run the targeted test**

Run: `python -m pytest tests/test_admin_api.py -q`

Expected: collection succeeds and tests fail because `/admin-api/*` is not registered.

- [ ] **Step 3: Implement the API router**

Create `create_admin_api_router()` with the documented routes and reuse existing `ResourceClassifier`, `IngestionService`, `IngestionJob`, `TravelSource`, `SourceEvidence`, and SSRF-safe cover logic. Add CORS configuration from `TRIPGUARD_ADMIN_ALLOWED_ORIGINS`; do not add a browser-readable credential.

- [ ] **Step 4: Verify targeted and full backend tests**

Run: `python -m pytest tests/test_admin_api.py -q && python -m pytest -q`

Expected: all targeted assertions and all existing backend tests pass.

### Task 2: Bring the POI control plane into the BFF

**Files:**
- Modify: `app/admin_api.py`
- Modify: `app/config.py`
- Modify: `tests/test_admin_api.py`

- [ ] **Step 1: Write failing POI API tests**

Test normalized Tencent suggestion results and mocked Crawlab proxy calls for submit/list/status/read-pages/search. Assert the mock receives the bearer token and the browser JSON response does not contain it.

- [ ] **Step 2: Run the POI test subset**

Run: `python -m pytest tests/test_admin_api.py -k poi -q`

Expected: fail because POI BFF routes are absent.

- [ ] **Step 3: Implement server-side POI proxy routes**

Add `crawlab_results_api_url`, `crawlab_api_token`, `tencent_location_api_key`, and `tencent_location_base_url` settings. Implement only service-side request forwarding with bounded timeouts and status translation.

- [ ] **Step 4: Verify POI route behavior**

Run: `python -m pytest tests/test_admin_api.py -k poi -q`

Expected: all POI contract tests pass.

### Task 3: Remove legacy template and login delivery

**Files:**
- Delete: `app/admin_routes.py`
- Delete: `app/admin_auth.py`
- Delete: `app/templates/admin/`
- Delete: `app/static/admin.css`
- Modify: `app/main.py`
- Modify: `app/config.py`
- Modify: `tests/test_admin.py`

- [ ] **Step 1: Write an absence/contract test**

Assert `/admin` is no longer an application-rendered page and `/admin-api/tasks` is the supported management surface.

- [ ] **Step 2: Verify the test fails against the legacy registration**

Run: `python -m pytest tests/test_admin_api.py -k legacy -q`

Expected: fails while `create_admin_router()` remains registered.

- [ ] **Step 3: Remove the Jinja/session/auth wiring**

Remove template mount/imports, `SessionMiddleware`, `AdminAuthenticator`, legacy admin settings and legacy tests. Retain general client and ingestion API routes unchanged.

- [ ] **Step 4: Verify the backend surface**

Run: `python -m pytest -q`

Expected: all remaining tests pass, with no `admin_auth`, templates or `/admin` HTML route remaining.

### Task 4: Create the independent React administration application

**Files:**
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/package.json`
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/src/main.tsx`
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/src/App.tsx`
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/src/api.ts`
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/src/pages/IngestionConsole.tsx`
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/src/pages/PoiConsole.tsx`
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/src/styles.css`
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/src/*.test.tsx`

- [ ] **Step 1: Write failing front-end route and API-client tests**

Cover root redirect, both `/admin` and `/admin/poi` routes, URL submission, task polling stop on terminal status, POI suggestion and crawl submission using a mocked API client.

- [ ] **Step 2: Run Vitest**

Run: `npm test -- --run`

Expected: tests fail before components and client exist.

- [ ] **Step 3: Implement the SPA**

Implement the shared warm travel-workspace shell, two console pages, API client, component state and responsive CSS. Do not create login/logout controls or embed service tokens.

- [ ] **Step 4: Verify front-end build and tests**

Run: `npm test -- --run && npm run build`

Expected: tests pass and Vite produces `dist/`.

### Task 5: Publish the separated repository to Gongfeng

**Files:**
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/.gitignore`
- Create: `/Users/aatroxli/coding/travel/tripguard-admin-web/README.md`

- [ ] **Step 1: Verify no similarly named Gongfeng project exists**

Use the Gongfeng MCP project search for `tripguard-admin-web`.

- [ ] **Step 2: Create the private Gongfeng repository**

Create `tripguard-admin-web` in the authenticated user's default namespace, initialize its `master` branch with the built application source, README and `.gitignore`.

- [ ] **Step 3: Verify remote repository contents**

Use Gongfeng MCP repository tree/file inspection to confirm `package.json`, `src/`, `README.md` and no generated `dist/` or credential files were published.

### Task 6: Final integration audit

**Files:**
- Modify: `README.md`
- Create: `docs/admin-web-deployment.md`

- [ ] **Step 1: Document FUE test deployment variables**

Document `VITE_ADMIN_API_BASE_URL`, `TRIPGUARD_ADMIN_ALLOWED_ORIGINS`, the private server-side POI variables, and the requirement to place FUE gateway authentication ahead of production access. Do not include values.

- [ ] **Step 2: Run complete verification**

Run: backend `python -m pytest -q`; frontend `npm test -- --run && npm run build`; inspect Git status in both repositories; inspect the Gongfeng repository tree.

- [ ] **Step 3: Report deployed-ready artifacts**

Report the Gongfeng project URL/ID, branch, build result, retained backend API contract, and the remaining FUE gateway/test-environment configuration action.
