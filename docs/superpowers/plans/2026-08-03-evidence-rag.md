# TripGuard Evidence RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace whole-source prompt context with provenance-preserving LlamaIndex evidence retrieval while preserving the existing `/chat/recommend` API contract.

**Architecture:** `TravelSource` and `SourceEvidence` remain SQLite source-of-truth. A `RagIndex` builds one LlamaIndex document per source, persists its vector/doc/index stores under a configured directory, and returns bounded `RetrievedEvidence` records. The endpoint keeps `TravelQuery` SQL filtering as a deterministic candidate gate, uses the RAG index only to rank/snippet those candidates, and falls back to the current source search if RAG is unavailable.

**Tech Stack:** FastAPI, SQLModel, Pydantic, LlamaIndex core, Ollama embeddings, Python 3.12, pytest.

---

### Task 1: Add RAG configuration and testable evidence contracts

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/config.py`
- Create: `app/rag.py`
- Create: `tests/test_rag.py`

- [ ] **Step 1: Write failing evidence-document tests**

```python
def test_document_keeps_source_and_evidence_provenance() -> None:
    source = make_source(source_id="src_tokyo", title="浅草早餐")
    evidence = make_evidence(source_id="src_tokyo", evidence_id="evd_asakusa", full_text="浅草寺附近早上八点开始排队。")

    document = build_source_document(source, evidence)

    assert document.id_ == "src_tokyo"
    assert document.metadata["source_id"] == "src_tokyo"
    assert document.metadata["evidence_id"] == "evd_asakusa"
    assert document.text == evidence.full_text
```

- [ ] **Step 2: Run the focused test and observe the missing-import failure**

Run: `pytest tests/test_rag.py::test_document_keeps_source_and_evidence_provenance -q`

Expected: FAIL because `app.rag` does not exist.

- [ ] **Step 3: Add minimal dependencies and contracts**

Add `llama-index-core` and `llama-index-embeddings-ollama` to runtime dependencies. Add `rag_persist_dir`, `rag_embedding_model`, and `rag_top_k` to `Settings`. Implement `RetrievedEvidence` and `build_source_document(source, evidence)` in `app/rag.py`; metadata must include source id, evidence id, URL, title, destination, category, tags, and segment provenance.

- [ ] **Step 4: Run the focused test and commit**

Run: `pytest tests/test_rag.py::test_document_keeps_source_and_evidence_provenance -q`

Expected: PASS.

```bash
git add pyproject.toml app/config.py app/rag.py tests/test_rag.py
git commit -m "feat: add RAG evidence contracts"
```

### Task 2: Persist, backfill, and incrementally synchronize the index

**Files:**
- Modify: `app/rag.py`
- Create: `app/rag_backfill.py`
- Modify: `app/main.py`
- Test: `tests/test_rag.py`

- [ ] **Step 1: Write failing index lifecycle tests**

```python
def test_upsert_replaces_existing_source_nodes(tmp_path: Path) -> None:
    index = RagIndex.for_test(tmp_path)
    index.upsert_source(make_source(body_text="旧内容"), make_evidence(full_text="旧内容"))
    index.upsert_source(make_source(body_text="新内容"), make_evidence(full_text="新内容"))

    results = index.retrieve("新内容", allowed_source_ids={"src_tokyo"})

    assert [item.text for item in results] == ["新内容"]
```

- [ ] **Step 2: Run the focused test and observe the missing-index failure**

Run: `pytest tests/test_rag.py::test_upsert_replaces_existing_source_nodes -q`

Expected: FAIL because `RagIndex` is not implemented.

- [ ] **Step 3: Implement persistent index and backfill command**

Implement `RagIndex` with Ollama embeddings in production and a deterministic injected embedding model in tests. Persist LlamaIndex storage under `Settings.rag_persist_dir`. Upsert must delete the old `ref_doc_id=source_id` before inserting the current evidence document. Add `python -m app.rag_backfill` to iterate existing `TravelSource` rows, load matching `SourceEvidence`, and index every source without mutating SQLite data.

- [ ] **Step 4: Synchronize successful source writes**

Add a single helper in `main.py` that looks up saved evidence and invokes `app.state.rag_index.upsert_source(...)` after a source transaction commits. Call it for direct URL collection, direct image collection, and completed asynchronous ingestion. Catch index errors, log them, and keep source ingestion successful; the backfill command repairs missed index writes.

- [ ] **Step 5: Run lifecycle tests and commit**

Run: `pytest tests/test_rag.py -q`

Expected: PASS.

```bash
git add app/rag.py app/rag_backfill.py app/main.py tests/test_rag.py
git commit -m "feat: persist and synchronize evidence RAG index"
```

### Task 3: Retrieve bounded evidence in recommendations

**Files:**
- Modify: `app/main.py`
- Modify: `app/llm.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing API retrieval test**

```python
def test_recommend_passes_retrieved_evidence_not_whole_source(tmp_path: Path) -> None:
    retriever = FakeRagIndex([RetrievedEvidence(source_id="src_tokyo", evidence_id="evd_1", text="表参道下午茶需排队。")])
    client, llm = make_client(tmp_path, rag_index=retriever)

    response = client.post("/chat/recommend", json={"message": "东京下午茶"})

    assert response.status_code == 200
    assert llm.contexts[0]["body_text"] == "表参道下午茶需排队。"
    assert response.json()["used_sources"][0]["source_id"] == "src_tokyo"
```

- [ ] **Step 2: Run the focused test and observe the missing injection/context failure**

Run: `pytest tests/test_api.py::test_recommend_passes_retrieved_evidence_not_whole_source -q`

Expected: FAIL because `create_app` has no RAG dependency and recommendation contexts still use whole source text.

- [ ] **Step 3: Inject RAG and apply hybrid retrieval**

Extend `create_app` with an optional `rag_index` dependency for tests. After existing `TravelQuery` and SQL `search_sources`, call `rag_index.retrieve(message, allowed_source_ids=...)`. Build LLM contexts only from returned snippets and preserve `source_id`; when the index is disabled, empty, or raises, retain the current whole-source context path. Do not alter `ChatRecommendResponse`.

- [ ] **Step 4: Run API regression tests and commit**

Run: `pytest tests/test_api.py -q`

Expected: PASS.

```bash
git add app/main.py app/llm.py tests/test_api.py
git commit -m "feat: ground recommendations with RAG evidence"
```

### Task 4: Validate Python 3.12 runtime and deployment readiness

**Files:**
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Test: all `tests/`

- [ ] **Step 1: Add non-secret RAG environment documentation**

Document the embedding model and persisted RAG directory. Configure the Compose service to persist the index under `/data/rag` and use `host.docker.internal` for Ollama embeddings.

- [ ] **Step 2: Run the complete suite in Python 3.12**

Run from `claw` with a mounted worktree in `python:3.12-slim` after installing `.[dev]`.

Expected: all tests pass.

- [ ] **Step 3: Smoke-test index backfill in an empty temporary database**

Run: `python -m app.rag_backfill --help` and an integration test that writes a source, backfills, reloads the persisted index, and retrieves its evidence.

- [ ] **Step 4: Commit deployment configuration**

```bash
git add .env.example docker-compose.yml tests app
git commit -m "chore: configure persistent RAG runtime"
```
