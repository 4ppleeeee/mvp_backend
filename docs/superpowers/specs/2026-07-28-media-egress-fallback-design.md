# Media Egress Fallback Design

## Goal

Make video ingestion resilient to YouTube's selective blocking of automated subtitle and media requests without changing the existing router-default path or storing browser credentials. When an administrator supplies a separate media egress URL, the service retries a blocked media extraction through that egress and records the final route and failure phase on the ingestion job.

## Context

The claw host and the TripGuard container can load `youtube.com`, but the same egress receives `RequestBlocked` from `youtube-transcript-api` and a human-verification response from yt-dlp for the tested YouTube video. BiliNote's source has a runtime proxy manager which passes a proxy to both libraries; TripGuard imported the downloader and caption order but not that configuration boundary.

## Options Considered

1. Always send all media traffic through an explicit proxy. This matches a typical BiliNote local configuration, but needlessly replaces the user's existing router-default path and makes every supported platform depend on a new service.
2. Retry only blocked media work through an explicitly configured egress. This keeps current behavior unchanged, limits the new route to the failure case, and lets an administrator opt in per deployment. **Selected.**
3. Use cookies, browser extraction, or a verification-solving service. This conflicts with the background-only workflow and is out of scope.

## Configuration and Secrecy

`TRIPGUARD_MEDIA_PROXY_URL` is an optional deployment-only setting. It may contain credentials, so it is read from the existing private environment file, never returned by an API, stored in SQLite, rendered in HTML, or written to logs. Absence of the setting means no retry egress is available.

The effective route is represented only by safe labels:

- `router_default`: the existing host/router route;
- `configured_proxy`: the configured media egress was selected.

## Components

### Media egress policy

Create a small ingestion-layer policy that receives an optional proxy URL and produces a safe route label plus library-specific settings:

- yt-dlp gets its `proxy` option only for `configured_proxy`;
- `youtube-transcript-api` gets a `requests.Session` with HTTP and HTTPS proxy mappings only for `configured_proxy`;
- neither client logs the actual proxy URL.

Adapters receive this policy rather than reading environment variables themselves. This preserves platform adapters as media-specific code and keeps deployment configuration at the application boundary.

### Typed media extraction failures

Wrap failures from caption, metadata, and audio acquisition in a typed `MediaExtractionError`. It carries only:

- a safe phase (`caption`, `metadata`, or `audio`);
- a safe route label;
- a sanitized upstream error message;
- whether the failure is retryable through the configured proxy.

Caption absence remains a normal fallback to audio. A transport rejection or yt-dlp verification rejection is an extraction failure eligible for fallback; it must not be misreported as "no captions".

### Service retry

`IngestionService` receives a router-default pipeline and, only when the setting exists, a configured-proxy pipeline. It runs the normal pipeline first. On a retryable typed media failure it updates the job to `retrying_media_egress`, runs the configured-proxy pipeline once, and never loops between routes.

Other pipeline, ASR, and LLM failures do not trigger media-egress retries. This feature introduces no account, cookie, CAPTCHA, or browser automation flow.

### Persisted observability

`IngestionJob` gains nullable `media_egress` and `failure_stage` fields. Existing SQLite databases are migrated idempotently during initialization by checking the table schema and adding only missing columns. A successful job stores the route that yielded its evidence. A failed job stores the final attempted route and the media phase that failed.

The task detail fragment displays these safe values. The dashboard remains a concise task list; it keeps its existing status and stage presentation.

## Data Flow

```text
router-default pipeline
  -> caption / metadata / audio succeeds -> save route=router_default
  -> retryable media rejection + no configured proxy -> fail with phase and route
  -> retryable media rejection + configured proxy
       -> set stage=retrying_media_egress
       -> configured-proxy pipeline
            -> succeeds -> save route=configured_proxy
            -> fails -> fail with final phase and route
```

## Tests

- Verify media client options omit a proxy by default and include it only for the configured route.
- Verify a retryable router-default media failure invokes the configured-proxy pipeline exactly once and records `configured_proxy` on success.
- Verify no configured proxy leaves the failure terminal with `router_default` and its phase.
- Verify a configured-proxy failure is terminal, records the proxy route and phase, and does not retry again.
- Verify SQLite initialization upgrades a pre-feature ingestion table without data loss.
- Verify the task detail renders safe route and phase labels without exposing any configured proxy value.

## Non-goals

- Altering router rules, changing the current side-router configuration, or exposing an operator proxy configuration UI.
- Downloading or retaining videos, audio, snapshots, or keyframes.
- Cookie collection, CAPTCHA solving, account login, or any other verification bypass.
