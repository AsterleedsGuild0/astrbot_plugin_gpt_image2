# Changelog

本文件记录 GPT Image2 AstrBot 插件的重要版本变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

---

## v0.2.0 - 准备发布

### v0.2.0 Added

- 新增 draw/edit/`/plan confirm` 生图多站点 fallback。
- 新增 `fallback_api_providers` 字符串列表配置，适配 AstrBot WebUI 列表编辑器。
- 支持每个备用站点单独配置 `api_mode`、`model`、`responses_model`。
- 新增 `adaptive_provider_priority` 自适应站点优先级策略。
- 新增 `provider_failure_cooldown` 失败站点降级冷却时间。
- 新增 provider 健康状态持久化文件 `provider_stats.json`，保存在 AstrBot `plugin_data` 下。
- 新增官方权威兜底层：`role=authoritative_fallback` 与 `adaptive=false`。
- 新增备用站点切换进度提示，避免用户在多站点 fallback 期间无感等待。
- 生图成功时在结果消息中显示实际命中的 API 站点、尝试序号和耗时。
- Plan 会话参考图按 data URL 去重，避免同一图片跨轮次或引用链重复计数。
- Plan confirm 生图失败时发送纯文本可复制的 `/image2 edit` 或 `/image2 draw` 直接重试命令。
- Plan confirm 生图成功后也发送纯文本可复制命令，方便在其他地方复用相同提示词。
- 新增 `send_copyable_prompt_after_success` 配置，用于控制 Plan 成功后是否发送可复制命令。
- VSCode Run and Debug 默认打包配置改为生成测试包，并保留 release 配置。

### v0.2.0 Changed

- `/image2 edit` 改为复用统一生图链路，支持多站点 fallback。
- 默认请求超时时间从 `600` 秒调整为 `120` 秒，便于测试和快速切换备用站点。
- 多站点错误摘要改为更紧凑的 Markdown 预览，避免上游多行错误导致卡片显示混乱。
- 网络请求失败时显示异常类型、请求 URL、耗时和 timeout，避免只显示空的“网络请求失败”。
- `HTTP 524` 错误提示改为通用网关超时说明，不再在非 Plan 场景误提示 `/plan confirm`。
- `HTTP 400`、API 返回结构异常、API 返回错误现在会继续尝试后续备用站点。
- help 信息显示生图 API 站点数量和自适应站点优先级状态。

### v0.2.0 Fixed

- 修复 `/image2 edit` 只使用主站点、没有尝试备用站点的问题。
- 修复 WebUI 中 `fallback_api_providers` 被当成字符串列表时，旧 dict 解析逻辑无法生效的问题。
- 修复部分上游返回空 `data` 或错误对象时错误信息不够可诊断的问题。

---

## v0.1.1 - 2026-05-18

### v0.1.1 Added

- 新增 Plan 模式多轮提示词整理流程。
- 支持 Plan 会话中携带参考图，并在确认后复用参考图进行最终生图。
- 新增 image2 自包含 Markdown 卡片文本回复渲染。
- 新增卡片渲染后底部空白裁剪。
- 新增文本渲染诊断日志，辅助定位 HTML 渲染失败或返回非图片的问题。

### v0.1.1 Changed

- Plan 群聊交互改为只捕获 `/plan` 前缀消息，普通群聊消息不再被会话吞掉。
- Plan confirm 生图失败时保留会话、完整提示词和参考图，允许再次 confirm 重试。
- Plan 请求显式禁用工具调用，避免模型触发非预期 image generation tool。
- 文本回复链路调整为 image2 Markdown 卡片优先，失败后回退纯文本。

### v0.1.1 Fixed

- 修复带图消息中 `/plan` 不在消息开头时无法被识别的问题。
- 修复 Plan 会话失败后立即退出导致无法重试的问题。
- 优化 429、524、HTML 错误页和 `image_generation_call` 的中文错误提示。

---

## v0.1.0 - 初始版本

### v0.1.0 Added

- 支持 `/image2 draw <提示词>` 文生图。
- 支持 `/image2 edit <提示词>` 基于消息或引用消息中的图片进行编辑。
- 支持 OpenAI 兼容 Images API 与 Responses API。
- 支持配置图片尺寸、质量、输出格式、生成数量和输出保存。
- 支持通过脚本打包 AstrBot WebUI 可导入 zip。
