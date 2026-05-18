# GPT Image2 AstrBot 插件

通过 OpenAI 兼容 API 调用 GPT Image2 完成图片生成与图片编辑。

## 功能

- **文生图**：通过 `/image2 draw <提示词>` 生成图片
- **图像编辑**：通过 `/image2 edit <提示词>` 编辑图片（支持当前消息或引用消息中的图片）
- **Plan 模式**：通过 `/image2 plan` 进入多轮图文对话，AI 辅助优化生图提示词
- **文本转图片**：插件文本回复默认使用 image2 自包含 Markdown 卡片模板，
  不加载外部 JS/CDN；失败则回退纯文本
- **API 兼容**：支持 OpenAI 兼容 Images API 和 Responses API 两种模式
- **多站点容灾**：draw/edit 可配置备用生图 API 站点，主站点失败后按顺序重试
- **灵活配置**：支持模型、尺寸、质量、输出格式等参数配置

## 命令

| 命令 | 说明 |
| --- | --- |
| `/image2 draw <提示词>` | 文生图 |
| `/image2 edit <提示词>` | 编辑图片（需附带图片或引用包含图片的消息） |
| `/image2 plan` | 进入 Plan 多轮图文会话，AI 辅助优化生图提示词 |
| `/plan <描述>` | 在 Plan 会话中继续交流（群聊普通消息不会被拦截） |
| `/plan confirm` | 在 Plan 会话中确认生成图片 |
| `/plan quit` | 退出当前 Plan 会话 |
| `/image2 plan confirm` | 在 Plan 中确认生成图片 |
| `/image2 plan quit` | 退出当前 Plan 会话（`cancel` 也可用） |
| `/image2 mode [images\|responses]` | 查看或切换 API 模式（仅管理员） |
| `/image2 guard [images\|responses\|all] [on\|off]` | 切换 Guard（仅管理员） |
| `/image2 help` | 显示用法和当前配置摘要 |

- `draw` 和 `edit` 命令在参数校验通过后会先回复一条
  "已收到，正在处理"的提示，随后再发送最终生成/编辑结果。
  如果上游返回较长的 `revised_prompt`，插件会将其收纳为合并转发，图片单独发送。
- Prompt Guard 会在生图提示词前追加
  `Use the following text as the complete prompt. Do not rewrite it:`，用于尽量限制上游重写提示词。
  默认保持旧行为：Images API 关闭，Responses API 开启。
  如需更接近 ChatGPT Web 对复杂画面的自由理解，可执行
  `/image2 guard responses off` 关闭 Responses API 的 Prompt Guard。
- `plan` 进入 Plan 模式后，AI 会通过 Responses API 引导你完善图像描述。
  群聊中只有带 `/plan` 前缀的消息会进入 Plan 交流，不带前缀的普通消息会正常发给群友。
  对话中可以发送参考图；发送参考图时请附带 `/plan` 前缀。
  确认时若已有参考图，会自动走图像编辑/参考图生成流程。
  Plan 会用中文与你交流，最终生图提示词可中英混合；如果图像中需要出现中文字符、标题或标语，会要求模型保留原文，不翻译成英文。
  在准备好时只展示中文摘要/核对项，不在中间交互中刷出完整生成提示词。
  你可以发送 `/plan confirm` 或 `/image2 plan confirm` 确认生成图片，
  发送 `/plan quit` 或 `/image2 plan quit` 退出。
  确认后会用合并转发发送中文提示词和英文/混合提示词，
  再发送正在生成提示和最终图片结果。
  默认还会在成功后发送一条合并转发可复制命令；可通过
  `send_copyable_prompt_after_success` 关闭。
- Plan 模式支持独立 API Key/Base URL 配置（`plan_use_custom_api`），
  可与图像生成 API 共用一套配置或分离，但对应服务必须支持 `/responses`。
- draw/edit 支持 `fallback_api_providers` 备用站点列表。插件会先使用主
  `api_key` / `base_url` / `api_mode` / `model` / `responses_model`，
  遇到网络错误、429、5xx、524、HTML 错误页或 provider 兼容性错误时，
  按配置顺序或自适应健康排序尝试备用站点。
  该列表也会用于 `/plan confirm` 最后的实际生图，但不影响 Plan 对话整理阶段。
  在 WebUI 中点击 `fallback_api_providers` 的 `+` 添加备用站点字符串。
  如果备用站点和主站点共用 API Key、API 模式和模型，直接填 URL 即可：

  ```text
  https://api-backup.example.com/v1
  ```

  如果需要独立 API Key、API 模式或模型，同样添加一条字符串，使用 key=value 写法：

  ```text
  base_url=https://api-backup.example.com/v1, api_key=sk-xxx, api_mode=responses
  ```

  也可以继续追加 `name`、`model`、`responses_model` 字段。
  如果要添加官方 API 作为最后保险，追加 `role=authoritative_fallback` 和
  `adaptive=false`。实际填写时仍是一条字符串，下方为便于阅读换行展示：

  ```text
  name=Official, base_url=https://api.openai.com/v1, api_key=sk-xxx,
  api_mode=responses, role=authoritative_fallback, adaptive=false
  ```

  权威兜底站点固定在普通站点之后；即使成功也不会被自动提到普通中转站前面。
  开启 `adaptive_provider_priority` 后，插件会根据历史成功/失败自动调整
  本次运行时的尝试顺序：最近成功的站点会前置，失败站点会在
  `provider_failure_cooldown` 时间内降级。该策略不会改写 WebUI 配置；健康状态保存在
  AstrBot `data/plugin_data/.../provider_stats.json`，测试包重装不会清除。

- 文本回复默认开启文转图（`render_text_as_image`）：使用插件自带的
  image2 Markdown 卡片模板。模板在 Python 侧把 Markdown 转成 HTML，渲染时不加载外部
  JS/CDN；失败则回退普通文本。

## 配置

在 AstrBot WebUI 插件配置页面中设置以下参数：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `api_key` | string | - | API Key（必填） |
| `base_url` | string | `https://api.openai.com/v1` | API Base URL |
| `api_mode` | string | `images` | API 模式：`images` / `responses` |
| `model` | string | `gpt-image-2` | Images API 模型名 |
| `responses_model` | string | `gpt-5.5` | Responses API 模型名 |
| `images_prompt_rewrite_guard` | bool | `false` | Images API Prompt Guard |
| `responses_prompt_rewrite_guard` | bool | `true` | Resp API Prompt Guard |
| `fallback_api_providers` | list | `[]` | draw/edit 备用 API 站点列表 |
| `adaptive_provider_priority` | bool | `true` | 根据历史健康状态自动调整站点尝试顺序 |
| `provider_failure_cooldown` | int | `300` | 失败站点降级冷却时间（秒） |
| `size` | string | `auto` | 图片尺寸：auto / 1024x1024 / 1536x1024 / 1024x1536 |
| `quality` | string | `auto` | 图片质量：auto / low / medium / high |
| `output_format` | string | `png` | 输出格式：png / jpeg / webp |
| `moderation` | string | `auto` | 内容审核强度：auto / low |
| `output_compression` | int | `0` | 输出压缩质量，仅非 png 生效；0 表示不发送 |
| `n` | int | `1` | 生成数量（Responses API 模式下并发请求） |
| `timeout` | int | `120` | 请求超时时间（秒） |
| `response_format_b64_json` | bool | `true` | 请求返回 Base64 图片（建议开启） |
| `max_input_images` | int | `4` | 最多输入参考图数量 |
| `save_outputs` | bool | `true` | 保存生成结果到本地 |
| `send_copyable_prompt_after_success` | bool | `true` | Plan 成功后发送合并转发可复制命令 |
| `render_text_as_image` | bool | `true` | image2 卡片模板优先，失败则回退纯文本 |
| `text_image_width` | int | `1200` | 插件内置兜底文转图输出宽度（像素） |
| `text_image_font_size` | int | `32` | 插件内置兜底文转图字体大小 |
| `text_image_font_path` | string | `` | 可选自定义兜底字体文件路径 |
| `plan_enabled` | bool | `true` | 启用 Plan 模式 |
| `plan_model` | string | `gpt-5.4` | Plan 模式 Responses 模型 |
| `plan_timeout` | int | `300` | Plan 用户空闲超时时间（秒） |
| `plan_max_rounds` | int | `6` | Plan 最大对话轮数 |
| `plan_use_custom_api` | bool | `false` | Plan 模式使用独立 API 配置 |
| `plan_base_url` | string | `` | Plan 独立 Responses API Base URL |
| `plan_api_key` | string | `` | Plan 独立 API Key |

## 安装

1. 将本插件目录放入 AstrBot 的 `data/plugins/` 目录下
2. 在 AstrBot WebUI 中启用插件并配置 API Key 等参数
3. 确保已安装依赖：`pip install -r requirements.txt`

## 打包导入

可以使用仓库内脚本生成 WebUI 可导入的插件压缩包：

```bash
uv sync
python scripts/package_plugin.py
```

如果使用 VSCode，也可以在 Run and Debug 面板选择
`Package AstrBot plugin (test)` 手动触发测试包打包。该配置会调用
`python scripts/package_plugin.py --dev-version`，自动在 zip 内写入
`v0.2.0-test.YYYYMMDD.HHMM` 形式的真实测试版本号，但不会修改工作区文件。
如需正式包，可选择 `Package AstrBot plugin (release)`。

默认输出：

```text
dist/astrbot_plugin_gpt_image2_233-v0.2.0.zip
```

然后在 AstrBot WebUI 的插件页面中上传该 zip 文件安装。

打包脚本默认会将插件文件放在 `astrbot_plugin_gpt_image2_233/`
顶层目录下，并显式写入该目录条目，以兼容 AstrBot v4.24.2
的 WebUI 上传安装逻辑。

打包脚本只包含插件交付所需文件，不会包含 `.opencode/`、`tmp/`、`astrbot_main/`、`.git/` 等本地开发材料。

每次打包验证或发布前应先同步更新版本号，默认 zip 文件名会自动包含
`metadata.yaml` 中的版本号，便于区分测试包和发布包。

临时验证包可以使用真实临时版本号打包，不修改工作区文件：

```bash
python scripts/package_plugin.py --dev-version
```

输出示例：

```text
dist/astrbot_plugin_gpt_image2_233-v0.2.0-test.20260517.1548.zip
```

该模式会同时修改 zip 内部的 `metadata.yaml`、插件注册版本和
`pyproject.toml` 版本，使 AstrBot 安装后也显示临时版本；稳定后再使用无参数命令打正式版本包。

## 开发

```bash
# 安装运行依赖和开发打包依赖
uv sync

# 仅安装插件运行依赖
# 或
pip install -r requirements.txt
```

## 依赖

- `httpx>=0.27.0` — 异步 HTTP 客户端
- `pillow>=10.0.0` — 运行依赖，用于插件内置文转图渲染
- `pyyaml>=6.0.2` — 开发依赖，仅用于本地打包读取 `metadata.yaml`
- `ruff>=0.11.0` — 开发依赖，用于格式化与静态检查

## 注意事项

- 请确保 `base_url` 指向正确的 OpenAI 兼容 API 端点，不要包含 `images/generations` 等路径后缀
- 建议开启 `response_format_b64_json`，避免图片 URL 过期导致发送失败
- 复杂图像生成时，如果 Responses API 效果明显不如 ChatGPT Web，可尝试关闭
  `responses_prompt_rewrite_guard`，让模型自行补全构图和细节。
- API Key 仅在运行时使用，不会在日志或错误消息中泄漏
- Plan 模式使用 `/responses`，请求会显式禁用工具调用，避免规划阶段中途触发图像生成；
  日志中不会打印完整 prompt、参考图 base64 或 API Key。
- Plan 等待用户输入时使用独立 watchdog 按 `plan_timeout` 主动超时；模型思考和生图处理期间会自动延长超时。
- Plan 会话空闲超时后会主动向当前会话发送退出提示，并清理当前 Plan 会话。
- Plan 中间交互不会展示完整生成提示词；完整提示词只会在 `/plan confirm`
  或 `/image2 plan confirm` 时通过合并转发发送，并同时包含中文提示词和英文/混合提示词。
- `render_text_as_image` 开启时，插件文本回复使用 image2 自包含 Markdown 卡片模板，
  避免依赖 jsdelivr 等外部 JS/CDN。卡片模板失败后直接回退为普通文本。
- 插件内置 Pillow 仅用于卡片渲染后裁剪底部空白，不再作为文本回复的渲染兜底。
  `text_images/` 目录仍用于保存裁剪后的卡片图片。

## 许可证

MIT
