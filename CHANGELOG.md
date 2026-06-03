# Changelog

本文件记录 GPT Image2 AstrBot 插件的重要版本变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

---

## v0.4.6 - 2026-06-03

### v0.4.6 Added

- `/image2 stats` 新增平均任务完成耗时，用于从用户任务入口到最终可显示结果的宏观体验统计。
- 各站点统计新增平均成功耗时和平均失败耗时，按真实上游请求往返时间记录，不包含本地图片转换、表单构建和结果链构建。

### v0.4.6 Changed

- Provider stats schema 升级到 v4，新增 task/provider 耗时聚合字段并兼容旧统计文件缺失字段的展示。
- 升级版本至 0.4.6。

---

## v0.4.5 - 2026-05-26

### v0.4.5 Added

- `/image2 draw <提示词>` 现在会自动检测当前消息或引用消息中的图片：无图时文生图，带图时自动图生图/编辑。
- 新增 `draw_aliases` 配置项，用自定义前缀触发统一 `/image2 draw` 入口。

### v0.4.5 Changed

- `edit_aliases` 更名为 `draw_aliases`；自定义别名现在走统一 draw 逻辑，而不是严格 edit 逻辑。
  从旧版本升级时，需要在 WebUI 中把原 `edit_aliases` 配置手动迁移到 `draw_aliases`。
- Plan 成功后的可复制复用命令统一使用 `/image2 draw`，带参考图时仍提示用户附带或引用相同参考图。
- 升级版本至 0.4.5。

---

## v0.4.4 - 2026-05-25

### v0.4.4 Added

- Images API 返回 HTTP 2xx 但 JSON 结构异常时，现在会把脱敏响应预览和 JSON
  结构摘要写入 `provider_failures.jsonl`。
- `/image2 stats recent [N]` 新增展示 `response_json_summary`，便于定位站点返回
  空 `data`、非 JSON 或非标准兼容格式的问题。

### v0.4.4 Changed

- 升级版本至 0.4.4。

---

## v0.4.3 - 2026-05-25

### v0.4.3 Added

- 新增 `/plan retry` 和 `/image2 plan retry`，Plan 模型调用失败后可直接重试上一条
  Plan 输入，无需用户重新发送长文本或重新携带参考图。

### v0.4.3 Changed

- Plan 模型调用失败不再消耗最大对话轮数；成功返回后才会写入会话历史和轮次。
- 升级版本至 0.4.3。

---

## v0.4.2 - 2026-05-25

### v0.4.2 Added

- 新增 `/image2 retry` 管理员命令，用于查看和切换备用站点重试提示。
- 新增 `provider_retry_notice_enabled` 全局开关，可关闭中途切换备用站点提示。
- 新增 `provider_retry_notice_sessions` 会话级覆盖，可通过 `/image2 retry here on/off`
  按当前群或私聊会话控制重试提示。
- 新增 `provider_retry_notice_interval`，同一群/会话内按最短间隔合并切换提示，
  间隔内的多次失败切换会汇总到下一条提示摘要中，减少群聊刷屏。

### v0.4.2 Fixed

- 修复 Plan 会话会拦截 `/image2 stats`、`/image2 providers` 等非 Plan 命令的问题。
- 修复 `/plan confirm` 长时间多站点生图时可能被 Plan 空闲超时清理，导致提示语和
  重试状态矛盾的问题。

### v0.4.2 Changed

- 升级版本至 0.4.2。

---

## v0.4.1 - 2026-05-21

### v0.4.1 Fixed

- 修复 `/image2 diag` 诊断包脱敏不完整：`config_redacted.json` 现在递归脱敏嵌套
  API Key、Token、Secret、Password，包括 `fallback_api_providers` 字符串中的
  `api_key=` / `key=` 参数、JSON 编码配置、URL 凭据和查询参数、裸 `sk-`/`fk-` 密钥。
- 修复 `/image2 diag` 在 `provider_failures.jsonl` 不存在或读取失败时未写入空文件的问题；
  诊断包现在始终包含该文件。
- 修复诊断包中权威兜底、Plan 独立 API Key 等配置项可能未被完整脱敏的问题。

### v0.4.1 Added

- 新增独立 `config_redact.py` 脱敏工具模块，便于无 AstrBot 运行时测试。
- 非 2xx API 响应现在通过结构化诊断写入 `provider_failures.jsonl`，包含状态码、
  content-type、请求 ID、响应预览和耗时。
- 响应预览新增 Bearer Token、`api_key` / `token` / `secret` / `password`、
  `sk-` / `fk-` 风格密钥和 URL-safe base64 脱敏。

---

## v0.4.0 - 2026-05-21

### v0.4.0 Added

- 新增 `/image2 stats` 管理员命令，显示 Provider 聚合统计、失败原因分布、
  失败状态码分布、各站点详情。
- 新增 `/image2 stats recent [N]` 命令查看最近 N 条失败记录。
- 新增 `/image2 diag` 管理员命令，生成包含 summary.md、provider_stats.json、
  provider_failures.jsonl、config_redacted.json、version.txt 的诊断压缩包。
- 新增 `provider_failures.jsonl` 失败详情文件，追加每次生图失败的脱敏记录
  （时间戳、Provider、原因分类、状态码、重试性等），不存储提示词、图片或
  API Key。
- 新增 v2 provider_stats 字段：per-provider `failure_reasons` 和
  `failure_status_codes` 计数；顶层 `summary` 聚合。
- 新增 `_classify_failure_reason` 分类器：network_timeout/connect/proxy/protocol、
  http_400-524、html_error_page、api_schema_error、provider_compatibility、unknown。
- HTTP 失败日志增强：在 Images generate、Images edit、Responses 和 Plan Responses
  的错误分支中记录 status、elapsed_ms、response_bytes、content_type、请求 ID 头
  （x-request-id/cf-ray/openai-request-id）和脱敏响应预览。
- Provider 失败尝试记录 elapsed_ms。
- `/image2 help` 新增 `/image2 stats` 和 `/image2 diag` 命令提示。

### v0.4.0 Changed

- 升级版本至 0.4.0。
- provider_stats.json 兼容旧 v1 文件，新增 v2 字段后自动升级。
- provider_failures.jsonl 自动裁剪至最近 5000 行以避免无限增长。

---

## v0.3.0 - 2026-05-20

### v0.3.0 Added

- 新增全局 `api_mode` 模式过滤：draw/edit 仅尝试支持当前全局模式的站点。
- 新增 `primary_provider_name` 配置，用于显示主站点名称。
- 新增独立权威兜底配置节：`authoritative_fallback_enabled`、`authoritative_fallback_name`、
  `authoritative_fallback_api_key`、`authoritative_fallback_base_url`、
  `authoritative_fallback_images_model`、`authoritative_fallback_responses_model`。
- 新增 `/image2 providers` 管理员命令，显示站点顺序、能力、健康状态。
- 新增 `capabilities` 字段支持（`images`/`responses`/`all`/`both`），用于声明
  fallback 备用站点的能力范围。旧 `api_mode` 字段仍可自动推断 `capabilities`。

### v0.3.0 Changed

- Provider 配置模型移除独立 `api_mode`，能力由 `model` / `responses_model` 是否为
  非空字符串决定。新增 `supports_mode()` / `model_for_mode()` 能力查询方法。
- 排序改为三段固定：primary → 动态排序 normal → authoritative_fallback。
  仅 normal 角色参与自适应健康排序和冷却降级。
- `_parse_fallback_api_provider` 支持 `capabilities` 解析和旧 `api_mode` 自动推断。
- provider_stats.json 写入新增 `images_model` / `responses_model` 字段。
- 未命名 fallback 站点的 provider_id 不再包含旧 `api_mode`，升级后这类站点的健康统计会重新开始累计。
- `/image2 help` 新增 `/image2 providers` 命令提示。
- 升级版本至 0.3.0。

### v0.3.0 Deprecated

- fallback 列表中的 `role=authoritative_fallback` 仍兼容但优先使用独立配置节；
  启用独立配置节后列表中该类项将被忽略并记录 warning。

---

## v0.2.0 - 2026-05-19

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
- 新增 Images API 与 Responses API 的 Prompt Guard 独立配置。
- 新增 `/image2 guard` 管理员命令，用于查看和切换 Prompt Guard。
- draw/edit 成功时将模型返回的 `revised_prompt` 收纳为合并转发，图片单独发送。
- Plan 会话参考图按 data URL 去重，避免同一图片跨轮次或引用链重复计数。
- Plan confirm 生图失败时发送纯文本可复制的 `/image2 edit` 或 `/image2 draw` 直接重试命令。
- Plan confirm 生图成功后也通过合并转发发送可复制命令，方便在其他地方复用相同提示词。
- 新增 `send_copyable_prompt_after_success` 配置，用于控制 Plan 成功后是否发送可复制命令。
- Plan 最终提示词改为同时生成中文提示词和英文/混合提示词，并在 confirm 时通过合并转发发送。
- VSCode Run and Debug 默认打包配置改为生成测试包，并保留 release 配置。

### v0.2.0 Changed

- `/image2 edit` 改为复用统一生图链路，支持多站点 fallback。
- 默认请求超时时间从 `600` 秒调整为 `120` 秒，便于测试和快速切换备用站点。
- 多站点错误摘要改为更紧凑的 Markdown 预览，避免上游多行错误导致卡片显示混乱。
- 网络请求失败时显示异常类型、请求 URL、耗时和 timeout，避免只显示空的“网络请求失败”。
- `HTTP 524` 错误提示改为通用网关超时说明，不再在非 Plan 场景误提示 `/plan confirm`。
- `HTTP 400`、API 返回结构异常、API 返回错误现在会继续尝试后续备用站点。
- help 信息显示生图 API 站点数量和自适应站点优先级状态。
- Images API 的 `n` 会先尝试上游原生参数；上游不支持或返回不足时自动用单图并发请求补足。

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
