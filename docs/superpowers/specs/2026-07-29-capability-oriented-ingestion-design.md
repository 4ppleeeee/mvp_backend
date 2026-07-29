# Capability-Oriented Ingestion Design

## Goal

Separate how a user submits material, which source owns it, what resource kind it contains, and which extraction capabilities are available. This lets audio-only Xiaoyuzhou episodes, videos from different platforms, articles, and images use accurate source-specific acquisition without forcing every resource into the `article` or `video` branch.

## Decisions

### 1. Keep input form separate from resource kind

`input_type` describes the submission boundary: `url`, `image`, `text`, or `file`. A URL is not inherently a video, and an uploaded file is not inherently an image.

`resource_kind` describes the discovered resource: `article`, `audio`, `video`, `image`, `document`, or `unknown`. Existing persisted `media_type` remains readable during migration, but new orchestration must use the discovered kind and capabilities.

### 2. Resolve source before choosing capabilities

The source registry identifies a platform from the URL host and creates a source adapter. The adapter may perform a public probe before returning the final resource kind. Dynamic sources such as Xiaohongshu can therefore start as unresolved and later report article or video.

Each adapter owns source-specific access preparation and acquisition details. The orchestration layer never knows how a platform obtains a token, follows a redirect, parses an HTML page, or calls a public API.

The source access boundary is represented by a context/provider extension point. A future Douyin implementation may add an ephemeral `MsTokenProvider` inside `DouyinAccessContext`; it must not become a global video capability and must not introduce Cookie storage, CAPTCHA handling, browser automation, or verification bypass.

### 3. Plan work from capabilities

Adapters report capabilities such as:

- `metadata`
- `article_body`
- `caption`
- `audio`
- `video`
- `keyframes`
- `ocr`

The planner turns those capabilities into an ordered execution plan. The common order is metadata, platform caption when available, audio transcription when caption is absent, optional visual extraction, LLM analysis, and persistence. Source adapters implement the individual operations and may perform their own prerequisite steps.

Examples:

```text
Xiaoyuzhou episode: metadata + audio + transcription
Bilibili video: metadata + caption + audio + transcription + optional video/keyframes
Xiaohongshu article: metadata + article_body
Xiaohongshu video: metadata + caption/audio + transcription + optional video/keyframes
Image upload: ocr + analysis
```

### 4. Preserve existing behavior during migration

The first implementation keeps the current database columns and admin API response shape. `media_type` remains populated for compatibility, but the internal planner no longer uses `video` as a synonym for every media-processing route. Existing Bilibili, YouTube, Douyin, Kuaishou, image, and article flows keep their current fallback and evidence persistence behavior.

## Error handling

Source-specific access and acquisition errors remain wrapped as `MediaExtractionError` with a safe phase and route. Missing captions remain a normal capability absence and trigger audio transcription. Unsupported capabilities fail with a clear, source-specific error rather than an attribute error.

## Non-goals

- No Cookie import or persistence.
- No CAPTCHA or verification bypass.
- No browser automation.
- No replacement of the existing evidence, LLM, or admin persistence model.
- No full implementation of Douyin `msToken` in this refactor; only the extension boundary is defined.
