# POI Crawl Auto-sync Design

## Goal

Restore the backend delivery path from a successful POI crawl to a generated attraction draft and a single create request to the Travel attraction service.  Persist enough state to resume safe work after a backend restart and to show the final state in the existing admin API.

## Scope

This change is limited to `mvp_backend_github` and its `/admin-api` POI workflow.

- Submitting `POST /admin-api/poi/crawls` creates or reuses one local `PoiCrawlRecord` for the returned logical `crawlTaskId`.
- A background worker polls the Crawlab results API, reads every successful native task's pages, asks the existing Ollama client for an initial draft, converts that draft to `attrInfo`, and calls `/attraction/create`.
- The worker persists the draft, attraction ID, current state, errors, and update time.
- Startup resumes records that are safe to continue.
- An explicit sync endpoint lets an administrator resume a failed or historical record after checking its content.

The frontend redesign, a new authentication flow, and creating records for crawls that never produced pages are out of scope.

## Data and State

Reuse `PoiCrawlRecord` so no new table is required.  `poi_json` and `source_urls` preserve the original request, `draft_json` preserves the generated source material, `attraction_id` is written only after the remote create returns successfully, and `sync_error` holds a user-safe operational error.

`sync_status` has these values:

| State | Meaning | Automatic action |
| --- | --- | --- |
| `queued` | The request has been accepted locally. | Poll Crawlab. |
| `crawling` | Crawlab has not produced usable pages yet. | Poll again later. |
| `generating` | Pages were found and the worker is preparing a draft. | Continue in the same worker invocation. |
| `creating` | A create request may be in progress. | Do not auto-retry after a process restart. |
| `created` | The remote attraction ID is persisted. | Never create again. |
| `failed` | Crawlab, LLM, or upstream create failed. | Require explicit sync/retry. |

The only completion condition is at least one readable result page from a source whose result is successful or partially successful.  A crawl with no readable pages is marked `failed`; it is never turned into an attraction from guessed data.  The results API's task status may be stale after native task cleanup, so readable `manifest`-backed pages are accepted as evidence even if the aggregate status is unavailable.

## Components and Flow

1. `admin_api.py` validates the returned crawl payload and stores a record atomically after Crawlab accepts the request.  The response remains the existing `{ok, data}` shape with a local sync summary added where applicable.
2. A focused `poi_sync.py` service reads aggregate source metadata and pages through the existing authenticated Crawlab helper.  It owns state transitions and the draft-to-`attrInfo` conversion so route handlers remain thin.
3. `main.py` owns one bounded POI executor.  It schedules newly submitted records and, on startup, schedules records in `queued` or `crawling`.  It does not schedule `creating`, preventing unknown remote outcomes from producing duplicate attractions.
4. `POST /admin-api/poi/crawls/{crawl_task_id}/sync` explicitly schedules a record that is `failed`, `queued`, or `crawling`.  It rejects `created` and `creating` without making an upstream request.
5. A record/status endpoint returns the persisted state, draft, attraction ID, and safe error text to the console.

The worker opens short-lived database sessions around each state transition.  It never keeps a SQL session open while awaiting Ollama or making network calls.  Before sending the create request it writes `creating`; after the request succeeds it writes `created` and `attraction_id`.  If generation or create raises, it writes `failed` with an error message.  A process crash after `creating` deliberately requires an explicit operator decision instead of retrying blindly.

## Existing Historical Tasks

Historical jobs may not have a local `PoiCrawlRecord`, because the legacy admin delivery routes were removed.  The recovery endpoint accepts the original POI metadata only when creating that missing local record, then follows the same page-evidence rule.  For the known successful August 20 task, the original POI metadata is retrieved from the crawl job before running the single create request.  The known zero-page failed task is reported as failed and is not created.

## Validation

Tests use the FastAPI test client and monkeypatch the external Crawlab, Ollama, and attraction calls.  They cover record persistence on submit, waiting for pages, successful draft/create persistence, failed crawls without pages, duplicate-create prevention, restart recovery selection, and explicit retry eligibility.  The existing admin API suite and the full backend test suite must pass before deployment.

Deployment first verifies the real backend health endpoint and the configured attraction base URL without printing any environment values.  Then it runs the successful historical task once, checks that a remote attraction ID and local `created` state are returned, and confirms that the failed zero-page task has no create attempt.
