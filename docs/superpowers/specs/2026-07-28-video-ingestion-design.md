# TripGuard 异步 Ingestion 与视频证据管线设计

## 目标

在 TripGuard 后端新增统一异步 Ingestion Service。客户端提交链接、图片或本地视频上传后立即获得任务 ID；后台负责判定介质、抽取内容、调用 Ollama 做旅行结构化分析，并将可长期推荐/检索的文本证据入库。

本设计的首期视频来源与 BiliNote 主流程实际注册的平台保持一致：B 站、YouTube、抖音/TikTok、快手和本地视频上传。`xiaoyuzhoufm_download.py` 未注册到 BiliNote 主流程，因此不属于此兼容范围。

## 约束与非目标

- 原始链接和规范化链接必须长期保存。
- 不长期保存视频、音频、原图、网页 HTML/快照或关键帧。任务的临时二进制材料必须在成功、失败、超时和取消时清理。
- 长期保存派生文本证据：网页正文、OCR 文本、视频字幕/ASR 及带时间戳片段、平台元数据和文字化的结构化事实。
- 只处理公开可访问或用户已合法授权的资源。登录要求、人机验证、地区/访问限制等不能由服务自动绕过。
- 本期不迁入 BiliNote 的笔记生成、GPT/RAG 产品层、前端/桌面端、截图/关键帧功能、本地文件状态体系或 Cookie 管理。
- 本期不改造 App；原有同步 `/sources/*` API 继续兼容。后续 App 迁移到新异步 API。

## 采用方案

将 BiliNote 的媒体处理经验抽成 TripGuard 自己的兼容内核，而非整体复制其下载器或部署为独立服务。

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 整段复制 BiliNote 下载器 | 不采用 | 与其配置、缓存、Cookie、笔记产品流程高度耦合。 |
| 提取兼容内核并适配接口 | 采用 | 平台差异局部化，TripGuard 保留自己的任务、Ollama 和存储边界。 |
| 以 BiliNote 作为独立内部服务 | 不采用 | 会产生两套 API、状态和存储，长期维护成本高。 |

若将 BiliNote 的 MIT 许可代码复制或实质性改写进项目，必须保留原版权与 MIT 许可文本，并在第三方声明中标注来源。

## 总体架构

```text
POST /ingestions
      |
      v
IngestionJob (queued)
      |
      v
ResourceClassifier -> ResourceDescriptor
      |
      +-- ARTICLE / IMAGE / DOCUMENT -> 对应文本提取管线
      |
      +-- VIDEO -> VideoPipeline
                       |
                       v
                Platform VideoAdapter
                       |
            公开字幕优先 -> 临时音频 -> faster-whisper
                       |
                       v
              EvidenceBundle / SourceEvidence
                       |
                       v
                    Ollama 分析
                       |
                       v
          TravelSource + SourceEvidence (succeeded)
```

### 职责边界

| 组件 | 职责 |
| --- | --- |
| `IngestionService` | 创建任务、调度后台执行、维护状态、重试、收尾清理。 |
| `ResourceClassifier` | 解析/展开 URL，给出规范化 URL、平台和 `ARTICLE`、`IMAGE`、`VIDEO`、`DOCUMENT`、`UNKNOWN` 等介质分类。 |
| `VideoPipeline` | 统一视频元数据、字幕优先策略、ASR 降级、证据归一化和任务目录生命周期。 |
| `VideoAdapter` | 仅处理特定平台的链接匹配、规范化、元数据、公开字幕与临时音频取得。 |
| `MediaAcquirer` | 集中封装 `yt-dlp` 与 FFmpeg 调用，不由每个平台重复实现。 |
| `Transcriber` | 将临时音频转成带时间戳的文本；初期实现为 `faster-whisper`。 |
| 现有 `OllamaLlmClient` | 对证据包产出旅行相关性、目的地、分类、地点和标签。 |
| Repository | 保存 `TravelSource`、`SourceEvidence` 与任务状态。 |

## 数据模型

### `IngestionJob`

负责可查询、可恢复的工作状态，而不保存二进制材料。

| 字段 | 含义 |
| --- | --- |
| `job_id` | 对外暴露的任务 ID。 |
| `input_type` | `url`、`image`、`video_upload` 或 `text`。 |
| `original_url` / `canonical_url` | 用户提交链接和规范化后的链接。 |
| `source_platform` / `media_type` | 分类结果。 |
| `status` / `stage` | 任务总状态和当前阶段。 |
| `attempt_count` / `max_attempts` | 有限重试控制。 |
| `error_code` / `error_message` | 可安全展示、可行动的失败原因；不保存敏感上游响应。 |
| `source_id` | 成功后关联到 `TravelSource`。 |
| `created_at` / `started_at` / `finished_at` | 生命周期审计时间。 |

阶段顺序固定为：`queued -> classifying -> extracting -> transcribing -> analyzing -> saving -> succeeded | failed`。无须转写的任务可从 `extracting` 直接进入 `analyzing`。

### `TravelSource`

保留现有“收藏卡片与推荐聚合”职责：标题、清洗后的摘要正文、原始链接、平台、目的地、分类、地点和标签。它不承载整段字幕，避免卡片查询和推荐上下文无界膨胀。

### `SourceEvidence`

一条 `TravelSource` 可有多条证据。首期字段包括：

- `evidence_id`、`source_id`、`kind`、`origin`、`language`；
- `full_text`；
- `segments` JSON（每段 `start_seconds`、`end_seconds`、`text`）；
- `metadata` JSON（标题、作者/频道、发布时间、时长、公开封面 URL 等非二进制元数据）；
- `created_at`。

`origin` 的枚举为 `platform_caption`、`auto_caption`、`asr`、`ocr`、`article`。后续 RAG 的切块从 `SourceEvidence` 生成，不改变 `TravelSource` 的卡片语义。

## API

### 新接口

```text
POST /ingestions
GET  /ingestions/{job_id}
```

`POST /ingestions` 接收 JSON URL/文本输入或 multipart 图片/视频上传。它只创建 `IngestionJob`，返回 `202 Accepted`：

```json
{
  "job_id": "ing_xxx",
  "status": "queued"
}
```

`GET /ingestions/{job_id}` 返回阶段、尝试次数、失败码和成功后的 `source_id`。客户端轮询该接口；本期不引入 WebSocket 或回调。

现有 `/sources/collect`、`/sources/analyze`、`/sources/collect-image` 和 `/sources/analyze-image` 保持不变。待 App 迁移后再逐步收敛到 Ingestion API。

本地视频只能通过 multipart 上传进入任务目录；服务不得接受客户端指定的服务器绝对路径。

## 视频 Adapter 合约与首期策略

```text
VideoAdapter
  matches(url) -> bool
  normalize(url) -> canonical_url
  fetch_metadata(url) -> MediaMetadata
  fetch_caption(url) -> Transcript | None
  acquire_audio(url, job_dir) -> TemporaryAudio
```

| 来源 | 字幕优先路径 | ASR 降级 |
| --- | --- | --- |
| YouTube | 公开人工字幕，后自动字幕 | `yt-dlp` 临时音频 -> faster-whisper。 |
| B 站 | 公开播放器字幕 | `yt-dlp` 临时音频 -> faster-whisper。 |
| 抖音/TikTok | 可公开获得的元数据/字幕 | 可公开获得的临时音频 -> faster-whisper。 |
| 快手 | 可公开获得的元数据/字幕 | 可公开获得的临时音频 -> faster-whisper。 |
| 本地视频 | 文件元数据 | FFmpeg 提取临时音频 -> faster-whisper。 |

字幕结果统一为完整文本、语言、时间戳片段和来源类型。若字幕取得成功，不下载音视频；仅获取必要元数据。若没有字幕，才进入临时音频和 ASR 路径。

## 失败、重试与清理

| 条件 | 结果 |
| --- | --- |
| 网络超时、限流、上游 5xx | 有限次数指数退避重试。 |
| 无字幕 | 正常降级到 ASR。 |
| 公开媒体无法取得 | `media_unavailable` 失败。 |
| 平台下架、地区限制 | `content_unavailable` 或 `region_restricted` 失败。 |
| 登录、人机验证或访问控制 | `auth_required`、`human_verification_required` 或 `access_restricted` 失败；不做自动规避。 |
| `yt-dlp` 提取器过期 | `extractor_outdated` 失败；升级依赖后由用户创建新任务。 |
| OCR/ASR/LLM 无法完成 | 精确阶段失败码，保留任务和已写入的安全文本诊断。 |

每次任务在独立的 `job_id` 临时目录执行。清理必须位于最外层 `finally`，覆盖正常完成、异常、超时和取消；数据库中不保存临时目录路径。

## 依赖与部署边界

首期新增运行依赖为 `yt-dlp`、`faster-whisper` 和系统级 FFmpeg。Whisper 模型缓存是运行时模型资产，而不是用户视频材料，可由部署配置选择 CPU/GPU 和模型规格。

后端保持为 API 进程与后台 worker 的可分离架构：本地 MVP 可先运行同进程后台执行器，部署到 `claw` 时将同一任务处理器迁移到独立 worker，不改变 API 和持久化契约。本设计不授权或要求本阶段部署到 `claw`。

## 验证策略

- 单元测试：URL 分类、状态迁移、字幕优先、ASR 降级、失败码、`finally` 清理、证据入库。
- Adapter 测试：使用不含媒体二进制的固定元数据/字幕 fixture 和假的 `MediaAcquirer`/`Transcriber`。
- API 测试：任务创建、轮询、成功后的 `source_id`、失败任务展示。
- 手工 smoke test：使用合法公开链接分别验证五类来源；公网平台不进入 CI，避免易碎网络依赖与意外下载。

## 实施顺序

1. 数据模型、repository、异步任务 API 和状态机。
2. 统一 `VideoPipeline`、临时目录管理、`MediaAcquirer` 和 `faster-whisper` Adapter。
3. YouTube、B 站、抖音/TikTok、快手和本地上传 Adapter。
4. 将证据接入现有 Ollama 分析与 `TravelSource` 保存。
5. 完整测试与公开链接 smoke test；随后另行规划 App 迁移和 `claw` 部署。
