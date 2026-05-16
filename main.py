"""GPT Image2 AstrBot 插件

命令组 /image2：
  /image2 draw <prompt>  文生图
  /image2 edit <prompt>  从消息/引用消息提取图片并编辑
  /image2 help           展示用法和配置摘要
"""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
import traceback

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image as CompImage, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .client import GPTImageClient, ImageParams, ImageResult
from .image_utils import (
    ensure_output_dir,
    extract_images_from_event,
    image_to_data_url,
    image_to_file_path,
    save_base64_to_file,
)


@register(
    "gpt_image2",
    "233",
    "通过 OpenAI 兼容 API 调用 GPT Image2 完成图片生成与编辑",
    "0.0.1",
)
class GPTImage2Plugin(Star):
    def __init__(self, context: Context, config: dict) -> None:
        super().__init__(context)
        self.config = config
        self.plugin_name = "astrbot_plugin_gpt_image2"
        self._output_dir: str | None = None

    # ── 工具方法 ────────────────────────────────────────────────

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((perf_counter() - start) * 1000)

    @staticmethod
    def _file_size(path: str) -> int | None:
        try:
            return Path(path).stat().st_size
        except OSError:
            return None

    def _event_context(self, event: AstrMessageEvent) -> str:
        """返回不含消息正文的事件上下文，便于日志排查。"""
        return (
            f"platform={event.get_platform_name()} "
            f"platform_id={event.get_platform_id()} "
            f"session={event.get_session_id()} "
            f"group={event.get_group_id() or '-'} "
            f"sender={event.get_sender_id() or '-'}"
        )

    @staticmethod
    def _params_summary(params: ImageParams) -> str:
        return (
            f"size={params.size} quality={params.quality} "
            f"format={params.output_format} n={params.n} "
            f"compression={params.output_compression or '-'}"
        )

    async def _send_processing_ack(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        action: str,
    ) -> None:
        """主动发送处理中提示，避免在 handler 中途 yield 打断处理流程。"""
        try:
            await event.send(MessageChain().message(text))
            logger.debug(
                "[GPTImage2] processing acknowledgement sent "
                f"action={action} {self._event_context(event)}"
            )
        except Exception as e:
            logger.warning(
                "[GPTImage2] processing acknowledgement failed "
                f"action={action} {self._event_context(event)} "
                f"error={type(e).__name__}: {e}"
            )

    def _get_client(self) -> GPTImageClient:
        """从配置创建 API 客户端"""
        api_key = self.config.get("api_key", "")
        if not api_key:
            raise ValueError("未配置 API Key。请在插件设置中填入 API Key。")

        return GPTImageClient(
            api_key=api_key,
            base_url=self.config.get("base_url", "https://api.openai.com/v1"),
            model=self.config.get("model", "gpt-image-2"),
            responses_model=self.config.get("responses_model", "gpt-5.5"),
            timeout=self.config.get("timeout", 600),
            response_format_b64_json=self.config.get("response_format_b64_json", True),
        )

    def _get_params(self) -> ImageParams:
        """从配置创建参数模型"""
        return ImageParams(
            size=self.config.get("size", "auto"),
            quality=self.config.get("quality", "auto"),
            output_format=self.config.get("output_format", "png"),
            moderation=self.config.get("moderation", "auto"),
            n=self.config.get("n", 1),
            output_compression=self._get_output_compression(),
        )

    def _get_output_compression(self) -> int | None:
        """读取输出压缩配置；0 或空值表示不发送。"""
        value = self.config.get("output_compression", 0)
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _get_output_dir(self) -> str:
        """获取输出目录（惰性创建）"""
        if self._output_dir is None:
            plugin_name = getattr(self, "name", self.plugin_name)
            plugin_data_dir = (
                Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
            )
            self._output_dir = ensure_output_dir(str(plugin_data_dir))
            logger.debug(f"[GPTImage2] output directory ready path={self._output_dir}")
        return self._output_dir

    def _extract_prompt(self, event: AstrMessageEvent, subcommand: str) -> str:
        """从原始消息中提取子命令后的完整提示词。"""
        message = event.message_str.strip()
        prefixes = (
            f"/image2 {subcommand}",
            f"image2 {subcommand}",
        )
        for prefix in prefixes:
            if message.startswith(prefix):
                return message[len(prefix) :].strip()

        parts = message.split(maxsplit=2)
        if len(parts) >= 3 and parts[-2] == subcommand:
            return parts[-1].strip()
        return ""

    def _save_config(self) -> bool:
        """保存插件配置。"""
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()
            return True
        return False

    def _save_results(
        self,
        results: list[ImageResult],
        params: ImageParams,
    ) -> list[dict]:
        """保存结果并返回 (filepath_or_url, revised_prompt) 列表

        如果 save_outputs 为 True 且结果包含 b64_json，先保存到本地。
        URL 结果直接返回 URL。
        """
        save = bool(self.config.get("save_outputs", True))
        output_dir = self._get_output_dir() if save else None
        out_fmt = params.output_format

        saved: list[dict] = []
        for r in results:
            item: dict[str, str | None] = {
                "path": None,
                "url": None,
                "revised_prompt": r.revised_prompt,
            }
            if r.b64_json and save and output_dir:
                filepath = save_base64_to_file(r.b64_json, output_dir, out_fmt)
                item["path"] = filepath
                logger.debug(
                    "[GPTImage2] saved generated image "
                    f"index={len(saved) + 1} path={filepath} "
                    f"bytes={self._file_size(filepath)} format={out_fmt}"
                )
            elif r.b64_json and not save:
                item["b64_json"] = r.b64_json
                logger.debug(
                    "[GPTImage2] kept generated image in memory "
                    f"index={len(saved) + 1} b64_chars={len(r.b64_json)}"
                )
            elif r.url:
                item["url"] = r.url
                logger.debug(
                    "[GPTImage2] using generated image URL "
                    f"index={len(saved) + 1} url_len={len(r.url)}"
                )
            saved.append(item)
        return saved

    # ── 命令组 ──────────────────────────────────────────────────

    @filter.command_group("image2")
    def image2() -> None:
        """GPT Image2 绘图命令组"""
        pass

    @image2.command("help")
    async def help(self, event: AstrMessageEvent):
        """展示用法和配置摘要"""
        lines = [
            "📋 GPT Image2 插件使用说明",
            "",
            "命令：",
            "  /image2 draw <提示词>    文生图",
            "  /image2 edit <提示词>    编辑图片（附带图片或引用图片消息）",
            "  /image2 mode [模式]      查看/切换 API 模式（管理员）",
            "  /image2 help             显示本帮助",
            "",
            "当前配置：",
        ]

        cfg = self.config
        api_key_set = "已设置" if cfg.get("api_key") else "未设置"
        lines.append(f"  API Key         : {api_key_set}")
        lines.append(f"  Base URL        : {cfg.get('base_url', '未设置')}")
        lines.append(f"  API 模式        : {cfg.get('api_mode', 'images')}")
        lines.append(f"  Images 模型     : {cfg.get('model', 'gpt-image-2')}")
        lines.append(f"  Responses 模型  : {cfg.get('responses_model', 'gpt-5.5')}")
        lines.append(f"  图片尺寸        : {cfg.get('size', 'auto')}")
        lines.append(f"  图片质量        : {cfg.get('quality', 'auto')}")
        lines.append(f"  输出格式        : {cfg.get('output_format', 'png')}")
        lines.append(f"  生成数量 n      : {cfg.get('n', 1)}")
        lines.append(
            f"  保存输出        : {'是' if cfg.get('save_outputs', True) else '否'}"
        )

        yield event.plain_result("\n".join(lines))

    @image2.command("mode")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mode(self, event: AstrMessageEvent, mode: str | None = None):
        """查看或切换 API 模式（管理员）"""
        current_mode = self.config.get("api_mode", "images")
        logger.info(
            "[GPTImage2] mode command received "
            f"{self._event_context(event)} current_mode={current_mode} "
            f"requested_mode={mode or '-'}"
        )
        if mode is None or not str(mode).strip():
            yield event.plain_result(
                "当前 API 模式："
                f"{current_mode}\n"
                "可用模式：images / responses\n"
                "用法：/image2 mode <images|responses>"
            )
            return

        next_mode = str(mode).strip().lower()
        if next_mode not in {"images", "responses"}:
            yield event.plain_result(
                "API 模式无效。可用模式：images / responses\n"
                "用法：/image2 mode <images|responses>"
            )
            return

        if next_mode == current_mode:
            yield event.plain_result(f"API 模式已经是：{current_mode}")
            return

        self.config["api_mode"] = next_mode
        saved = self._save_config()
        logger.info(
            "[GPTImage2] API mode switched "
            f"{self._event_context(event)} from={current_mode} to={next_mode} saved={saved}"
        )
        suffix = "已保存到插件配置。" if saved else "但当前配置对象不支持自动保存。"
        yield event.plain_result(
            f"API 模式已从 {current_mode} 切换为 {next_mode}，{suffix}"
        )

    @image2.command("draw")
    async def draw(self, event: AstrMessageEvent):
        """文生图"""
        started = perf_counter()
        prompt = self._extract_prompt(event, "draw")
        if not prompt or not prompt.strip():
            logger.info(
                f"[GPTImage2] draw rejected empty prompt {self._event_context(event)}"
            )
            yield event.plain_result(
                "请提供图片描述提示词。用法：/image2 draw <提示词>"
            )
            return

        prompt = prompt.strip()

        try:
            client = self._get_client()
        except ValueError as e:
            yield event.plain_result(str(e))
            return

        params = self._get_params()
        api_mode = self.config.get("api_mode", "images")
        logger.info(
            "[GPTImage2] draw start "
            f"{self._event_context(event)} mode={api_mode} prompt_len={len(prompt)} "
            f"{self._params_summary(params)} save_outputs={self.config.get('save_outputs', True)}"
        )
        await self._send_processing_ack(
            event,
            f"✅ 已收到文生图请求，正在使用 {api_mode} 模式生成图片，请稍候…",
            action="draw",
        )

        try:
            if api_mode == "images":
                results = await client.generate_images_api(prompt, params)
            else:
                results = await client.generate_responses_api(prompt, params)
        except RuntimeError as e:
            logger.warning(
                "[GPTImage2] draw API failed "
                f"{self._event_context(event)} mode={api_mode} "
                f"elapsed_ms={self._elapsed_ms(started)} error={e}"
            )
            yield event.plain_result(f"GPT Image2 调用失败：{e}")
            return
        except Exception as e:
            logger.error(
                "[GPTImage2] draw unexpected error "
                f"{self._event_context(event)} mode={api_mode} "
                f"elapsed_ms={self._elapsed_ms(started)} error={type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            yield event.plain_result(f"GPT Image2 调用失败：{e}")
            return

        logger.info(
            "[GPTImage2] draw API success "
            f"{self._event_context(event)} mode={api_mode} results={len(results)} "
            f"elapsed_ms={self._elapsed_ms(started)}"
        )

        chain = self._build_image_chain(results, params)
        if chain:
            logger.info(
                "[GPTImage2] draw reply ready "
                f"{self._event_context(event)} chain_items={len(chain)} "
                f"total_elapsed_ms={self._elapsed_ms(started)}"
            )
            yield event.chain_result(chain)
        else:
            logger.warning(
                "[GPTImage2] draw returned no displayable images "
                f"{self._event_context(event)} results={len(results)}"
            )
            yield event.plain_result("GPT Image2 未返回任何可显示的图片。")

    @image2.command("edit")
    async def edit(self, event: AstrMessageEvent):
        """从当前消息或引用消息提取图片后编辑"""
        started = perf_counter()
        prompt = self._extract_prompt(event, "edit")
        if not prompt or not prompt.strip():
            logger.info(
                f"[GPTImage2] edit rejected empty prompt {self._event_context(event)}"
            )
            yield event.plain_result(
                "请提供编辑提示词。用法：/image2 edit <提示词>"
                "（请附带图片或引用包含图片的消息）"
            )
            return

        prompt = prompt.strip()
        messages = event.get_messages()
        max_input = int(self.config.get("max_input_images", 4))
        images = extract_images_from_event(messages, max_input)
        logger.info(
            "[GPTImage2] edit images extracted "
            f"{self._event_context(event)} prompt_len={len(prompt)} "
            f"message_components={len(messages)} input_images={len(images)} "
            f"max_input_images={max_input}"
        )

        if not images:
            logger.warning(
                "[GPTImage2] edit rejected no input images "
                f"{self._event_context(event)} message_components={len(messages)}"
            )
            yield event.plain_result(
                "没有找到可编辑的图片。请附带图片，"
                "或引用一条包含图片的消息后使用 /image2 edit <提示词>。"
            )
            return

        try:
            client = self._get_client()
        except ValueError as e:
            yield event.plain_result(str(e))
            return

        params = self._get_params()
        api_mode = self.config.get("api_mode", "images")
        logger.info(
            "[GPTImage2] edit start "
            f"{self._event_context(event)} mode={api_mode} prompt_len={len(prompt)} "
            f"input_images={len(images)} {self._params_summary(params)} "
            f"save_outputs={self.config.get('save_outputs', True)}"
        )
        await self._send_processing_ack(
            event,
            f"✅ 已收到图像编辑请求，已识别 {len(images)} 张参考图，"
            f"正在使用 {api_mode} 模式处理，请稍候…",
            action="edit",
        )

        try:
            if api_mode == "images":
                # Images API 编辑：使用本地文件路径
                image_paths: list[str] = []
                for idx, img in enumerate(images, start=1):
                    path = await image_to_file_path(img)
                    image_paths.append(path)
                    logger.debug(
                        "[GPTImage2] edit input image converted to file "
                        f"index={idx} path={path} bytes={self._file_size(path)}"
                    )
                results = await client.edit_images_api(prompt, image_paths, params)
            else:
                # Responses API 编辑：使用 data URL
                data_urls: list[str] = []
                for idx, img in enumerate(images, start=1):
                    data_url = await image_to_data_url(img)
                    data_urls.append(data_url)
                    logger.debug(
                        "[GPTImage2] edit input image converted to data URL "
                        f"index={idx} chars={len(data_url)}"
                    )
                results = await client.edit_responses_api(prompt, data_urls, params)
        except RuntimeError as e:
            logger.warning(
                "[GPTImage2] edit API failed "
                f"{self._event_context(event)} mode={api_mode} "
                f"elapsed_ms={self._elapsed_ms(started)} error={e}"
            )
            yield event.plain_result(f"GPT Image2 调用失败：{e}")
            return
        except Exception as e:
            logger.error(
                "[GPTImage2] edit unexpected error "
                f"{self._event_context(event)} mode={api_mode} "
                f"elapsed_ms={self._elapsed_ms(started)} error={type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            yield event.plain_result(f"GPT Image2 调用失败：{e}")
            return

        logger.info(
            "[GPTImage2] edit API success "
            f"{self._event_context(event)} mode={api_mode} results={len(results)} "
            f"elapsed_ms={self._elapsed_ms(started)}"
        )

        chain = self._build_image_chain(results, params)
        if chain:
            logger.info(
                "[GPTImage2] edit reply ready "
                f"{self._event_context(event)} chain_items={len(chain)} "
                f"total_elapsed_ms={self._elapsed_ms(started)}"
            )
            yield event.chain_result(chain)
        else:
            logger.warning(
                "[GPTImage2] edit returned no displayable images "
                f"{self._event_context(event)} results={len(results)}"
            )
            yield event.plain_result("GPT Image2 未返回任何可显示的图片。")

    # ── 回复构建 ────────────────────────────────────────────────

    def _build_image_chain(
        self,
        results: list[ImageResult],
        params: ImageParams,
    ) -> list:
        """构建包含图片和 revised_prompt 的消息链"""
        if not results:
            return []

        saved = self._save_results(results, params)
        chain: list = []
        logger.debug(
            "[GPTImage2] building reply chain "
            f"saved_items={len(saved)} result_items={len(results)}"
        )

        for idx, item in enumerate(saved):
            rp = item.get("revised_prompt")
            if rp:
                chain.append(Plain(f"📝 改写提示词：{rp}\n"))

            path = item.get("path")
            url = item.get("url")
            b64 = item.get("b64_json")

            if path:
                chain.append(CompImage.fromFileSystem(path))
            elif url:
                chain.append(CompImage.fromURL(url))
            elif b64:
                chain.append(CompImage.fromBase64(b64))
            else:
                chain.append(Plain(f"[第 {idx + 1} 张图片：无法获取]"))
                logger.warning(
                    "[GPTImage2] reply item has no displayable image "
                    f"index={idx + 1} keys={list(item.keys())}"
                )
                continue

        logger.debug(f"[GPTImage2] reply chain built chain_items={len(chain)}")
        return chain
