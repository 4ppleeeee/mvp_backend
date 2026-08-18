# TripGuard 独立管理后台设计

## 目标

将当前 FastAPI 内嵌的管理页面拆分为独立的 React + Vite + TypeScript 单页应用，并新建工蜂仓库 `tripguard-admin-web`。应用提供两个浏览器路由：

- `/admin`：外链/分享文案和图片的采集、任务状态、审核与已入库资料。
- `/admin/poi`：腾讯地点搜索、POI 网页抓取提交、聚合抓取状态和证据页查询。

登录页面、管理员密码散列和会话 Cookie 从本次管理后台路径中移除。FUE 的网关认证将在部署时作为外层保护接入；在此之前只允许将该无登录 BFF 用于受控的 test 环境，不能作为公网管理 API 暴露。

## 架构

```text
Browser
  -> tripguard-admin-web (FUE static hosting)
      -> TripGuard FastAPI /admin-api/*
          -> SQLite / uploads / ingestion worker
          -> Tencent Location Service
          -> Crawlab results API (Bearer token only in FastAPI)
```

前端从 `VITE_ADMIN_API_BASE_URL` 读取 API 基址；为空时使用同域相对路径。后端用 `TRIPGUARD_ADMIN_ALLOWED_ORIGINS` 限定 FUE test 域名，并且只有显式设置 `TRIPGUARD_ADMIN_API_ENABLED=true` 才注册 `/admin-api/*`；默认不注册，避免无登录 BFF 被意外暴露。CORS 只用于浏览器兼容，不能替代未来的 FUE 网关认证；启用该开关的环境仍必须由 FUE 网关认证保护。

## 后端 BFF 契约

所有新的管理接口位于 `/admin-api`，返回 JSON，且不返回管理员凭据、Crawlab token、腾讯地图 key、临时媒体路径或任意 URL 代理能力。

### 采集控制台

- `GET /admin-api/tasks?limit=20`：返回标题优先级与中文状态已计算好的任务列表。
- `POST /admin-api/tasks/url`：接受 `{ "url": "分享文案或 URL" }`，返回 `202 { job_id, status }`。
- `POST /admin-api/tasks/image`：接受 `multipart/form-data file`，返回 `202 { job_id, status }`。
- `GET /admin-api/tasks/{job_id}`：返回任务、展示字段、关联资料和证据。
- `POST /admin-api/tasks/{job_id}/review`：接受 `{ "decision": "accept|reject", "reason"?: string }`。
- `GET /admin-api/sources?limit=60`、`GET /admin-api/sources/{source_id}`：返回持久化的 `TravelSource` 和证据。
- `GET /admin-api/sources/{source_id}/cover`：保留现有公共地址检查、6 次跳转、图片 MIME 与 5 MiB 上限。

### POI 控制台

- `POST /admin-api/poi/location/suggest`：代理腾讯地点建议，输出规范化 POI。
- `POST /admin-api/poi/crawls`、`GET /admin-api/poi/crawls`、`GET /admin-api/poi/crawls/{crawl_task_id}`：代理 Crawlab POI 聚合作业。
- `GET /admin-api/poi/tasks/{native_task_id}/pages`、`POST /admin-api/poi/tasks/{native_task_id}/search`：读取或搜索已确认聚合作业的原生来源页。

后端仅在服务端注入 `Authorization: Bearer <Crawlab token>`；浏览器绝不持有该 token。

## 前端

`tripguard-admin-web` 使用 React Router：`/admin` 为采集控制台，`/admin/poi` 为 POI 控制台；初始访问 `/` 重定向至 `/admin`。共享 API client 将非 2xx 响应转换为可显示错误。

采集页保留任务与资料两个 tab、URL/图片提交、任务详情两秒轮询、人工审核和资料详情。POI 页保留地点候选、公开来源 URL 提交、抓取状态、原生页面阅读与文本搜索。两页都沿用暖白旅行资料工作台视觉，不包含登录/退出 UI。

## 验收

1. 后端不再注册 Jinja `/admin` 与登录路由，而是提供覆盖两个控制台需要的 `/admin-api/*` JSON 接口。
2. 采集任务仍复用既有异步 worker、标题优先级、审核与安全封面代理。
3. POI 请求经后端代理，且浏览器响应与前端构建产物中没有 Crawlab 或腾讯地图凭据。
4. 新工蜂仓库能通过 `npm run build`，并拥有 `/admin`、`/admin/poi` 两条可访问路由。
5. 后端 API 测试与前端组件/API 行为测试通过；后续 FUE test 部署只需配置 API 基址和受控 CORS origin。
