# TripGuard 视频 Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an asynchronous ingestion API that converts supported public video links or uploaded videos into persisted textual evidence and TravelSource cards.

**Architecture:** The API creates an `IngestionJob`; an in-process executor runs `IngestionService`, which classifies the input and dispatches a `VideoPipeline`. Platform adapters only normalize URLs, provide public subtitles and obtain temporary audio. A shared media acquirer, faster-whisper transcriber and evidence persistence layer own all cross-platform behavior.

**Tech Stack:** FastAPI, SQLModel/SQLite, Pydantic v2, httpx, yt-dlp, faster-whisper, FFmpeg, pytest.

---

## Planned file structure

```text
app/
  ingestion/
    __init__.py                    # public construction helpers
    domain.py                      # immutable pipeline values, enums and adapter protocol
    classifier.py                  # URL/media classification and platform matching
    media.py                       # yt-dlp and FFmpeg temporary-audio acquisition
    transcriber.py                 # faster-whisper implementation and transcript normalization
    pipeline.py                    # subtitle-first video orchestration and temporary cleanup
    service.py                     # persisted job state machine and Ollama/TravelSource bridge
    adapters/
      __init__.py                  # adapter registry
      base.py                      # common URL helpers and abstract adapter base
      youtube.py                   # YouTube captions and yt-dlp metadata
      bilibili.py                  # Bilibili public captions and yt-dlp metadata
      douyin.py                    # public Douyin/TikTok probe with generic media fallback
      kuaishou.py                  # public Kuaishou probe with generic media fallback
      upload.py                    # multipart-uploaded local video adapter
  models.py                        # IngestionJob and SourceEvidence SQLModel tables
  schemas.py                       # ingestion API contracts
  repository.py                    # job/evidence persistence mapping
  config.py                        # worker, temporary directory and Whisper settings
  main.py                          # route registration and executor lifecycle
tests/
  test_ingestion_domain.py
  test_ingestion_pipeline.py
  test_ingestion_service.py
  test_ingestion_api.py
  fixtures/ingestion/*.json
```

Existing `TravelSource`, `/sources/*` endpoints, recommendation behavior and App code remain compatible. `Dockerfile` installs FFmpeg and `pyproject.toml` declares runtime Python dependencies.

### Task 1: Add persistence models and immutable API types

**Files:**
- Modify: `app/models.py`
- Modify: `app/schemas.py`
- Create: `app/ingestion/__init__.py`
- Create: `app/ingestion/domain.py`
- Test: `tests/test_ingestion_domain.py`

- [ ] **Step 1: Write failing model/schema tests**

```python
from app.ingestion.domain import MediaType, Transcript, TranscriptSegment
from app.schemas import CreateIngestionRequest


def test_create_ingestion_request_accepts_url_only() -> None:
    request = CreateIngestionRequest(url="https://youtu.be/abc123")
    assert request.input_type == "url"
    assert request.url == "https://youtu.be/abc123"


def test_transcript_requires_ordered_segments() -> None:
    transcript = Transcript(
        language="zh",
        origin="platform_caption",
        full_text="第一句 第二句",
        segments=(
            TranscriptSegment(start_seconds=0, end_seconds=2, text="第一句"),
            TranscriptSegment(start_seconds=2, end_seconds=4, text="第二句"),
        ),
    )
    assert transcript.media_type is MediaType.VIDEO
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `pytest tests/test_ingestion_domain.py -v`

Expected: FAIL during collection because `app.ingestion` does not exist.

- [ ] **Step 3: Define the domain values and SQL tables**

Create `app/ingestion/domain.py` with `MediaType`, `JobStatus`, `JobStage`, `EvidenceOrigin`, `TranscriptSegment`, `Transcript`, `MediaMetadata`, `TemporaryAudio`, and the `VideoAdapter` protocol. `Transcript` must reject empty `full_text`, derive `media_type = MediaType.VIDEO`, and store `segments` as an immutable tuple.

Append these SQLModel tables to `app/models.py`:

```python
class IngestionJob(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(default_factory=new_job_id, index=True, unique=True)
    input_type: str = Field(index=True)
    original_url: str | None = Field(default=None, index=True)
    canonical_url: str | None = Field(default=None, index=True)
    source_platform: str | None = Field(default=None, index=True)
    media_type: str = Field(default="unknown", index=True)
    status: str = Field(default="queued", index=True)
    stage: str = Field(default="queued", index=True)
    attempt_count: int = Field(default=0)
    max_attempts: int = Field(default=2)
    error_code: str | None = None
    error_message: str | None = None
    source_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class SourceEvidence(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    evidence_id: str = Field(default_factory=new_evidence_id, index=True, unique=True)
    source_id: str = Field(index=True)
    kind: str = Field(default="transcript")
    origin: str = Field(index=True)
    language: str | None = None
    full_text: str
    segments: list[dict[str, object]] = Field(default_factory=list, sa_column=Column(JSON))
    metadata_json: dict[str, object] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now, index=True)
```

Add Pydantic request/response schemas: `CreateIngestionRequest`, `IngestionAcceptedResponse`, and `IngestionStatusResponse`. URL requests require a non-empty `url`; uploads will use a separate multipart endpoint schema-free at the transport boundary.

- [ ] **Step 4: Run the domain tests**

Run: `pytest tests/test_ingestion_domain.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the persistence contracts**

```bash
git add app/models.py app/schemas.py app/ingestion tests/test_ingestion_domain.py
git commit -m "feat: add ingestion persistence contracts"
```

### Task 2: Classify URLs and register adapters

**Files:**
- Create: `app/ingestion/classifier.py`
- Create: `app/ingestion/adapters/__init__.py`
- Create: `app/ingestion/adapters/base.py`
- Create: `app/ingestion/adapters/youtube.py`
- Create: `app/ingestion/adapters/bilibili.py`
- Create: `app/ingestion/adapters/douyin.py`
- Create: `app/ingestion/adapters/kuaishou.py`
- Create: `app/ingestion/adapters/upload.py`
- Create: `tests/fixtures/ingestion/urls.json`
- Modify: `tests/test_ingestion_domain.py`

- [ ] **Step 1: Add failing classification cases**

```python
import pytest
from app.ingestion.classifier import ResourceClassifier
from app.ingestion.domain import MediaType


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://youtu.be/abc123", "youtube"),
        ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
        ("https://v.douyin.com/abc/", "douyin"),
        ("https://v.kuaishou.com/abc", "kuaishou"),
    ],
)
def test_classifier_identifies_video_platform(url: str, platform: str) -> None:
    descriptor = ResourceClassifier.default().classify_url(url)
    assert descriptor.media_type is MediaType.VIDEO
    assert descriptor.source_platform == platform
```

- [ ] **Step 2: Run the classification cases to verify failure**

Run: `pytest tests/test_ingestion_domain.py::test_classifier_identifies_video_platform -v`

Expected: FAIL because `ResourceClassifier` does not exist.

- [ ] **Step 3: Implement registry-first classification stubs**

Implement `BaseVideoAdapter` in `adapters/base.py`, with hostname-based `matches`, URL validation using `urllib.parse.urlsplit`, and no network call. Create lightweight `YoutubeAdapter`, `BilibiliAdapter`, `DouyinAdapter`, `KuaishouAdapter`, and `UploadedVideoAdapter` classes that only provide matching and canonicalization; each media-facing method raises `NotImplementedError` until its dedicated task. Register those instances in `adapters/__init__.py`. `ResourceClassifier.default()` receives the registry and returns a `ResourceDescriptor` with canonical URL equal to the submitted URL until a platform adapter resolves a short-link during extraction.

Use the exact hostname sets:

```python
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com", "b23.tv"}
DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "v.douyin.com", "tiktok.com", "www.tiktok.com"}
KUAISHOU_HOSTS = {"kuaishou.com", "www.kuaishou.com", "v.kuaishou.com"}
```

All other HTTP(S) URLs classify to `ARTICLE`; malformed or non-HTTP URL input raises `ValueError("unsupported URL scheme")`.

- [ ] **Step 4: Run all domain tests**

Run: `pytest tests/test_ingestion_domain.py -v`

Expected: PASS.

- [ ] **Step 5: Commit classification**

```bash
git add app/ingestion tests/test_ingestion_domain.py tests/fixtures/ingestion/urls.json
git commit -m "feat: classify supported video links"
```

### Task 3: Add safe temporary-media and transcription primitives

**Files:**
- Modify: `app/config.py`
- Create: `app/ingestion/media.py`
- Create: `app/ingestion/transcriber.py`
- Modify: `tests/test_ingestion_domain.py`

- [ ] **Step 1: Write cleanup and normalization tests**

```python
from pathlib import Path
from app.ingestion.media import JobDirectory
from app.ingestion.transcriber import normalize_whisper_segments


def test_job_directory_always_removes_audio(tmp_path: Path) -> None:
    with JobDirectory(tmp_path, "ing_test") as job_dir:
        (job_dir / "audio.m4a").write_bytes(b"temporary")
    assert not (tmp_path / "ing_test").exists()


def test_normalize_whisper_segments_discards_blank_text() -> None:
    transcript = normalize_whisper_segments(
        language="zh", segments=[(0.0, 1.0, " 第一段 "), (1.0, 2.0, " ")]
    )
    assert transcript.full_text == "第一段"
    assert len(transcript.segments) == 1
    assert transcript.origin == "asr"
```

- [ ] **Step 2: Run the new primitive tests to verify failure**

Run: `pytest tests/test_ingestion_domain.py -v`

Expected: FAIL because media/transcriber modules are missing.

- [ ] **Step 3: Implement temporary material ownership**

Add `ingestion_temp_dir`, `ingestion_max_attempts`, `whisper_model`, `whisper_device`, and `whisper_compute_type` to `Settings`. Implement `JobDirectory` as a context manager under `ingestion_temp_dir / job_id`, rejecting job IDs that contain path separators and calling `shutil.rmtree(path, ignore_errors=True)` in `__exit__`.

Define `MediaAcquirer.acquire_audio(url, job_dir)` and `FfmpegAudioExtractor.extract(input_path, job_dir)` behind protocols. The yt-dlp implementation must use an output template inside `job_dir`, `noplaylist=True`, `quiet=True`, and `format="bestaudio/best"`; it returns only a `TemporaryAudio` path that is inside `job_dir`. It must never configure cookies, browser profile import, proxy credentials, or verification bypass behavior.

Implement `FasterWhisperTranscriber.transcribe(audio_path)` lazily: import and construct `WhisperModel` on first call, then map each emitted segment to `TranscriptSegment`. Do not construct/download the model during API startup or unit tests.

- [ ] **Step 4: Run the primitive tests**

Run: `pytest tests/test_ingestion_domain.py -v`

Expected: PASS.

- [ ] **Step 5: Commit primitive ownership**

```bash
git add app/config.py app/ingestion/media.py app/ingestion/transcriber.py tests/test_ingestion_domain.py
git commit -m "feat: add temporary media and whisper primitives"
```

### Task 4: Implement YouTube and Bilibili caption adapters

**Files:**
- Modify: `app/ingestion/adapters/youtube.py`
- Modify: `app/ingestion/adapters/bilibili.py`
- Create: `tests/test_ingestion_pipeline.py`
- Create: `tests/fixtures/ingestion/youtube_caption.json`
- Create: `tests/fixtures/ingestion/bilibili_caption.json`

- [ ] **Step 1: Write the caption-priority tests**

```python
from app.ingestion.adapters.youtube import YoutubeAdapter


def test_youtube_prefers_manual_caption_over_auto_caption() -> None:
    adapter = YoutubeAdapter(transcript_client=FakeYoutubeTranscriptClient())
    transcript = adapter.fetch_caption("https://youtu.be/abc123")
    assert transcript is not None
    assert transcript.origin == "platform_caption"
    assert transcript.language == "zh-Hans"


def test_bilibili_returns_none_when_public_caption_is_absent() -> None:
    adapter = BilibiliAdapter(player_client=FakeBilibiliPlayerClient(caption=None))
    assert adapter.fetch_caption("https://www.bilibili.com/video/BV1xx411c7mD") is None
```

- [ ] **Step 2: Run the caption tests to verify failure**

Run: `pytest tests/test_ingestion_pipeline.py -v`

Expected: FAIL because the classification stubs raise `NotImplementedError` for `fetch_caption`.

- [ ] **Step 3: Implement public caption extraction only**

`YoutubeAdapter.fetch_caption` uses an injected transcript client. It tries preferred languages `zh-Hans`, `zh`, `zh-CN`, `zh-TW`, `en`, `en-US`, `ja`, selecting a manually created caption before an automatic caption and returning `None` when no public caption exists.

`BilibiliAdapter.fetch_caption` uses an injected public-player client to obtain the selected subtitle track and maps its body list to timestamped segments. If metadata or the public subtitle URL is unavailable, it returns `None`; it does not import BiliNote's risk-control patch, create cookie files or synthesize signed requests.

Both adapters use `MediaAcquirer` for metadata/audio fallback and normalize all captions into the Task 1 `Transcript` type.

- [ ] **Step 4: Run caption tests and existing tests**

Run: `pytest tests/test_ingestion_pipeline.py tests/test_api.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the public caption adapters**

```bash
git add app/ingestion/adapters/youtube.py app/ingestion/adapters/bilibili.py tests/test_ingestion_pipeline.py tests/fixtures/ingestion
git commit -m "feat: add youtube and bilibili caption adapters"
```

### Task 5: Implement Douyin, Kuaishou and upload adapters

**Files:**
- Modify: `app/ingestion/adapters/douyin.py`
- Modify: `app/ingestion/adapters/kuaishou.py`
- Modify: `app/ingestion/adapters/upload.py`
- Modify: `app/ingestion/adapters/__init__.py`
- Modify: `tests/test_ingestion_pipeline.py`

- [ ] **Step 1: Write the generic fallback tests**

```python
from pathlib import Path
from app.ingestion.adapters.upload import UploadedVideoAdapter


def test_uploaded_video_uses_ffmpeg_extractor(tmp_path: Path) -> None:
    uploaded = tmp_path / "input.mp4"
    uploaded.write_bytes(b"not-a-real-video")
    adapter = UploadedVideoAdapter(audio_extractor=FakeFfmpegExtractor())
    audio = adapter.acquire_audio(uploaded, tmp_path / "job")
    assert audio.path == tmp_path / "job" / "audio.m4a"


def test_douyin_absent_public_caption_falls_back_without_private_state() -> None:
    adapter = DouyinAdapter(media_acquirer=FakeMediaAcquirer())
    assert adapter.fetch_caption("https://v.douyin.com/abc/") is None
    assert adapter.media_acquirer.calls == []
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_ingestion_pipeline.py -v`

Expected: FAIL because the classification stubs do not implement audio acquisition.

- [ ] **Step 3: Implement the remaining adapters**

`DouyinAdapter` and `KuaishouAdapter` implement hostname matching, URL canonicalization through an ordinary HTTP redirect resolver with a bounded timeout, metadata/audio acquisition through the generic `MediaAcquirer`, and return `None` for captions when no documented public caption endpoint is available. They must not copy BiliNote's embedded tokens, request signing, Cookie configuration or browser automation.

`UploadedVideoAdapter` accepts a `Path` already copied into the task directory, obtains metadata via `ffprobe` and extracts a task-local audio file with `FfmpegAudioExtractor`. It rejects files outside the active job directory.

- [ ] **Step 4: Run the adapter test suite**

Run: `pytest tests/test_ingestion_pipeline.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the remaining adapters**

```bash
git add app/ingestion/adapters tests/test_ingestion_pipeline.py
git commit -m "feat: add public short-video and upload adapters"
```

### Task 6: Orchestrate subtitle-first video evidence extraction

**Files:**
- Create: `app/ingestion/pipeline.py`
- Modify: `tests/test_ingestion_pipeline.py`

- [ ] **Step 1: Add failing end-to-end pipeline tests with fakes**

```python
def test_pipeline_skips_audio_when_caption_exists(tmp_path: Path) -> None:
    adapter = FakeAdapter(caption=caption("平台字幕"))
    acquirer = FakeMediaAcquirer()
    result = VideoPipeline(adapter=adapter, transcriber=FakeTranscriber(), temp_root=tmp_path).extract("https://youtu.be/x", "ing_x")
    assert result.transcript.origin == "platform_caption"
    assert acquirer.calls == []
    assert not (tmp_path / "ing_x").exists()


def test_pipeline_transcribes_temporary_audio_when_caption_absent(tmp_path: Path) -> None:
    result = VideoPipeline(adapter=FakeAdapter(caption=None), transcriber=FakeTranscriber("ASR 文本"), temp_root=tmp_path).extract("https://youtu.be/x", "ing_x")
    assert result.transcript.origin == "asr"
    assert result.transcript.full_text == "ASR 文本"
    assert not (tmp_path / "ing_x").exists()
```

- [ ] **Step 2: Run the pipeline tests to verify failure**

Run: `pytest tests/test_ingestion_pipeline.py -v`

Expected: FAIL because `VideoPipeline` is missing.

- [ ] **Step 3: Implement `VideoPipeline.extract`**

Implement the ordered algorithm exactly:

```python
with JobDirectory(self._temp_root, job_id) as job_dir:
    metadata = adapter.fetch_metadata(url)
    transcript = adapter.fetch_caption(url)
    if transcript is None:
        audio = adapter.acquire_audio(url, job_dir)
        transcript = self._transcriber.transcribe(audio.path)
    return EvidenceBundle(metadata=metadata, transcript=transcript)
```

The implementation validates non-empty transcript text, wraps expected public-access failures in typed `IngestionError` codes, and relies on `JobDirectory` rather than ad-hoc cleanup. It must not call video download or key-frame extraction.

- [ ] **Step 4: Run pipeline and legacy API tests**

Run: `pytest tests/test_ingestion_pipeline.py tests/test_api.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the pipeline**

```bash
git add app/ingestion/pipeline.py tests/test_ingestion_pipeline.py
git commit -m "feat: extract subtitle-first video evidence"
```

### Task 7: Persist jobs/evidence and bridge to existing Ollama analysis

**Files:**
- Modify: `app/repository.py`
- Create: `app/ingestion/service.py`
- Modify: `tests/test_ingestion_service.py`

- [ ] **Step 1: Write failing successful-job and rejected-content tests**

```python
def test_service_saves_source_and_evidence_for_travel_video(session: Session) -> None:
    service = make_service(session, llm=FakeLlmClient(), pipeline=FakePipeline("东京咖啡路线"))
    job = service.run("ing_test")
    assert job.status == "succeeded"
    assert job.source_id is not None
    evidence = get_source_evidence(session, job.source_id)
    assert evidence.full_text == "东京咖啡路线"


def test_service_marks_non_travel_video_succeeded_without_source(session: Session) -> None:
    service = make_service(session, llm=NonTravelLlmClient(), pipeline=FakePipeline("股票讲解"))
    job = service.run("ing_test")
    assert job.status == "succeeded"
    assert job.source_id is None
```

- [ ] **Step 2: Run service tests to verify failure**

Run: `pytest tests/test_ingestion_service.py -v`

Expected: FAIL because repository functions and `IngestionService` are absent.

- [ ] **Step 3: Implement state transitions and persistence**

Add repository functions `get_job`, `create_job`, `update_job_state`, `save_source_evidence`, and `get_source_evidence`. All updates commit and refresh their row before returning it.

`IngestionService.run(job_id)` performs `classifying`, `extracting`/`transcribing`, `analyzing`, and `saving` transitions. It calls `OllamaLlmClient.analyze_source` with the media title plus transcript full text, reuses `normalize_analysis`, creates `TravelSource` only if travel-related, and always writes `SourceEvidence` for a successfully extracted item. A non-travel item is terminal `succeeded` with no `source_id`; extraction/ASR/LLM errors are terminal `failed` with a typed safe code.

- [ ] **Step 4: Run service tests**

Run: `pytest tests/test_ingestion_service.py tests/test_api.py -v`

Expected: PASS.

- [ ] **Step 5: Commit service persistence**

```bash
git add app/repository.py app/ingestion/service.py tests/test_ingestion_service.py
git commit -m "feat: persist ingestion evidence and analysis"
```

### Task 8: Expose asynchronous routes and bounded executor

**Files:**
- Modify: `app/main.py`
- Modify: `app/schemas.py`
- Modify: `app/config.py`
- Create: `tests/test_ingestion_api.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write API tests using an injected immediate executor**

```python
def test_create_ingestion_returns_202_and_status(tmp_path: Path) -> None:
    client = make_ingestion_client(tmp_path, executor=ImmediateExecutor())
    response = client.post("/ingestions", json={"url": "https://youtu.be/abc123"})
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    status = client.get(f"/ingestions/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] in {"queued", "succeeded"}


def test_unknown_ingestion_returns_404(tmp_path: Path) -> None:
    assert make_ingestion_client(tmp_path).get("/ingestions/ing_missing").status_code == 404
```

- [ ] **Step 2: Run API tests to verify failure**

Run: `pytest tests/test_ingestion_api.py -v`

Expected: FAIL with 404 because ingestion routes do not exist.

- [ ] **Step 3: Add executor lifecycle and routes**

Add a bounded `ThreadPoolExecutor(max_workers=settings.ingestion_worker_count)` to `app.state` in `create_app`, and shut it down from FastAPI lifespan. `POST /ingestions` validates/classifies the URL, persists a queued job, submits only `service.run(job_id)` and returns `IngestionAcceptedResponse` with HTTP 202. `GET /ingestions/{job_id}` maps the row to `IngestionStatusResponse` and raises the existing 404 style when absent.

Do not use FastAPI `BackgroundTasks` for pipeline work: it lacks bounded worker ownership and shutdown behavior. Route tests inject `ImmediateExecutor` and fake adapter/pipeline dependencies through `app.state`.

- [ ] **Step 4: Run all API tests**

Run: `pytest tests/test_ingestion_api.py tests/test_api.py -v`

Expected: PASS.

- [ ] **Step 5: Commit API and executor**

```bash
git add app/main.py app/schemas.py app/config.py tests/test_ingestion_api.py tests/test_api.py
git commit -m "feat: expose asynchronous ingestion API"
```

### Task 9: Add multipart video upload safely

**Files:**
- Modify: `app/main.py`
- Modify: `app/ingestion/service.py`
- Modify: `tests/test_ingestion_api.py`

- [ ] **Step 1: Write failing upload boundary tests**

```python
def test_video_upload_creates_job_without_exposing_server_path(tmp_path: Path) -> None:
    client = make_ingestion_client(tmp_path, executor=RecordingExecutor())
    response = client.post(
        "/ingestions/upload-video",
        files={"file": ("route.mp4", b"video-bytes", "video/mp4")},
    )
    assert response.status_code == 202
    assert "path" not in response.json()


def test_upload_rejects_non_video_content_type(tmp_path: Path) -> None:
    response = make_ingestion_client(tmp_path).post(
        "/ingestions/upload-video", files={"file": ("note.txt", b"x", "text/plain")}
    )
    assert response.status_code == 415
```

- [ ] **Step 2: Run upload tests to verify failure**

Run: `pytest tests/test_ingestion_api.py -v`

Expected: FAIL with 404 because upload route does not exist.

- [ ] **Step 3: Implement the multipart boundary**

`POST /ingestions/upload-video` accepts `UploadFile`; require `content_type.startswith("video/")`, generate a job first, copy the stream to that job's temporary staging directory with a server-generated safe filename, then submit the job. The upload adapter reads only this persisted staging reference held in process memory/service state; no client path is accepted or returned. Enforce `settings.ingestion_max_upload_bytes` while streaming and return 413 when exceeded.

- [ ] **Step 4: Run upload and legacy route tests**

Run: `pytest tests/test_ingestion_api.py tests/test_api.py -v`

Expected: PASS.

- [ ] **Step 5: Commit upload support**

```bash
git add app/main.py app/ingestion/service.py tests/test_ingestion_api.py
git commit -m "feat: accept local video ingestion uploads"
```

### Task 10: Package runtime dependencies and verify the complete backend

**Files:**
- Modify: `pyproject.toml`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `README.md`
- Modify: `tests/test_ingestion_pipeline.py`

- [ ] **Step 1: Add a failing dependency/configuration assertion**

```python
from app.config import Settings


def test_ingestion_settings_have_safe_defaults(tmp_path: Path) -> None:
    settings = Settings(ingestion_temp_dir=str(tmp_path / "tmp"))
    assert settings.ingestion_worker_count == 1
    assert settings.ingestion_max_attempts == 2
    assert settings.ingestion_max_upload_bytes == 1024 * 1024 * 512
```

- [ ] **Step 2: Run it to verify failure**

Run: `pytest tests/test_ingestion_pipeline.py::test_ingestion_settings_have_safe_defaults -v`

Expected: FAIL until all configuration fields exist.

- [ ] **Step 3: Package the runtime deliberately**

Add `yt-dlp>=2025.1.15` and `faster-whisper>=1.1.1` to the main dependencies. In `Dockerfile`, install FFmpeg before `pip install`:

```dockerfile
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*
```

Set these compose variables with safe defaults: `TRIPGUARD_INGESTION_TEMP_DIR=/tmp/tripguard-ingestion`, `TRIPGUARD_INGESTION_WORKER_COUNT=1`, `TRIPGUARD_INGESTION_MAX_ATTEMPTS=2`, `TRIPGUARD_WHISPER_MODEL=base`, `TRIPGUARD_WHISPER_DEVICE=cpu`, and `TRIPGUARD_INGESTION_MAX_UPLOAD_BYTES=536870912`. Do not mount the temporary directory as a persistent volume.

Update README with asynchronous request examples, polling semantics, supported platforms, text-only retention, deployment prerequisites, and clear public-access-only failure behavior.

- [ ] **Step 4: Run the complete Python test suite**

Run: `pytest -v`

Expected: PASS.

- [ ] **Step 5: Build the container**

Run: `docker compose build backend`

Expected: successful image build with FFmpeg installed and Python dependencies resolved.

- [ ] **Step 6: Commit the packaged feature**

```bash
git add pyproject.toml Dockerfile docker-compose.yml README.md tests/test_ingestion_pipeline.py
git commit -m "build: package video ingestion runtime"
```

## Plan self-review

- Spec coverage: Tasks 1, 7 and 8 implement persistent jobs/evidence and the two asynchronous APIs; Tasks 2–6 implement media classification, five adapters, subtitle-first processing, ASR fallback and cleanup; Task 9 handles the required local-video upload boundary; Task 10 provides dependency, deployment and verification coverage.
- Explicit exclusions: no media persistence, no key-frame extraction, no BiliNote product code, no App migration, no `claw` deployment and no authentication/verification bypass are introduced by any task.
- Type consistency: every adapter produces `Transcript`, `MediaMetadata` and `TemporaryAudio` from Task 1; `VideoPipeline` produces `EvidenceBundle`; `IngestionService` owns job transitions and evidence persistence; API routes only expose the Pydantic schemas from Task 1.
- Placeholder check: this plan contains no deferred requirements; each implementation task has a concrete test, command and commit boundary.
