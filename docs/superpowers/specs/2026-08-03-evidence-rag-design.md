# TripGuard Evidence RAG Design

## Scope

Add a LlamaIndex-backed evidence retrieval layer to the existing FastAPI backend. This phase does not add LangChain, an Agent loop, policy search, or A2UI responses.

## Current boundary

`TravelSource` is the saved travel record and `SourceEvidence` holds its full text and optional segments. `/chat/recommend` currently parses a `TravelQuery`, filters `TravelSource` with SQL, then supplies whole source bodies to the LLM.

## Target flow

1. Saving an accepted source creates or updates its LlamaIndex document.
2. One document has `ref_doc_id = source_id`; nodes preserve `source_id`, `evidence_id`, original URL, destination, category, tags, and segment position.
3. The persisted index lives under the existing `/data` Docker volume so it survives container replacement.
4. A backfill command reindexes all existing saved sources without changing source records.
5. `EvidenceRetriever` receives the user message plus existing `TravelQuery` filters. It first applies the SQL filters, then performs vector retrieval over only those source ids. It returns bounded evidence snippets and their provenance.
6. `/chat/recommend` keeps its request and response shape, but uses retrieved snippets rather than whole source bodies as LLM context. `used_source_ids` are still hydrated from the database before the API response is sent.

## Dependency and runtime choice

- Add `llama-index-core` and `llama-index-embeddings-ollama`.
- Use the existing Ollama host as an embedding provider, configured independently from the chat model.
- Default the embedding model to `nomic-embed-text`; deployment must ensure that model is available in Ollama before enabling RAG.
- Use LlamaIndex's persisted local storage for this MVP. Do not add Qdrant, Postgres, or a second database in this phase.

## Safety and correctness rules

- The model never receives a source title, URL, or citation that is not backed by a retrieved node.
- Every retrieved node must carry `source_id`; API citations continue to be derived from `TravelSource`, never copied from model text.
- If the index is unavailable or empty, `/chat/recommend` falls back to the existing SQL retrieval path so the existing feature remains available.
- The index is an acceleration and retrieval artifact, not the source of truth. SQLite `TravelSource` and `SourceEvidence` remain authoritative.

## Verification

- Unit tests cover document construction, SQL-constrained candidate selection, provenance preservation, and index upsert/delete behavior.
- API tests prove a semantically relevant evidence snippet is supplied to the recommender while the returned citation remains an existing saved source.
- Run authoritative tests in a Python 3.12 Docker environment on `claw`; the local Python 3.14 virtual environment is not a valid baseline.
