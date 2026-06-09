# GPT Image2 AstrBot 插件

通过 OpenAI 兼容 API 调用 GPT Image2 完成图片生成、图片编辑和 Plan 多轮提示词整理。

---

## 功能概览

- `/image2 draw <提示词>`：文生图；附带或引用图片时自动走图生图/编辑。
- `/image2 edit <提示词>`：显式图片编辑，要求当前消息或引用消息中包含图片。
- `/image2 plan`：进入多轮图文设计会话，确认后调用实际生图接口。
- 多站点容灾：主站失败后可按配置尝试普通备用站点和权威兜底站点。
- 站点费用观测：支持直接余额差观测、总额减用量观测（已提供 MICU 实测示例），以及固定参考成本展示。
- 诊断与统计：提供站点健康、失败记录、费用事件和诊断包。

---

## 快速开始

1. 将插件目录放入 AstrBot 的 `data/plugins/` 目录，或在 WebUI 上传打包后的 zip。
2. 在 WebUI 配置至少这些字段：
   - `api_key`
   - `base_url`
   - `api_mode`
   - `model` 或 `responses_model`
3. 重载插件后执行：

```text
/image2 providers
/image2 draw 一只白色小猫，简单测试图
```

如果需要备用站点、费用统计或 Plan 独立模型，请先阅读下方文档。

---

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `/image2 draw <提示词>` | 统一绘图；无图时文生图，带图时图生图/编辑 |
| `/image2 edit <提示词>` | 显式图片编辑；必须带图或引用带图消息 |
| `/image2 plan` | 进入 Plan 多轮图文会话 |
| `/plan confirm` | 在 Plan 会话中确认生成 |
| `/plan retry` | 重试上一条失败的 Plan 输入 |
| `/plan quit` | 退出 Plan 会话 |
| `/image2 mode [images\|responses]` | 查看或切换全局 API 模式（管理员） |
| `/image2 guard [images\|responses\|all] [on\|off]` | 切换 Prompt Guard（管理员） |
| `/image2 retry [global\|here\|interval] ...` | 控制备用站点重试提示（管理员） |
| `/image2 providers` | 查看站点状态、可用模式、健康和固定参考成本（管理员） |
| `/image2 balance` | 实时查询已配置余额接口的站点余额（管理员） |
| `/image2 costs` | 查看费用统计（管理员） |
| `/image2 costs recent [N]` | 查看最近 N 条费用事件（管理员） |
| `/image2 stats` | 查看 Provider 成功率、失败原因和耗时统计（管理员） |
| `/image2 stats recent [N]` | 查看最近 N 条失败记录（管理员） |
| `/image2 diag` | 生成诊断包（管理员） |
| `/image2 help` | 显示插件内帮助 |

---

## 文档索引

- [配置指南](./docs/configuration.md)：完整配置项、主站/备用站/权威兜底写法。
- [费用观测配置](./docs/billing.md)：余额接口测试、MICU 实测示例、固定参考成本和排错流程。

建议新用户按顺序阅读：先看配置指南，再看费用观测配置。

---

## 打包导入

生成 WebUI 可导入 zip：

```bash
uv sync
python scripts/package_plugin.py
```

生成临时测试包，不修改工作区版本号：

```bash
python scripts/package_plugin.py --dev-version
```

默认正式包输出示例：

```text
dist/astrbot_plugin_gpt_image2_233-v0.4.8.zip
```

测试包会写入类似 `v0.4.8-test.YYYYMMDD.HHMM` 的包内版本，便于在 AstrBot WebUI 区分部署版本。

---

## 开发验证

```bash
uv sync
python -m unittest discover -s tests
python -m compileall main.py image2_core tests
python -m ruff check .
git diff --check
```

---

## 注意事项

- `base_url` 应指向 OpenAI 兼容 API 根路径，例如 `https://api.example.com/v1`，不要包含 `images/generations`。
- 建议开启 `response_format_b64_json`，避免上游图片 URL 过期导致发送失败。
- Plan 模式使用 `/responses`，可复用主站配置，也可通过 `plan_use_custom_api` 使用独立 API。
- API Key 仅在运行时使用，不应写入 issue、PR、日志截图或公开文档。
- `render_text_as_image` 开启时，插件文本回复使用内置 Markdown 卡片模板，不依赖外部 JS/CDN。

---

## 许可证

MIT
