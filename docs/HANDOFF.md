# TripGuard 后端交接

最后更新：2026-07-29（Asia/Shanghai）

本文件是新设备上的 Codex 继续该项目的唯一入口。开始工作前，先完整阅读本文件，再检查本地仓库状态和 claw 运行状态；不要假设远端部署目录是 Git 仓库。

## 仓库与运行位置

- 开发仓库：`/Users/aatroxli/coding/travel/mvp_backend_github`
- GitHub：`https://github.com/4ppleeeee/mvp_backend.git`，分支 `master`
- 运行服务器：SSH 别名 `claw`，或 `ssh -p 12343 aatroxli@openclaw.aatroxli.site`
- claw 部署目录：`/home/aatroxli/tripguard`
- 后端容器：`tripguard-mvp-backend`
- 后端端口：claw 的 `18080` 映射至容器 `8000`

`/Users/aatroxli/coding/travel/mvp_backend/` 是运行时副本，不是常规开发目标；修改和 Git 操作都在 `mvp_backend_github/` 完成。

## 当前能力与数据边界

后端是 FastAPI + SQLModel/SQLite 服务，支持链接、公开网页、小红书公开 H5、图片和视频的异步 Ingestion。视频获取/转写沿用 BiliNote 风格：先取公开字幕或自动字幕，失败后用 yt-dlp 获取临时音频并由 faster-whisper 转写；临时音频、视频、关键帧均不持久化。

小红书链接初始按公开文章抓取，避免把图文笔记错误交给 yt-dlp。异步任务会读取公开页面 HTML；发现 `<video>`、公开 `.mp4`/`.m3u8` 或视频字段标记后，才切换到视频管线。小红书没有受支持的公开字幕接口，因此视频使用临时音频 + Whisper 转写。

视频、文章和图片的已入库资料必须遵守以下边界：

- `IngestionJob.evidence_text`：任务级完整文本，支持排错与重新分析。
- `SourceEvidence.full_text`：资料级权威原始证据。视频是完整字幕/转写，文章是抓取正文，图片是 OCR/识别文本。
- `TravelSource.body_text`：完整正文/转写的副本，保证旧 API 可读。
- `TravelSource.summary_text`：仅由 LLM 生成的卡片摘要，管理后台结果页和推荐的首轮上下文使用它。
- 管理后台展示 `summary_text`，可展开 `SourceEvidence.full_text` 查看完整证据。

不要为了适配小上下文模型而截断或覆盖完整证据。模型只输出结构化字段和有长度上限的摘要；长文本应更换长上下文模型，或在将来做分段检索/合并，原文始终保留。

当前已验证的视频资料：任务 `ing_f6cf9d145b1a4fab`，资料 `src_506da751a09f4a61`，是京都伏见稻荷大社攻略。

## 媒体与封面约束

- 不接入 Cookie、验证码、浏览器自动化或人机验证绕过。
- 不保存原图、网页快照、临时音频、视频或关键帧。
- 只保存封面 URL；普通网页封面经受鉴权的后端代理读取，不持久化图片。
- YouTube 封面仅在 HTTPS 且主机为 `i.ytimg.com` 或 `i3.ytimg.com` 时由浏览器直连。原因是该域名的 DNS 含一个非公网 IPv6 记录，严格 SSRF 防护会拒绝后端代理；不要放宽 `SafeHtmlFetcher._require_public_url()` 的 SSRF 规则来解决此问题。

## 测试

目标运行时为 Python 3.12。优先在 claw Docker 环境运行测试；本机虚拟环境可能不完整或 Python 版本不一致。

```bash
ssh -p 12343 aatroxli@openclaw.aatroxli.site \
  'cd ~/tripguard && docker run --rm -v "$PWD:/app" -w /app tripguard_mvp-backend \
   sh -lc "pip install -q pytest && pytest -q"'
```

截至本文件更新，完整测试为 `70 passed`（存在 FastAPI/Starlette 弃用警告）。每次修改后至少运行相关测试、`git diff --check`，部署后验证 `/health`。

## 安全部署到 claw

远端目录并非可靠 Git checkout。不要在 claw 直接 `git pull`；从已提交的本地开发仓库安全同步源码。绝不覆盖或删除远端 `.env`、`.admin.env`、`data`、`uploads`、`ingestion-tmp` 或数据库。

```bash
cd /Users/aatroxli/coding/travel/mvp_backend_github

COPYFILE_DISABLE=1 tar --no-xattrs \
  --exclude='.git' --exclude='.venv' --exclude='.superpowers' \
  --exclude='.worktrees' --exclude='.pytest_cache' \
  --exclude='.env' --exclude='.admin.env' --exclude='data' \
  --exclude='uploads' --exclude='ingestion-tmp' \
  --exclude='tripguard.db' --exclude='tmp-*' \
  -czf - . | ssh -p 12343 aatroxli@openclaw.aatroxli.site \
  'cd ~/tripguard && tar -xzf - && docker compose build backend && docker compose up -d backend'

ssh -p 12343 aatroxli@openclaw.aatroxli.site \
  'cd ~/tripguard && curl -fsS http://127.0.0.1:18080/health && echo && docker compose ps backend'
```

Docker 重建可能因 `faster-whisper` 及视频依赖下载而耗时数分钟。旧容器在 `docker compose up -d backend` 前应持续可用；不要并发启动多次 build。

## 新设备的首个工作步骤

```bash
cd /Users/aatroxli/coding/travel/mvp_backend_github
git status --short
git log --oneline -8
git pull --ff-only origin master
ssh -p 12343 aatroxli@openclaw.aatroxli.site \
  'cd ~/tripguard && docker compose ps backend && curl -fsS http://127.0.0.1:18080/health'
```

然后向 Codex 提供：

```text
请阅读 /Users/aatroxli/coding/travel/mvp_backend_github/docs/HANDOFF.md，
检查当前 git status 和 claw 容器状态，并严格遵守其中的数据边界、部署方式与安全约束后继续工作。
```

## 禁止事项

- 不记录、输出、提交或询问密码、token、私钥、Cookie、代理凭据或管理员密钥。
- 不对任何工作树执行 `git reset --hard` 或 `git checkout --`。
- 不把 claw 的运行时数据、SQLite 数据库、上传文件或临时媒体提交到 Git。
- 不将公共视频解析问题处理为验证码绕过或登录态自动化。
