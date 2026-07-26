# TripGuard MVP Backend

FastAPI backend for the TripGuard travel knowledge base MVP.

## Scope

- `POST /sources/collect`: save a parsed URL/image/text source after LLM travel relevance analysis.
- `POST /sources/analyze`: analyze a parsed URL/image/text source without saving it.
- `GET /sources`: list saved cards without exposing full body text.
- `GET /sources/{source_id}`: fetch one source including body text.
- `POST /chat/recommend`: retrieve saved sources and ask the LLM to generate a recommendation with trusted source citations.
- `GET /client/config`: client-facing runtime config, including the public API base URL.
- `GET /health`: runtime health metadata.

The service is intentionally isolated from other projects on the host:

- Compose project name: `tripguard_mvp`
- Docker network: `tripguard_mvp_net`
- Backend port: `18080` by default
- Public API base URL: `https://trip.aatroxli.site:1221`
- LLM endpoint: LM Studio's OpenAI-compatible API at `http://host.docker.internal:11434/v1`, reached through the SSH reverse tunnel on `home-lan`.

## Local Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:18080/health
```

Before starting the backend, keep the SSH reverse tunnel open so that
`home-lan:127.0.0.1:11434` forwards to the local LM Studio server at
`127.0.0.1:1234`, with `gemma4:latest` loaded:

```bash
ssh -N \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:11434:127.0.0.1:1234 \
  home-lan
```

If the model identifier differs in LM Studio, edit `.env` and set
`TRIPGUARD_LLM_MODEL` to the loaded model identifier.

## Example Requests

Client config:

```bash
curl https://trip.aatroxli.site:1221/client/config
```

Analyze without saving:

```bash
curl -X POST https://trip.aatroxli.site:1221/sources/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "input_type": "url",
    "url": "https://xhslink.com/example",
    "title": "东京表参道超好吃的舒芙蕾松饼",
    "body_text": "这家店在表参道附近，很出片，适合下午茶，排队大概 30 分钟。",
    "source_platform": "xhs",
    "cover_image_url": "https://img.example/cover.jpg"
  }'
```

```bash
curl -X POST https://trip.aatroxli.site:1221/sources/collect \
  -H 'Content-Type: application/json' \
  -d '{
    "input_type": "url",
    "url": "https://xhslink.com/example",
    "title": "东京表参道超好吃的舒芙蕾松饼",
    "body_text": "这家店在表参道附近，很出片，适合下午茶，排队大概 30 分钟。",
    "source_platform": "xhs",
    "cover_image_url": "https://img.example/cover.jpg"
  }'
```

```bash
curl -X POST https://trip.aatroxli.site:1221/chat/recommend \
  -H 'Content-Type: application/json' \
  -d '{"message":"东京 3 天想逛吃拍照，别太游客"}'
```
