# 关键帧媒体管线设计

## 目标

在不接入视觉模型的前提下，为 TripGuard 增加可选的视频关键帧媒体管线：下载临时完整视频、按间隔抽帧、去重、拼接网格图，并让 `VideoPipeline` 返回可供后续视觉分析使用的图片数据。

## 范围与约束

- 关键帧流程默认关闭，现有字幕优先和 Whisper fallback 行为保持不变。
- 仅在显式开启关键帧选项时下载完整视频；不下载 Cookie、不使用验证码或浏览器验证绕过。
- 原视频、单帧和网格图只存在于任务临时目录，`VideoPipeline.extract()` 返回前将它们编码为临时 data URL，任务目录退出时统一删除。
- 本阶段不修改 LLM 请求，不调用视觉模型，不改变 `TravelSource` 或公共搜索数据。
- 当前实现只返回网格图 data URL，不长期保存关键帧图片。

## 数据流

```text
VideoPipeline.extract()
  -> fetch_caption()
  -> fetch_metadata()
  -> [keyframes enabled] download full video into JobDirectory
  -> FFmpeg extract frames at configured interval
  -> remove adjacent identical frames
  -> group frames by grid size and compose JPEG grids
  -> encode grid JPEGs as data:image/jpeg;base64 URLs
  -> if no caption, download temporary audio and run Whisper
  -> EvidenceBundle(metadata, transcript, keyframe_images)
  -> JobDirectory cleanup
```

字幕仍然优先于 Whisper。关键帧开启后，即使已有字幕，也需要额外下载完整视频；关键帧失败应作为媒体提取错误返回，不静默伪造图片结果。

## 接口设计

### `EvidenceBundle`

增加：

```python
keyframe_images: tuple[str, ...] = ()
```

每一项是 JPEG 网格图的 `data:image/jpeg;base64,...` 字符串。使用不可变 tuple，避免调用方修改管线结果。

### `VideoPipeline`

构造参数增加可选配置：

```python
keyframe_enabled: bool = False
frame_interval_seconds: int = 6
grid_size: tuple[int, int] = (2, 2)
```

默认值保持现有行为。网格图每组包含 `rows * columns` 张帧；不完整的最后一组跳过，避免发送尺寸不一致的网格。

### 媒体下载与抽帧模块

- 在现有 `BiliNoteYtDlpAcquirer` 中增加完整视频临时下载能力。
- 抽帧模块使用系统 FFmpeg 和 Python 标准库/已有依赖，不引入模型依赖。
- 抽帧时间戳从 0 开始，按间隔递增，并限制最大帧数。
- 图片在拼图前统一缩放并标注时间戳；拼图后编码为 data URL。

## 错误处理

- 参数非法（间隔小于 1、网格尺寸小于 1）在构造抽帧器时拒绝。
- yt-dlp、FFmpeg、图片读取或拼图失败统一转换为带阶段和出口信息的 `MediaExtractionError`。
- `JobDirectory` 的上下文清理必须覆盖成功和异常路径。
- 关键帧关闭时不触发完整视频下载，也不影响已有字幕/音频流程。

## 测试与验收

- 单元测试验证 yt-dlp 完整视频下载的临时输出路径和媒体出口参数。
- 使用假的 FFmpeg/图片输入测试抽帧时间点、去重、网格数量和 data URL 输出。
- 管线测试验证关键帧开启时 `EvidenceBundle.keyframe_images` 有值，关闭时为空。
- 管线测试验证异常时任务目录被清理。
- 在 claw 容器内用真实公开视频做一次不接模型的媒体链路验证，确认返回网格图片数量和 Base64 前缀。

