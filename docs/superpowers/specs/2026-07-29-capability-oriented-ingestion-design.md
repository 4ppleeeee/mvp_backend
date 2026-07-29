# 面向能力的 Ingestion 编排设计

## 目标

分离用户提交方式、内容来源、资源类型和可用解析能力。这样小宇宙纯音频、小红书视频、Bilibili 视频、文章和图片可以使用准确的来源专属获取逻辑，而不必被强行归入 `article` 或 `video` 分支。

## 设计决策

### 1. 输入形式与资源类型分离

`input_type` 描述用户如何提交内容：`url`、`image`、`text` 或 `file`。URL 本身不代表视频，上传文件本身也不代表图片。

`resource_kind` 描述探测到的资源类型：`article`、`audio`、`video`、`image`、`document` 或 `unknown`。迁移期间保留已有的 `media_type` 字段和 API 兼容性，但新的编排逻辑使用资源类型和能力集合。

### 2. 先识别来源，再决定能力

来源注册表根据 URL 主机识别平台，并创建来源适配器。适配器可以先执行公开探测，再返回最终资源类型和能力。小红书这类动态来源可以先处于未确定状态，之后再判断为文章或视频。

每个适配器负责自己的访问准备和资源获取细节。上层编排器不需要知道某个平台如何获取临时 token、跟随短链、解析 HTML 或调用公开 API。

来源访问边界通过上下文/Provider 扩展点表示。未来抖音可以在 `DouyinAccessContext` 内加入临时的 `MsTokenProvider`，但它不能变成全局的视频能力，也不能引入 Cookie 持久化、验证码处理、浏览器自动化或验证绕过。

### 3. 根据能力生成执行计划

适配器声明自己支持的能力，例如：

- `metadata`：元数据
- `article_body`：文章正文
- `caption`：平台字幕
- `audio`：音频获取
- `video`：视频获取
- `keyframes`：关键帧
- `ocr`：图片文字识别

规划器根据能力生成有序执行计划。通用顺序是：元数据、平台字幕、字幕缺失时的音频转写、可选视觉处理、LLM 分析和持久化。具体的来源适配器负责每个操作的实现，并可以在内部完成来源专属的前置步骤。

示例：

```text
小宇宙单集：metadata + audio + transcription
Bilibili 视频：metadata + caption + audio + transcription + 可选 video/keyframes
小红书图文：metadata + article_body
小红书视频：metadata + caption/audio + transcription + 可选 video/keyframes
图片上传：ocr + analysis
```

### 4. 迁移期间保持现有行为

第一阶段保留现有数据库字段和管理后台 API 响应结构。`media_type` 继续在兼容边界填充，但内部编排不再把 `video` 当作所有媒体处理流程的同义词。Bilibili、YouTube、抖音、快手、图片、文章现有的回退和证据持久化行为必须保持。

## 错误处理

来源专属的访问和获取错误继续包装为 `MediaExtractionError`，携带安全的阶段和出口信息。没有字幕属于正常的能力缺失，应继续回退到音频转写。真正不支持的能力必须返回清晰的来源专属错误，不能再以属性不存在的异常暴露。

## 非目标

- 不导入或持久化 Cookie。
- 不处理验证码或绕过验证。
- 不使用浏览器自动化。
- 不替换现有 evidence、LLM 或管理后台持久化模型。
- 本次重构不完整实现抖音 `msToken`，只定义其未来接入的来源访问上下文扩展点。
