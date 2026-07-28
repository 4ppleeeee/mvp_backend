# TripGuard 管理后台设计

## 目标

部署一个受单管理员账号保护的 Web 管理后台。管理员可粘贴链接、上传图片、查看异步任务的阶段/进度/失败原因，并阅读处理后保存的旅行资料和文本证据。

## 技术选择

管理后台由现有 FastAPI 服务直接提供：Jinja2 模板、静态 CSS 和少量浏览器端 JavaScript 轮询。它不引入独立前端构建、额外数据库或额外容器，所有任务均调用既有 Ingestion API 和 SQLite 数据。

## 访问控制

- `/admin/login` 展示登录表单，成功后写入签名、`HttpOnly`、`SameSite=Lax` 的会话 Cookie。
- 所有 `/admin` 路由都要求已登录会话；未认证用户重定向到登录页。
- 管理员用户名、Argon2 密码散列和会话密钥只通过 `TRIPGUARD_ADMIN_USERNAME`、`TRIPGUARD_ADMIN_PASSWORD_HASH`、`TRIPGUARD_ADMIN_SESSION_SECRET` 环境变量配置。
- 仓库、Docker Compose、日志、API 响应和对话中均不保存或打印密码、散列原文或会话密钥。
- 服务首次部署若缺少任一认证配置，管理页面返回明确的 503 配置错误，而不是降级为未鉴权访问。

## 页面与数据流

### 登录页

账号密码表单提交到 `POST /admin/login`。失败显示通用“账号或密码错误”，不泄露账号是否存在。登出由 `POST /admin/logout` 清除会话。

### 控制台首页

- URL 表单提交 `POST /admin/ingestions/url`，服务调用与 App 相同的 `POST /ingestions` 逻辑，重定向至任务详情页。
- 图片表单提交 `POST /admin/ingestions/image`，服务流式写入任务私有临时目录，创建 `input_type=image` 的 `IngestionJob`，提交后台 worker。
- 最近任务表展示原始链接/文件名、平台/介质、状态、阶段、开始时间和失败摘要。

### 任务详情页

页面初始渲染任务状态，浏览器每两秒请求 `GET /admin/ingestions/{job_id}/fragment`。片段展示：

```text
queued -> classifying -> extracting -> transcribing -> analyzing -> saving -> succeeded | failed
```

成功时显示 `TravelSource` 的标题、正文、目的地、分类、地点、标签、原始链接，以及 `SourceEvidence` 的来源、语言、全文和时间戳片段。失败时展示安全失败码与用户可理解的消息。任务为终态时停止轮询。

## 图片异步链路

图片上传不再使用现有 Base64 同步接口。新 `ImagePipeline` 通过既有 `OllamaLlmClient.analyze_image` 在 worker 中分析图像；仅把图片放在 `job_id` 临时目录。分析完成后将 `TravelSource` 和 `SourceEvidence(origin=ocr)` 写入 SQLite，之后无论成功、失败、超时或取消都删除图片。

`POST /ingestions/image` 也向 App/API 开放，管理后台仅是该接口的 HTML 表单消费者。上传限制由 `TRIPGUARD_INGESTION_MAX_UPLOAD_BYTES` 配置。

## 路由

```text
GET  /admin/login
POST /admin/login
POST /admin/logout
GET  /admin
POST /admin/ingestions/url
POST /admin/ingestions/image
GET  /admin/ingestions/{job_id}
GET  /admin/ingestions/{job_id}/fragment
```

API 新增：

```text
POST /ingestions/image
```

## 验证与部署

- 单元/API 测试覆盖：未登录重定向、正确/错误密码、URL 提交、图片上传大小/媒体类型校验、状态片段、成功结果展示。
- 以假的执行器、Ollama 和图片内容验证状态迁移，不在 CI 调用真实 Ollama 或平台媒体。
- Docker 镜像安装 Jinja2、Argon2 运行时和现有媒体依赖。
- 部署到 claw 前，通过其环境变量注入管理员账号、密码散列和会话密钥；不提交 `.env`。
- 部署后验证登录、模拟 URL/图片任务、终态详情页和服务健康检查。
