# TripGuard 管理后台迁移说明（交给实现 Agent）

> 目的：把当前 TripGuard 管理后台迁移/重写到另一处前端或应用中，同时保持信息架构、视觉语言、权限边界和后端行为一致。本文是实现规格，不要求继续使用 Jinja2；可以使用 React、Vue、Next.js 或其他技术栈。

## 0. 交付范围与原则

这是一个仅供内部管理员使用的“旅行资料采集控制台”，不是面向普通用户的产品前台。它有且只有三类核心页面：登录、解析任务、解析结果；另有任务详情与资料详情。

必须保持的产品边界：

- “解析任务”记录每一次提交，包含失败、处理中和未入库的成功任务。
- “解析结果”只展示已持久化为 `TravelSource`、可作为后续推荐依据的旅行资料；绝不能把仅完成处理但未入库的任务展示为结果。
- 列表的主信息是可读标题，而不是裸 URL；原始 URL 只应在详情页作为“打开原始链接”。
- 视频、图片、网页的原始文件、临时音频和关键帧不在后台展示或长期保存；展示的是标题、摘要、封面 URL、结构化旅行资料和文本证据。
- 不在前端保存或打印管理员密码、密码散列、会话密钥、Cookie、平台代理凭据或媒体下载凭据。

## 1. 推荐迁移方式

### 方案 A：独立前端 + 同域反向代理（推荐）

新前端部署在任意技术栈中，但通过同一 HTTPS 域名反代 TripGuard 后端：

- UI 路径：由新前端提供，例如 `/admin`。
- 后端管理 API：同域代理到 TripGuard，例如 `/admin-api/*` 或保留 `/admin/*`。
- 登录后的 HttpOnly 会话 Cookie 仍由后端签发，浏览器同域自动携带。
- 所有后端访问必须走 HTTPS；不允许把管理 Cookie 暴露给前端 JavaScript。

当前后端路由为 HTML 表单/片段路由，不适合跨域 SPA 直接消费。迁移时请在后端增加一个受同一鉴权保护的 JSON/BFF 层，或由新前端的 server-side BFF 转换它；不要从浏览器抓取或解析现有 HTML。

### 方案 B：继续由 FastAPI/Jinja2 服务

如果只是搬到另一台服务或另一套部署环境，可以直接迁移 `app/admin_routes.py`、`app/templates/admin/`、`app/static/admin.css`、`app/static/platform-icons/`，保持现有 HTML 路由。工作量最小，但不适合独立前端团队长期迭代。

### 不要采用

- 跨域前端直接调用现有表单 HTML 路由。
- 通过 localStorage 保存管理员用户名密码或会话 Token。
- 用原始 URL 作为任务卡片的大标题。
- 为“获取封面”放开任意 URL 代理；封面代理必须保留 SSRF 防护、跳转上限、图片 MIME 校验和 5 MiB 上限。

## 2. 现有页面地图

| 页面 | 当前路径 | 作用 | 关键行为 |
| --- | --- | --- | --- |
| 登录 | `GET /admin/login` | 管理员身份验证 | 失败只显示通用错误；成功回到任务页 |
| 登录提交 | `POST /admin/login` | 建立会话 | 参数 `username`、`password`；成功 303 到 `/admin` |
| 退出 | `POST /admin/logout` | 清空会话 | 回到 `/admin/login` |
| 解析任务 | `GET /admin` | 提交输入、查看最近 20 个任务 | 任务点击进入详情 |
| 提交链接 | `POST /admin/ingestions/url` | 支持纯 URL 或分享文案中的首个 HTTP URL | 创建任务并 303 到详情 |
| 提交图片 | `POST /admin/ingestions/image` | 上传单张图片 | `multipart/form-data` 的 `file` 字段，创建异步任务 |
| 任务详情 | `GET /admin/ingestions/{job_id}` | 查看动态进度、失败与审核 | 每 2 秒刷新任务片段，终态停止 |
| 任务状态片段 | `GET /admin/ingestions/{job_id}/fragment` | 任务详情局部刷新数据 | 新实现应替换为 JSON 轮询接口 |
| 审核 | `POST /admin/ingestions/{job_id}/review` | `review` 任务人工入库或拒绝 | `decision=accept|reject`，可附 `reason` |
| 解析结果 | `GET /admin/sources?source_id=...` | 左侧资料列表 + 右侧选中资料卡片 | 默认选最新一条，最多 60 条 |
| 资料详情 | `GET /admin/sources/{source_id}` | 独立展示一条旅行资料 | 可选保留 |
| 封面代理 | `GET /admin/sources/{source_id}/cover` | 安全代理非 YouTube 封面 | 需要管理员会话 |

未登录访问任何后台资源时：`/admin` 应重定向至登录页；API 版则应返回明确的 `401`。未配置管理鉴权时，登录页应返回 `503`，而不是以空密码运行。

## 3. 必需的数据契约

迁移前端时，后端必须至少向它提供下面的视图数据。字段来自现有 SQLModel，不要改写其语义。

### 3.1 任务 `IngestionJob`

```ts
type IngestionJob = {
  job_id: string;
  input_type: "url" | "image";
  original_url: string | null;       // 仅详情使用，列表不可作为主标题
  canonical_url: string | null;
  source_platform: "youtube" | "bilibili" | "douyin" | "xiaohongshu" | "xiaoyuzhou" | "image" | string | null;
  media_type: "video" | "audio" | "article" | "image" | string;
  status: "queued" | "running" | "succeeded" | "failed";
  stage: string;
  progress_percent: number;          // 0–100
  progress_message: string | null;
  progress_updated_at: string | null;
  created_at: string;
  failure_stage: "caption" | "metadata" | "audio" | "video" | "keyframe" | string | null;
  error_code: string | null;
  error_message: string | null;
  media_egress: string | null;
  source_id: string | null;
  ingest_decision: "pending" | "accept" | "review" | "reject";
  analysis_json: Record<string, unknown>;       // LLM 结果，可能含 title/reason
  evidence_metadata_json: Record<string, unknown>; // 媒体元数据，可能含 title
  evidence_text: string | null;
  evidence_origin: string | null;
};
```

任务列表需要由后端或 BFF 预先提供以下展示字段；不要把这套标题判定分散在各客户端：

```ts
type TaskListItem = IngestionJob & {
  display_title: string;
  display_metadata: string; // “平台 · 介质 · MM-DD HH:mm”
  display_status: "等待中" | "处理中" | "已完成" | "失败";
  failure_summary: string | null;
};
```

`display_title` 的优先级是强制规则：

1. `analysis_json.title`：LLM 生成的最终旅行资料标题；
2. `evidence_metadata_json.title`：已抓取的媒体标题；
3. 关联 `TravelSource.title`；
4. 图片任务显示“图片资料”；链接任务显示“{平台名} · {短标识}”。短标识优先取查询参数 `v`，否则取 URL path 最后一段，最多 14 个字符并以 `…` 截断。

这保证媒体元数据已经取得、但后续下载或转写失败时，任务仍然有可读标题。

状态映射：`queued → 等待中`、`running → 处理中`、`succeeded → 已完成`、`failed → 失败`。

失败摘要只在 `failed` 时显示：`字幕获取失败 / 元数据获取失败 / 媒体处理失败 / 视频处理失败 / 关键帧处理失败 / 解析失败` + `·` + 单行短错误。若错误含 `Fresh cookies` 或 `cookies are needed`，统一显示“平台拒绝当前请求”，不要向 UI 泄露上游底层错误。其他错误去掉换行并截断到 72 字符。

### 3.2 已入库旅行资料 `TravelSource`

```ts
type TravelSource = {
  source_id: string;
  title: string;
  body_text: string;
  summary_text: string | null;
  original_url: string | null;
  source_platform: string | null;
  cover_image_url: string | null;
  destination: string;
  category: string;
  location_name: string | null;
  normalized_tags: string[];
  raw_tags: string[];
  created_at: string;
};

type SourceEvidence = {
  source_id: string;
  origin: string;
  language: string | null;
  full_text: string;
  segments: Array<Record<string, unknown>>;
  metadata_json: Record<string, unknown>;
};
```

“解析结果”列表只取 `TravelSource`，按 `created_at` 倒序，不展示 `IngestionJob`。正文显示 `summary_text ?? body_text`；标签取 `(normalized_tags + raw_tags).slice(0, 6)`。

### 3.3 建议新增的 JSON API

为独立前端增加以下受会话鉴权保护的接口（示例路径可调整，但语义和状态码必须一致）：

```text
POST /admin-api/login                         -> 204 + Set-Cookie；失败 401
POST /admin-api/logout                        -> 204
GET  /admin-api/session                       -> { authenticated: true, username }
GET  /admin-api/tasks?limit=20                -> { items: TaskListItem[] }
POST /admin-api/tasks/url                     -> 202 { job_id, status }
POST /admin-api/tasks/image                   -> 202 { job_id, status }
GET  /admin-api/tasks/{job_id}                -> IngestionJob + display 字段
POST /admin-api/tasks/{job_id}/review         -> 200/204
GET  /admin-api/sources?limit=60              -> { items: TravelSource[] }
GET  /admin-api/sources/{source_id}           -> { source, evidence, cover_url }
GET  /admin-api/sources/{source_id}/cover     -> 图片二进制（仅在需要代理时）
```

任务创建保持异步：先持久化 `IngestionJob`，再交给后台 worker，前端拿到 `job_id` 后跳转详情。不要等待视频下载、转写或 LLM 分析结束。

## 4. 信息架构与交互规格

### 4.1 全局壳层

登录后所有页面使用同一主壳层：

- 居中最大宽度 `1240px`、淡暖白内容面、浅灰米色页面背景。
- 内容壳层圆角 `24px`，细边框和大而低对比度的阴影；桌面上下外边距 `28px`。
- 顶栏三列：左侧 TripGuard 品牌、中间两个 tab、右侧“退出登录”。
- tab 仅有“解析任务”和“解析结果”；激活项为暖白底、小阴影，非激活项为灰色文字。
- 所有界面中文，辅助 eyebrow 为英文大写小字：`INGESTION WORKSPACE`、`ACTIVITY`、`TRAVEL SOURCE LIBRARY`、`INGESTION RECORD`。

### 4.2 登录页

桌面端左右双栏，右栏约 `470px`：

- 左侧是柔和的浅绿到浅杏渐变场景，两个倾斜的“旅行灵感卡片”作为抽象装饰，主文案为“收集灵感 / 整理旅途”。不要使用外链图片或造成性能负担的大图。
- 右侧为品牌、`INGESTION CONSOLE`、标题“回到你的 / 旅行资料库”、简短说明、账号与密码字段、全宽“进入后台 →”按钮。
- 登录失败显示一条暖粉底的通用错误：“账号或密码错误，请重试。”
- 移动端隐藏两张装饰卡片，保留简化的顶部装饰区域和表单。

### 4.3 解析任务页

从上到下：

1. **Hero**：标题“解析任务”、解释“记录每一次提交，清楚知道它正在处理什么、处理到了哪一步。”，右侧有轻描边按钮“查看解析结果 →”。
2. **提交卡片**：浅米底、圆角 `16px`，桌面两列。
   - 链接输入：标签“解析链接或分享文案”；单行输入，占满可用宽度；placeholder 为“粘贴小红书分享文案、网页或视频链接”；右侧按钮“开始解析”。按钮必须 `white-space: nowrap`，窄宽度不能被挤成两行。
   - 图片输入：标签“解析图片”；文件选择控件；浅色次级按钮“上传图片”。
3. **最近任务**：标题区含 eyebrow `ACTIVITY`、标题“最近任务”和右侧任务数量。默认最多 20 条、最新在前。

每条任务是一整行可点击卡片（点击进入任务详情），三列：

```text
[40×40 平台图标]  [标题 / 细小元信息 / 可选失败摘要]          [状态点 + 状态文字]
```

- 行高紧凑，内边距 `14px 26px`，行间以细分隔线区分；hover 变为浅米底。
- 标题单行省略号；元信息与失败摘要各一行、省略号。
- 图片上传显示“图”字的浅绿色方形图标；已知平台展示真实本地图标；未知链接显示“链”。
- 支持：YouTube、Bilibili、抖音、小红书、小宇宙、图片。图标文件位于 `app/static/platform-icons/`，不要以 Unicode Emoji 代替。
- 状态色：成功绿 `#4f9569`；失败红 `#c3574a`；处理中/等待中金棕 `#b7802e`。状态前为 6px 圆点。
- 空状态：“还没有解析任务。粘贴链接、分享文案或上传图片开始。”

### 4.4 任务详情页

- 顶部返回链接“← 返回解析任务”、eyebrow `INGESTION RECORD`、标题“任务详情”、任务 ID 的小号灰字。
- 任务状态卡显示：状态、`当前阶段：stage · media_type · platform`；未终态时显示 `progress_percent% · progress_message`、进度条、最后更新时间。
- 若有原始 URL，在此处而非任务列表展示“打开原始链接 ↗”。
- 失败时显示 `error_code：error_message` 的浅粉警告块，并保留 `failure_stage`、`media_egress`（若有）。
- 已生成资料时显示绿色可点击的“已生成解析结果”卡，包含资料标题和“查看资料 →”。
- 任务成功但不适合入库时显示中性提示“任务已完成，但未生成旅行资料”及 LLM 的 `analysis_json.reason`。
- `ingest_decision === review` 时显示黄色审核块，提供“确认入库”和“拒绝入库”按钮；不得在普通成功/失败任务中出现。
- 有 `SourceEvidence` 时以可折叠的 `<details>` 显示“查看解析证据（origin）”；无资料但有 `evidence_text` 时显示“查看提取文本”。证据换行保留、允许长词折行。
- 页面每 **2 秒**刷新一次任务数据；`status` 为 `succeeded` 或 `failed` 后停止轮询。轮询失败时保留上一帧内容并在页面顶部显示可恢复的轻提示，不应清空详情。

### 4.5 解析结果页

该页面只面向已保存的 `TravelSource`，采用“左列表、右详情”工作台：

- 左栏固定约 `350px`，浅暖底；含“已入库资料”和数量。每项为 `38×38` 封面缩略图、单行标题、`destination · category`。选中项底色更深。
- 右栏为选中资料的大卡片：封面、平台与分类 badge、标题、摘要、三个事实项（目的地 / 分类 / 地点）、最多六个标签、原始链接。
- 无封面时使用现有的绿色—黄绿色—沙色渐变占位，不显示破损图片图标。
- 图片默认横向顶部大图；加载后若 `naturalHeight > naturalWidth`，切换为左侧约 34% 宽的竖图布局。移动端始终纵向。
- YouTube 官方缩略图域名 `i.ytimg.com` 或 `i3.ytimg.com` 使用原 URL；其他平台封面使用受鉴权的后端 cover proxy。新系统必须同样避免前端任意 URL 请求代理。
- 空状态：“还没有可展示的解析结果。完成并入库的旅行内容会出现在这里。”

## 5. 视觉 Token（必须接近）

这套后台不是传统黑灰 SaaS 面板。风格是“克制、温暖、旅行资料工作台”：暖白、大圆角、细线、低饱和自然色，信息密度中等但不拥挤。

```css
font-family: Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
--page-bg: #f3f1ea;
--surface: #fffdf8;
--ink: #20211e;
--line: #e7e2d8;
--subtle-line: #ebe6dd;
--muted: #77756e;
--accent: #ea653d;
--accent-text: #b9563a;
--green: #4f9569;
--error: #c3574a;
--pending: #b7802e;
--soft-green: #edf3ea;
--soft-error: #fff0ed;
--soft-warm: #f7f4ed;
```

品牌标记是 `28×28px` 的珊瑚色圆角方块，内嵌浅橙色 `7px` inset，不需要文字 logo 图片。标题字距偏紧（约 `-0.035em` 到 `-0.065em`）；正文 13–14px，辅助信息 10–12px。

响应式断点：`820px`。小屏时取消外部边距和壳层圆角；顶栏允许导航折到第二行；提交卡片改单列；结果工作台改上下布局；资料卡改单列；登录页改上下布局。

## 6. 认证与安全要求

- 使用单独后台账号。后端保存 Argon2 密码散列；用户名校验使用恒定时间比较。
- 会话 Cookie 应为签名、`HttpOnly`、`Secure`（生产 HTTPS）和 `SameSite=Lax`。前端代码不可读取它。
- 所有 UI/API 路由均要鉴权，包括任务片段、审核操作和封面代理。
- 登录错误不得区分“用户名不存在”和“密码错误”。
- URL 输入允许“分享文案 + URL”，后端应提取第一个 HTTP(S) URL；前端不能假定用户只粘贴纯链接。
- 图片上传只接受 `image/*`，受服务端大小限制；保存到任务私有临时目录，由后台任务最终清理。
- 所有外部封面代理保留公共地址校验，最多 6 次跳转，只接受 `image/*`，最大 5 MiB；禁止内网地址/元数据地址 SSRF。

## 7. 可执行的验收清单

实现 Agent 完成后，至少验证以下行为：

1. 未登录访问任务页会跳转登录；未配置后台认证会得到 503。
2. 错密码只出现通用错误；正确密码建立会话并进入任务页；退出后不能再访问后台数据。
3. 任务页可提交 YouTube URL、带小红书短链接的分享文案和图片；提交后立即进入具体任务详情，不等待后台解析。
4. 任务列表只显示最近 20 条。失败的抖音任务在已拿到媒体标题时显示该标题，**不显示**短链接；失败摘要显示“媒体处理失败”。
5. 任务列表优先级符合“LLM 标题 → 媒体标题 → 已入库资料标题 → 平台短标识”；原始 URL 在列表 HTML/DOM 中也不作为可见标题。
6. YouTube、Bilibili、抖音、小红书、小宇宙任务显示对应本地图标；图片任务显示图片区分；未知来源显示“链”。
7. 任务详情每两秒刷新；完成或失败后停止。`review` 任务可接受或拒绝，接受后生成资料并可从结果页看到。
8. 解析结果只显示 `TravelSource`。有封面、无封面、横图、竖图、YouTube 官方封面、其他平台代理封面均正确显示。
9. 820px 以下无横向溢出：链接输入和“开始解析”按钮仍保持一行，结果页变为上下布局。
10. 不向浏览器返回密码散列、会话密钥、平台 Cookie、临时媒体路径或未受保护的外部代理能力。

## 8. 当前实现的源码参照（只用于核对，不要求照搬）

```text
app/admin_routes.py                 # 鉴权、路由、展示字段组装、安全封面代理
app/templates/admin/login.html      # 登录页结构
app/templates/admin/dashboard.html  # 任务页结构
app/templates/admin/job.html        # 任务详情与 2 秒轮询
app/templates/admin/job_fragment.html# 任务状态、审核、证据
app/templates/admin/sources.html    # 解析结果工作台
app/templates/admin/source_detail.html
app/static/admin.css                # 全部视觉 token 与响应式规则
app/static/platform-icons/          # 平台 SVG 与来源说明
app/models.py                       # IngestionJob / TravelSource / SourceEvidence 数据定义
tests/test_admin.py                 # 可迁移的行为回归用例
```

## 9. 给实现 Agent 的直接任务提示

将以下段落原样附在任务中即可：

> 请根据 `docs/admin-console-migration-brief.md` 重建 TripGuard 管理后台。优先实现独立前端 + 同域受会话保护的 JSON/BFF API；不得从 HTML 抓取数据，也不得改变 IngestionJob、TravelSource、SourceEvidence 的持久化语义。严格实现任务与解析结果分离、标题优先规则、任务详情 2 秒轮询、审核流、封面安全代理和 820px 响应式布局。视觉以该文档的暖白旅行资料工作台为准，而非通用后台模板。先补齐自动化测试，再实现；完成后运行全量测试并提供桌面与移动端截图。
