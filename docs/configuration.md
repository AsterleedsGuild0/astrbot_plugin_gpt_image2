# GPT Image2 配置指南

本文档说明 AstrBot WebUI 中各配置项的用途，以及主站、备用站和权威兜底站点的推荐写法。

---

## 最小可用配置

至少配置：

| 配置项 | 示例 | 说明 |
| --- | --- | --- |
| `api_key` | `sk-...` | 主站 API Key |
| `base_url` | `https://api.example.com/v1` | OpenAI 兼容 API 根路径，不要写到 `/images/generations` |
| `api_mode` | `images` | `images` 或 `responses` |
| `model` | `gpt-image-2` | Images API 模型 |
| `responses_model` | `gpt-5.5` | Responses API 模型 |

配置后先执行：

```text
/image2 providers
/image2 draw 一只白色小猫，简单测试图
```

---

## 主站配置

主站使用顶层配置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `primary_provider_name` | `primary` | 主站在日志和 `/image2 providers` 中展示的名称 |
| `api_key` | - | 主站 API Key |
| `base_url` | `https://api.openai.com/v1` | 主站 API Base URL |
| `model` | `gpt-image-2` | 主站 Images API 模型 |
| `responses_model` | `gpt-5.5` | 主站 Responses API 模型 |
| `primary_billing_json` | `{}` | 主站费用观测配置，详见 [费用观测配置](./billing.md) |
| `primary_force_single_image_requests` | `false` | 本次任务全局 `n > 1` 且命中主站 Provider 时，强制拆成多次 `n=1` 上游请求 |

主站统计身份绑定 `base_url`，不绑定展示名或模型名。

---

## 备用站点配置

`fallback_api_providers` 是 JSON 编辑器字段，必须填写 JSON 数组。

破坏式更新后，不再支持只填 URL、key-value 字符串或 WebUI 字符串列表。

### JSON 数组写法

最小示例：

```json
[
  {
    "name": "backup-1",
    "base_url": "https://api-backup.example.com/v1"
  }
]
```

省略 `api_key`、`model`、`responses_model` 时会继承主站配置。

完整示例：

```json
[
  {
    "name": "backup-1",
    "base_url": "https://api-backup.example.com/v1",
    "api_key": "<api-key>",
    "capabilities": "images",
    "model": "gpt-image-2",
    "force_single_image_requests": true,
    "billing": {
      "total_url": "https://www.micuapi.ai/dashboard/billing/subscription",
      "total_json_path": "soft_limit_usd",
      "usage_url": "https://www.micuapi.ai/dashboard/billing/usage",
      "usage_json_path": "total_usage",
      "usage_scale": 0.01,
      "currency": "CNY",
      "scale": 1,
      "balance_multiplier": 1,
      "success_cost": 0.2,
      "failure_cost": 0
    }
  }
]
```

上面的 `billing` 展示的是完整余额/用量查询配置；插件已实现 `/image2 balance` 实时余额查询，以及生图前后余额差成本观测。若站点没有可用余额接口，也可以只填写固定参考成本：

```json
{
  "billing": {
    "currency": "CNY",
    "success_cost": 0.2,
    "failure_cost": 0
  }
}
```

固定参考成本可以配合手动余额锚点使用，例如 `/image2 balance set LTCraftAI 78.09`。这类余额会标注为“手动锚点估算”，不是站点实时余额；展示单位和余额换算倍率从对应 Provider 的 `billing` 配置读取。`balance_multiplier` 表示 1 个站点余额数值折算成多少展示单位，不一定是真实世界汇率，也可能是站长自定义充值倍率。sub2api-like 网关的 `/v1/usage` 如果返回 Key 配额或订阅用量，也建议走固定参考成本和手动余额锚点，不要直接当作钱包余额。

支持字段：

| 字段 | 说明 |
| --- | --- |
| `name` | 站点显示名；建议每个备用站唯一 |
| `base_url` | OpenAI 兼容 API 根路径 |
| `api_key` | 备用站 API Key；省略时复用主站 Key |
| `capabilities` | `all` / `images` / `responses` / `both` |
| `model` | Images API 模型 |
| `responses_model` | Responses API 模型 |
| `adaptive` | 是否参与自适应排序，默认参与 |
| `force_single_image_requests` | 本次任务全局 `n > 1` 且命中该 Provider 时，是否强制拆成多次 `n=1` 上游请求 |
| `billing` | 该 Provider 的费用观测配置；支持直接余额、总额减用量和固定参考成本，详见 [费用观测配置](./billing.md) |

`n` 是全局生成数量，不区分主站、备用站或权威兜底站。`force_single_image_requests` 只控制“命中某个 Provider 后，是否把该 Provider 的一次原生 `n` 请求拆成多次 `n=1` 上游请求”。它不会把整次任务的出图数量限制为 1；例如全局 `n=2` 时，开启后仍可能向同一 Provider 发两次 `n=1`，最终返回 2 张图。如果站点会按原生 `n` 扣费但实际只返回 1 张图，建议对该 Provider 开启 `force_single_image_requests`，避免原生 `n=2` 被站点扣两张但插件只收到一张。

---

## 权威兜底站点

权威兜底站点固定排在最后，只在主站和普通备用站失败后尝试。

推荐使用独立配置节：

| 配置项 | 说明 |
| --- | --- |
| `authoritative_fallback_enabled` | 是否启用权威兜底 |
| `authoritative_fallback_name` | 站点显示名 |
| `authoritative_fallback_api_key` | 独立 API Key；留空复用主站 Key |
| `authoritative_fallback_base_url` | 独立 Base URL；留空复用主站 Base URL |
| `authoritative_fallback_images_model` | Images API 模型；留空表示不支持 Images |
| `authoritative_fallback_responses_model` | Responses API 模型；留空表示不支持 Responses |
| `authoritative_fallback_billing_json` | 费用观测配置，详见 [费用观测配置](./billing.md) |
| `authoritative_fallback_force_single_image_requests` | 本次任务全局 `n > 1` 且命中权威兜底 Provider 时，强制拆成多次 `n=1` 上游请求 |

---

## 模式和能力匹配

`api_mode` 是全局模式：

- `images`：只尝试支持 Images API 的站点。
- `responses`：只尝试支持 Responses API 的站点。

备用站的 `capabilities` 控制是否参与当前模式：

| capabilities | Images 模式 | Responses 模式 |
| --- | --- | --- |
| `all` / 省略 | 可用 | 可用 |
| `both` | 可用 | 可用 |
| `images` | 可用 | 跳过 |
| `responses` | 跳过 | 可用 |

检查当前可用站点：

```text
/image2 providers
```

---

## 完整配置项速查

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `api_key` | string | - | 主站 API Key |
| `base_url` | string | `https://api.openai.com/v1` | 主站 API Base URL |
| `api_mode` | string | `images` | 全局 API 模式：`images` / `responses` |
| `primary_provider_name` | string | `primary` | 主站点显示名称 |
| `primary_billing_json` | text/json | `{}` | 主站费用观测配置 |
| `primary_force_single_image_requests` | bool | `false` | 全局 `n > 1` 且命中主站 Provider 时强制单图上游请求 |
| `model` | string | `gpt-image-2` | 主站 Images API 模型名 |
| `responses_model` | string | `gpt-5.5` | 主站 Responses API 模型名 |
| `images_prompt_rewrite_guard` | bool | `false` | Images API Prompt Guard |
| `responses_prompt_rewrite_guard` | bool | `true` | Responses API Prompt Guard |
| `fallback_api_providers` | text/json | `[]` | draw/edit 备用 API 站点 JSON 数组 |
| `authoritative_fallback_enabled` | bool | `false` | 启用权威兜底站点 |
| `authoritative_fallback_name` | string | `authoritative-fallback` | 权威兜底站点名称 |
| `authoritative_fallback_api_key` | string | `` | 权威兜底 API Key |
| `authoritative_fallback_base_url` | string | `` | 权威兜底 Base URL |
| `authoritative_fallback_images_model` | string | `` | 权威兜底 Images 模型，空表示不支持 |
| `authoritative_fallback_responses_model` | string | `` | 权威兜底 Responses 模型，空表示不支持 |
| `authoritative_fallback_billing_json` | text/json | `{}` | 权威兜底费用观测配置 |
| `authoritative_fallback_force_single_image_requests` | bool | `false` | 全局 `n > 1` 且命中权威兜底 Provider 时强制单图上游请求 |
| `adaptive_provider_priority` | bool | `true` | 根据历史健康状态自动调整普通备用站点顺序 |
| `provider_failure_cooldown` | int | `300` | 失败站点降级冷却时间，单位秒 |
| `provider_retry_notice_enabled` | bool | `true` | 全局控制备用站点中途切换提示 |
| `provider_retry_notice_interval` | int | `300` | 同一群/会话重试提示合并间隔，单位秒 |
| `provider_retry_notice_sessions` | string | `{}` | 按群/会话覆盖重试提示开关的 JSON |
| `size` | string | `auto` | 图片尺寸 |
| `quality` | string | `auto` | 图片质量 |
| `output_format` | string | `png` | 输出格式：`png` / `jpeg` / `webp` |
| `moderation` | string | `auto` | 内容审核强度 |
| `output_compression` | int | `0` | 非 png 输出压缩质量；0 表示不发送 |
| `n` | int | `1` | 生成数量 |
| `timeout` | int | `120` | 请求超时时间，单位秒 |
| `response_format_b64_json` | bool | `true` | 请求返回 Base64 图片 |
| `max_input_images` | int | `4` | 最多输入参考图数量 |
| `draw_aliases` | list | `[]` | 触发统一 `/image2 draw` 的自定义前缀 |
| `save_outputs` | bool | `true` | 保存生成结果到本地 |
| `send_copyable_prompt_after_success` | bool | `true` | Plan 成功后发送合并转发可复制命令 |
| `render_text_as_image` | bool | `true` | 文本回复优先渲染为卡片图片 |
| `text_image_width` | int | `1200` | 插件内置兜底文转图输出宽度 |
| `text_image_font_size` | int | `32` | 插件内置兜底文转图字体大小 |
| `text_image_font_path` | string | `` | 可选自定义兜底字体路径 |
| `plan_enabled` | bool | `true` | 启用 Plan 模式 |
| `plan_model` | string | `gpt-5.4` | Plan 模式 Responses 模型 |
| `plan_timeout` | int | `300` | Plan 用户空闲超时时间 |
| `plan_max_rounds` | int | `6` | Plan 最大对话轮数 |
| `plan_use_custom_api` | bool | `false` | Plan 模式使用独立 API 配置 |
| `plan_base_url` | string | `` | Plan 独立 Responses API Base URL |
| `plan_api_key` | string | `` | Plan 独立 API Key |
