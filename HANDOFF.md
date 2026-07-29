# TripGuard / mvp_backend Handoff

更新时间：2026-07-29（Asia/Shanghai）

这份文档可直接提供给另一台 Codex，作为继续工作的上下文。

## 当前工作区

- 本地项目：`/Users/aatroxli/Documents/tripGuard`
- 项目：TripGuard MVP backend
- 技术栈：FastAPI、SQLModel/SQLite、Pydantic v2、yt-dlp、youtube-transcript-api、faster-whisper、FFmpeg、Pillow
- 本地 Python 是 3.9；项目目标运行时是 Python 3.12，完整测试应优先在 Docker 容器中执行。
- 当前工作区有尚未提交的实现修改，这是有意保留的，不要用 `git reset --hard` 或 `git checkout --` 覆盖。

## 已完成的主要能力

现有异步 Ingestion、BiliNote 风格的字幕/转写适配和管理员后台已经完成。视频解析正常流程是：

1. 识别平台并取得元数据；
2. 优先获取平台字幕/转写；
3. 没有可用字幕时，下载临时音频并调用 faster-whisper；
4. 保存归一化后的 evidence，并由现有 LLM/后台流程继续处理。

新增的媒体出口能力包括：

- yt-dlp Node/EJS 配置；
- 显式重试与 primary/fallback 媒体出口；
- `MediaExtractionError`，区分 caption、metadata、audio、video、keyframe 阶段；
- 不接入 Cookie、验证码、浏览器自动化或验证绕过。

## 最新关键帧媒体管线

关键帧功能已实现，但默认关闭，不改变正常字幕/Whisper 流程。

涉及文件：

- `app/ingestion/keyframes.py`：FFprobe 获取时长、FFmpeg 按间隔抽帧、MD5 去重、Pillow 拼接 JPEG 网格、输出图片 Data URL；
- `app/ingestion/media.py`：`download_video()`，使用完整视频格式选择；
- `app/ingestion/adapters/base.py`：新增 `acquire_video()`；
- `app/ingestion/pipeline.py`：`VideoPipeline` 可选返回 `EvidenceBundle.keyframe_images`；
- `app/ingestion/domain.py`：`keyframe_images` 字段；
- `app/config.py`、`app/main.py`、`.env.example`：关键帧配置；
- `pyproject.toml`：增加 `pillow>=11.0.0`；
- `tests/test_keyframes.py`、`tests/test_media_egress.py` 及管线测试：覆盖关键帧和媒体下载行为。

默认配置：

```env
TRIPGUARD_VIDEO_KEYFRAMES_ENABLED=false
TRIPGUARD_VIDEO_FRAME_INTERVAL_SECONDS=6
TRIPGUARD_VIDEO_GRID_ROWS=2
TRIPGUARD_VIDEO_GRID_COLUMNS=2
```

启用方式（仅在明确需要测试视觉输入准备时）：

```env
TRIPGUARD_VIDEO_KEYFRAMES_ENABLED=true
TRIPGUARD_VIDEO_FRAME_INTERVAL_SECONDS=6
TRIPGUARD_VIDEO_GRID_ROWS=2
TRIPGUARD_VIDEO_GRID_COLUMNS=2
```

图片只在任务临时目录中生成，返回 Data URL 后任务目录会自动清理；当前没有视觉模型参与，也没有把图片持久化到数据库或上传目录。

## claw 部署

SSH：

```bash
ssh -p 12343 aatroxli@openclaw.aatroxli.site
```

远端项目目录：`/home/aatroxli/tripguard`

claw 当前不是一个正常的 Git checkout（远端目录没有可用提交历史），因此不要假设可以在远端执行 `git pull`。当前同步方式是从本地通过 SSH 流式 tar 覆盖代码文件，然后重建容器。

安全同步命令（不覆盖远端环境和运行数据）：

```bash
cd /Users/aatroxli/Documents/tripGuard
tar --exclude='./.env' \
  --exclude='./.admin.env' \
  --exclude='./data' \
  --exclude='./uploads' \
  --exclude='./ingestion-tmp' \
  --exclude='*.db' \
  --exclude='./__pycache__' \
  --exclude='./.pytest_cache' \
  --exclude='./.git' \
  --exclude='.DS_Store' \
  --exclude='._*' \
  -czf - . | ssh -p 12343 aatroxli@openclaw.aatroxli.site \
  'tar -xzf - -C /home/aatroxli/tripguard'
```

部署和健康检查：

```bash
ssh -p 12343 aatroxli@openclaw.aatroxli.site \
  'cd /home/aatroxli/tripguard && \
   docker compose build backend && \
   docker compose up -d backend && \
   curl -fsS http://127.0.0.1:18080/health && echo && \
   docker ps --filter name=tripguard-mvp-backend --format "{{.Names}} {{.Status}}"'
```

最近一次验证结果：

```text
{"status":"ok","service":"tripguard-mvp-backend","llm_model":"gemma4:latest"}
tripguard-mvp-backend Up
```

已保护的远端文件/目录：`.env`、数据库、`uploads`、`data`、`ingestion-tmp`。不要删除或重置它们。

## 验证情况

已完成：

- `python3 -m compileall -q app tests`
- `git diff --check`
- claw 容器内真实 Python 3.12 环境下的关键帧提取、管线返回和默认关闭测试；均通过。
- claw backend 健康检查通过。
- 关键帧代码本地与远端 SHA-256 一致。

没有完成：

- 真实公开视频端到端关键帧下载测试。之前选中的 YouTube 视频约 1.14 GiB，下载到约 95% 时为避免继续消耗带宽而中止。不要把这次测试描述为完整端到端成功。
- 本地完整 pytest。原因是本地 Python 3.9 与项目 Python 3.12 目标不一致。

如果继续验证，优先使用较小、公开、可下载的视频，并在 claw 容器内测试；不要使用 Cookie 或验证码绕过。

## Git 状态

最近提交：

```text
18ac11f docs: design keyframe media pipeline
45f81ef docs: specify media egress fallback
0e915db feat: refine ingestion admin task dashboard
25090cc fix: preserve captions when metadata is restricted
fd0166d fix: submit authenticated admin jobs
```

当前未提交修改主要是关键帧实现及此前的媒体出口、后台和 Ingestion 改动。开始新工作前先查看：

```bash
git status --short
git diff -- app/ingestion/pipeline.py app/ingestion/keyframes.py app/ingestion/media.py app/main.py app/config.py
```

不要提交 `.env`、数据库、模型缓存、上传内容或临时视频。

## 继续工作时的约束

- 保持字幕优先、无字幕才 Whisper 的顺序；
- 关键帧默认关闭，除非用户明确要求开启；
- 不接入 Cookie、验证码、浏览器自动化或验证绕过；
- 不把临时视频、单帧、拼图写入持久化数据目录；
- 修改后优先在 Python 3.12 Docker 容器中验证；
- 部署到 claw 前保留 `.env`、数据库、`uploads`、`data`、`ingestion-tmp`；
- 用户偏好是直接实现并部署，不需要先写计划。

## 给新 Codex 的第一条指令示例

```text
请先阅读 /Users/aatroxli/Documents/tripGuard/HANDOFF.md，并检查 git status、最近 git log、关键帧设计文档和当前 claw 容器状态。继续工作时遵守文档中的部署和安全约束，不要重置现有修改。
```
