# GPT Image2 AstrBot 插件

通过 OpenAI 兼容 API 调用 GPT Image2 完成图片生成与图片编辑。

## 功能

- **文生图**：通过 `/image2 draw <提示词>` 生成图片
- **图像编辑**：通过 `/image2 edit <提示词>` 编辑图片（支持当前消息或引用消息中的图片）
- **API 兼容**：支持 OpenAI 兼容 Images API 和 Responses API 两种模式
- **灵活配置**：支持模型、尺寸、质量、输出格式等参数配置

## 命令

| 命令 | 说明 |
| --- | --- |
| `/image2 draw <提示词>` | 文生图 |
| `/image2 edit <提示词>` | 编辑图片（需附带图片或引用包含图片的消息） |
| `/image2 mode [images\|responses]` | 查看或切换 API 模式（仅管理员） |
| `/image2 help` | 显示用法和当前配置摘要 |

## 配置

在 AstrBot WebUI 插件配置页面中设置以下参数：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `api_key` | string | - | API Key（必填） |
| `base_url` | string | `https://api.openai.com/v1` | API Base URL |
| `api_mode` | string | `images` | API 模式：`images` / `responses` |
| `model` | string | `gpt-image-2` | Images API 模型名 |
| `responses_model` | string | `gpt-5.5` | Responses API 模型名 |
| `size` | string | `auto` | 图片尺寸：auto / 1024x1024 / 1536x1024 / 1024x1536 |
| `quality` | string | `auto` | 图片质量：auto / low / medium / high |
| `output_format` | string | `png` | 输出格式：png / jpeg / webp |
| `moderation` | string | `auto` | 内容审核强度：auto / low |
| `output_compression` | int | `0` | 输出压缩质量，仅非 png 生效；0 表示不发送 |
| `n` | int | `1` | 生成数量（Responses API 模式下并发请求） |
| `timeout` | int | `600` | 请求超时时间（秒） |
| `response_format_b64_json` | bool | `true` | 请求返回 Base64 图片（建议开启） |
| `max_input_images` | int | `4` | 最多输入参考图数量 |
| `save_outputs` | bool | `true` | 保存生成结果到本地 |

## 安装

1. 将本插件目录放入 AstrBot 的 `data/plugins/` 目录下
2. 在 AstrBot WebUI 中启用插件并配置 API Key 等参数
3. 确保已安装依赖：`pip install "httpx>=0.27.0"`

## 打包导入

可以使用仓库内脚本生成 WebUI 可导入的插件压缩包：

```bash
python scripts/package_plugin.py
```

默认输出：

```text
dist/astrbot_plugin_gpt_image2_233.zip
```

然后在 AstrBot WebUI 的插件页面中上传该 zip 文件安装。

打包脚本默认会将插件文件放在 `astrbot_plugin_gpt_image2_233/`
顶层目录下，并显式写入该目录条目，以兼容 AstrBot v4.24.2
的 WebUI 上传安装逻辑。

打包脚本只包含插件交付所需文件，不会包含 `.opencode/`、`tmp/`、`astrbot_main/`、`.git/` 等本地开发材料。

## 开发

```bash
# 安装依赖
uv sync
# 或
pip install -r requirements.txt
```

## 依赖

- `httpx>=0.27.0` — 异步 HTTP 客户端

## 注意事项

- 请确保 `base_url` 指向正确的 OpenAI 兼容 API 端点，不要包含 `images/generations` 等路径后缀
- 建议开启 `response_format_b64_json`，避免图片 URL 过期导致发送失败
- API Key 仅在运行时使用，不会在日志或错误消息中泄漏

## 许可证

MIT
