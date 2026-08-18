# 面向能力的 Ingestion 编排实施计划

> **历史记录。** 本文后续提及的 `app/admin_routes.py` 与 `tests/test_admin.py`
> 已在 2026-08-14 的 Admin Web Split 中移除。后续采集回归应覆盖一般 API 或
> `/admin-api/*` JSON BFF，而不是重新引入嵌入式管理页面。

> **给执行代理的说明：** 必须使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`，按任务逐项执行。所有步骤使用复选框跟踪。

**目标：** 将 Ingestion 调度重构为基于来源适配器和能力的编排，接入小宇宙公开音频，同时保持现有平台行为。

**架构：** 增加来源注册表、探测结果、能力声明和规划器。规划器负责通用证据处理顺序，来源适配器负责各平台具体的访问和资源获取。保留现有持久化字段和兼容 API，并逐步移除 `article/video` 分支对内部调度的耦合。

**技术栈：** Python 3.12、FastAPI、SQLModel、requests、yt-dlp、FFmpeg、faster-whisper、pytest。

---

### 任务 1：增加来源与能力领域类型

**文件：**

- 新建：`app/ingestion/capabilities.py`
- 修改：`app/ingestion/domain.py`
- 测试：`tests/test_ingestion_capabilities.py`

- [ ] **步骤 1：先写失败测试**，覆盖 `ResourceKind`、`Capability`、`SourceProbe` 和确定性的能力排序。
- [ ] **步骤 2：运行** `pytest -q tests/test_ingestion_capabilities.py`，确认新类型尚不存在。
- [ ] **步骤 3：实现**字符串枚举和不可变的探测/计划数据类。保留 `MediaType`，只在 API 边界作为兼容类型使用。
- [ ] **步骤 4：运行聚焦测试**，确认测试通过。
- [ ] **步骤 5：运行** `git diff --check`。

### 任务 2：定义来源适配器边界和注册表

**文件：**

- 新建：`app/ingestion/sources.py`
- 修改：`app/ingestion/adapters/base.py`
- 修改：`app/ingestion/adapters/__init__.py`
- 测试：`tests/test_source_registry.py`

- [ ] **步骤 1：先写失败测试**，覆盖按 URL 主机查找来源、来源探测返回资源类型/能力，以及不支持的操作返回类型化提取错误。
- [ ] **步骤 2：运行聚焦测试**，确认注册表和探测接口尚不存在。
- [ ] **步骤 3：实现** `SourceAdapter` 协议，包含 `matches`、`probe`、`fetch_metadata`、`fetch_caption`、`acquire_audio` 和可选的 `acquire_video`；注册现有视频适配器，并允许来源专属适配器加入。
- [ ] **步骤 4：运行注册表测试和现有 Adapter 测试**。
- [ ] **步骤 5：提交** `refactor: add capability-oriented source boundary`。

### 任务 3：实现公开小宇宙音频来源

**文件：**

- 新建：`app/ingestion/adapters/xiaoyuzhou.py`
- 修改：`app/ingestion/sources.py`
- 测试：`tests/test_xiaoyuzhou_ingestion.py`
- 新增 fixture：`tests/fixtures/xiaoyuzhou_episode.html`

- [ ] **步骤 1：先写 fixture 测试**，使用用户提供的单集页面结构，提取标题、作者、时长和 HTTPS `.m4a` 地址，并返回 `resource_kind=audio`、`metadata`、`audio`、`transcription` 能力。
- [ ] **步骤 2：运行** `pytest -q tests/test_xiaoyuzhou_ingestion.py`，确认来源模块尚不存在。
- [ ] **步骤 3：实现**公开 HTML/内嵌状态解析、HTTPS 音频主机白名单、带大小限制的流式下载，以及返回 `None` 的 `fetch_caption()`；没有公开字幕接口时交给 Whisper。
- [ ] **步骤 4：运行 fixture 测试，并对 `https://www.xiaoyuzhoufm.com/episode/6a5f441fa3fec224d5a10e23` 做只读探测；不要持久化下载音频。
- [ ] **步骤 5：运行 `git diff --check`，提交** `feat: support Xiaoyuzhou audio sources`。

### 任务 4：增加能力规划器并迁移现有媒体管线

**文件：**

- 新建：`app/ingestion/planner.py`
- 修改：`app/ingestion/pipeline.py`
- 修改：`app/ingestion/service.py`
- 测试：`tests/test_ingestion_planner.py`
- 测试：`tests/test_ingestion_pipeline.py`

- [ ] **步骤 1：先写失败测试**，覆盖纯音频执行、字幕优先、仅在声明支持且开启时抽取关键帧，以及不支持能力时返回清晰错误。
- [ ] **步骤 2：运行聚焦测试**，确认当前视频专用协议无法满足这些场景。
- [ ] **步骤 3：实现**规划器：声明字幕能力时先尝试字幕；需要转写时获取音频；只有明确开启且来源支持时才获取视频/关键帧。保留临时目录清理和 `MediaExtractionError` 回退行为。
- [ ] **步骤 4：运行规划器、媒体管线和媒体出口测试**。
- [ ] **步骤 5：提交** `refactor: plan ingestion from source capabilities`。

### 任务 5：将 API 和管理后台切换到来源解析与能力计划

**文件：**

- 修改：`app/main.py`
- 修改：`app/admin_routes.py`
- 修改：`app/ingestion/classifier.py`
- 修改：`app/schemas.py`
- 测试：`tests/test_ingestion_api.py`
- 测试：`tests/test_admin.py`

- [ ] **步骤 1：先写失败测试**，确认用户提供的小宇宙 URL 得到 `source_platform=xiaoyuzhou`、`resource_kind=audio`，排队时不标记为 `video`；同时保留 Bilibili 和普通小红书图文回归测试。
- [ ] **步骤 2：运行 API/管理后台聚焦测试**，确认当前分类无法表达小宇宙音频。
- [ ] **步骤 3：实现**来源解析和探测，再生成能力计划。只在兼容边界填充旧 `media_type`，在现有 schema 中以增量方式暴露 `resource_kind`。
- [ ] **步骤 4：运行全部 API/管理后台测试，确认后台执行器收到正确的来源计划**。
- [ ] **步骤 5：提交** `refactor: dispatch ingestion through source plans`。

### 任务 6：在 Python 3.12 和 claw 上验证，并更新交接文档

**文件：**

- 修改：`HANDOFF.md`
- 修改：`docs/HANDOFF.md`
- 测试：全部现有测试

- [ ] **步骤 1：运行** `python3 -m compileall -q app tests` 和 `git diff --check`。
- [ ] **步骤 2：在 claw 的 Python 3.12 容器中运行** `pip install -q pytest && pytest -q`。
- [ ] **步骤 3：执行只读小宇宙探测；若音频体积可控，再做小型公开音频 smoke test，并记录未使用 Cookie 或浏览器自动化**。
- [ ] **步骤 4：更新两个 handoff 入口，记录来源/能力模型、小宇宙路径和准确验证结果**。
- [ ] **步骤 5：提交** `docs: update capability ingestion handoff`；只有用户明确要求部署时才推送或部署。
