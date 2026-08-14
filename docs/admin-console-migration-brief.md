# TripGuard 管理后台边界说明

TripGuard 后端不再提供 `/admin` 的 Jinja 页面、登录表单、会话 Cookie、
`/static` 静态目录或管理员密码配置。由独立的管理前端负责 `/admin` 浏览器
路由与展示；后端只在明确启用时提供 JSON BFF。

## 当前后端契约

在受控环境设置 `TRIPGUARD_ADMIN_API_ENABLED=true` 后，管理前端可使用
`/admin-api/*`：

- 任务：`GET /tasks`、`POST /tasks/url`、`POST /tasks/image`、
  `GET /tasks/{job_id}`、`POST /tasks/{job_id}/review`。
- 资料：`GET /sources`、`GET /sources/{source_id}`、
  `GET /sources/{source_id}/cover`。
- POI：`POST /poi/location/suggest`、`POST /poi/crawls`、
  `GET /poi/crawls`、`GET /poi/crawls/{crawl_task_id}`、
  `GET /poi/tasks/{native_task_id}/pages`、
  `POST /poi/tasks/{native_task_id}/search`。

任务列表继续由后端计算标题优先级和中文状态；审核仍通过
`IngestionService` 持久化 `IngestionJob`、`TravelSource` 与证据，不改变既有
客户端或采集 API 的语义。

## 访问与部署边界

`TRIPGUARD_ADMIN_ALLOWED_ORIGINS` 仅控制浏览器 CORS。所有生产
`/admin-api/*` 请求都必须由 FUE 网关认证保护，不能依赖 CORS 充当认证。
以下配置只能留在服务端，绝不能进入前端构建产物、请求、日志或响应：

- `TRIPGUARD_CRAWLAB_RESULTS_API_URL`
- `TRIPGUARD_CRAWLAB_API_TOKEN`
- `TRIPGUARD_TENCENT_LOCATION_API_KEY`
- `TRIPGUARD_TENCENT_LOCATION_BASE_URL`

独立前端可用 `VITE_ADMIN_API_BASE_URL` 指定 BFF 基址。平台图标仍保留在
`app/static/platform-icons/` 供后续前端迁移使用，但 FastAPI 不再挂载或提供
它们。
