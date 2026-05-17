"""GPT Image2 AstrBot 插件

命令组 /image2：
  /image2 draw <prompt>  文生图
  /image2 edit <prompt>  从消息/引用消息提取图片并编辑
  /image2 plan           进入 Plan 多轮会话，辅助优化生图提示词
  /image2 plan confirm   在 Plan 中确认生成图片
  /image2 plan quit      退出 Plan 会话
  /image2 mode [模式]    查看/切换 API 模式（管理员）
  /image2 help           展示用法和配置摘要
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import perf_counter
import traceback

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image as CompImage, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    USER_SESSIONS,
    session_waiter,
)

from .client import GPTImageClient, ImageParams, ImageResult
from .image_utils import (
    ensure_output_dir,
    extract_images_from_event,
    image_to_data_url,
    image_to_file_path,
    save_base64_to_file,
)
from .plan import (
    PLAN_SYSTEM_PROMPT,
    PlanConfig,
    PlanSession,
    remove_final_prompt_section,
    parse_final_prompt,
)


class SenderSessionFilter(SessionFilter):
    """按会话来源 + 发送者隔离 Plan 会话，避免群聊串扰。"""

    def filter(self, event: AstrMessageEvent) -> str:
        sender_id = event.get_sender_id() or "-"
        return f"{event.unified_msg_origin}:sender:{sender_id}"


@register(
    "gpt_image2",
    "233",
    "通过 OpenAI 兼容 API 调用 GPT Image2 完成图片生成与编辑",
    "0.1.1",
)
class GPTImage2Plugin(Star):
    PLAN_WAITER_TIMEOUT_GRACE = 10

    def __init__(self, context: Context, config: dict) -> None:
        super().__init__(context)
        self.config = config
        self.plugin_name = "astrbot_plugin_gpt_image2"
        self._output_dir: str | None = None
        self._plan_sessions: dict[str, PlanSession] = {}

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
    def _plan_session_id(event: AstrMessageEvent) -> str:
        """Plan 会话 ID：按会话来源和发送者隔离，避免群聊串扰。"""
        sender_id = event.get_sender_id() or "-"
        return f"{event.unified_msg_origin}:sender:{sender_id}"

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
            await self._send_text(event, text, action=action)
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

    def _render_text_as_image_enabled(self) -> bool:
        """是否将插件文本回复渲染为图片，减少群聊刷屏。"""
        return bool(self.config.get("render_text_as_image", True))

    async def _build_text_chain(
        self,
        text: str,
        *,
        action: str,
        prefer_image: bool | None = None,
    ) -> MessageChain:
        """构建文本回复消息链；默认使用 AstrBot 文转图，失败则回退纯文本。"""
        use_image = self._render_text_as_image_enabled()
        if prefer_image is not None:
            use_image = prefer_image

        if use_image:
            try:
                image_ref = await self.text_to_image(text, return_url=True)
                if image_ref:
                    image_ref = str(image_ref)
                    if image_ref.startswith("http"):
                        return MessageChain(chain=[CompImage.fromURL(image_ref)])
                    return MessageChain(chain=[CompImage.fromFileSystem(image_ref)])
            except Exception as e:
                logger.warning(
                    "[GPTImage2] text-to-image failed, fallback to plain text "
                    f"action={action} error={type(e).__name__}: {e}"
                )

        return MessageChain().message(text)

    async def _send_text(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        action: str,
        prefer_image: bool | None = None,
    ) -> None:
        """发送文本类回复；默认文转图，失败回退文字。"""
        chain = await self._build_text_chain(
            text,
            action=action,
            prefer_image=prefer_image,
        )
        await event.send(chain)

    async def _text_result(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        action: str,
        prefer_image: bool | None = None,
    ):
        """构建可 yield 的文本回复结果；默认文转图，失败回退文字。"""
        chain = await self._build_text_chain(
            text,
            action=action,
            prefer_image=prefer_image,
        )
        return event.chain_result(chain.chain)

    async def _send_proactive_message(
        self,
        session_origin: str,
        text: str,
        *,
        action: str,
    ) -> bool:
        """主动向会话发送消息，用于超时等已脱离原始请求的场景。"""
        try:
            chain = await self._build_text_chain(text, action=action)
            sent = await self.context.send_message(
                session_origin,
                chain,
            )
            logger.debug(
                "[GPTImage2] proactive message sent "
                f"action={action} session_origin={session_origin} sent={sent}"
            )
            return bool(sent)
        except Exception as e:
            logger.warning(
                "[GPTImage2] proactive message failed "
                f"action={action} session_origin={session_origin} "
                f"error={type(e).__name__}: {e}"
            )
            return False

    def _get_client(self) -> GPTImageClient:
        """从配置创建图片 API 客户端"""
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

    # ── Plan 模式工具方法 ──────────────────────────────────────

    def _get_plan_config(self) -> PlanConfig:
        """从全局配置提取 Plan 配置。"""
        return PlanConfig.from_config(self.config)

    def _get_plan_client(self, plan_config: PlanConfig) -> GPTImageClient:
        """创建 Plan 模式专用的 API 客户端（用于 /responses）。

        当 plan_use_custom_api=true 时使用独立 api_key/base_url，
        缺失则 fallback 到全局配置。
        """
        if plan_config.use_custom_api:
            api_key = plan_config.api_key or self.config.get("api_key", "")
            base_url = plan_config.base_url or self.config.get(
                "base_url", "https://api.openai.com/v1"
            )
        else:
            api_key = self.config.get("api_key", "")
            base_url = self.config.get("base_url", "https://api.openai.com/v1")

        if not api_key:
            raise ValueError("未配置 API Key。请在插件设置中填入 API Key。")

        return GPTImageClient(
            api_key=api_key,
            base_url=base_url,
            model=self.config.get("model", "gpt-image-2"),
            responses_model=self.config.get("responses_model", "gpt-5.5"),
            timeout=self.config.get("timeout", 600),
            response_format_b64_json=self.config.get("response_format_b64_json", True),
        )

    def _cleanup_plan(self, session_id: str) -> None:
        """清理指定会话的 Plan 状态。"""
        old = self._plan_sessions.pop(session_id, None)
        if old is not None:
            current_task = asyncio.current_task()
            if (
                old.timeout_task
                and not old.timeout_task.done()
                and old.timeout_task is not current_task
            ):
                old.timeout_task.cancel()
            logger.debug(
                f"[GPTImage2] plan session cleaned up session={session_id} "
                f"rounds={old.round_count}"
            )

    @staticmethod
    def _waiter_timeout(timeout: int) -> int:
        """session_waiter 超时略晚于 watchdog，避免抢先吞掉主动通知。"""
        return timeout + GPTImage2Plugin.PLAN_WAITER_TIMEOUT_GRACE

    @staticmethod
    def _stop_active_plan_waiter(session_id: str) -> bool:
        """停止 session_waiter，释放等待中的 Plan handler。"""
        waiter = USER_SESSIONS.get(session_id)
        if waiter is None:
            return False
        controller = getattr(waiter, "session_controller", None)
        if controller is None:
            return False
        controller.stop()
        return True

    def _reset_plan_timeout_watchdog(
        self,
        session_id: str,
        session_origin: str,
        timeout: int,
    ) -> None:
        """重置 Plan 空闲超时 watchdog，主动通知不依赖 session_waiter 返回。"""
        session = self._plan_sessions.get(session_id)
        if session is None:
            return

        if session.timeout_task and not session.timeout_task.done():
            session.timeout_task.cancel()

        session.timeout_generation += 1
        generation = session.timeout_generation

        async def _watchdog() -> None:
            try:
                logger.info(
                    "[GPTImage2] plan timeout watchdog armed "
                    f"session={session_id} generation={generation} timeout={timeout}s"
                )
                await asyncio.sleep(timeout)
                current = self._plan_sessions.get(session_id)
                if current is None or current.timeout_generation != generation:
                    return

                logger.info(
                    "[GPTImage2] plan timeout watchdog fired "
                    f"session={session_id} generation={generation} timeout={timeout}s"
                )
                sent = await self._send_proactive_message(
                    session_origin,
                    "⌛ Plan 会话等待超时，已自动退出。",
                    action="plan-timeout-watchdog",
                )
                if not sent:
                    logger.warning(
                        "[GPTImage2] plan timeout watchdog notification not sent "
                        f"session={session_id} session_origin={session_origin}"
                    )
                stopped = self._stop_active_plan_waiter(session_id)
                logger.debug(
                    "[GPTImage2] plan timeout watchdog stopped waiter "
                    f"session={session_id} stopped={stopped}"
                )
                self._cleanup_plan(session_id)
            except asyncio.CancelledError:
                logger.debug(
                    "[GPTImage2] plan timeout watchdog cancelled "
                    f"session={session_id} generation={generation}"
                )
            except Exception as e:
                logger.error(
                    "[GPTImage2] plan timeout watchdog error "
                    f"session={session_id} error={type(e).__name__}: {e}\n"
                    f"{traceback.format_exc()}"
                )

        session.timeout_task = asyncio.create_task(_watchdog())

    async def terminate(self) -> None:
        """插件卸载/重载时清理 Plan 会话与 watchdog。"""
        for session_id in list(self._plan_sessions):
            self._cleanup_plan(session_id)

    @staticmethod
    def _has_active_plan_waiter(session_id: str) -> bool:
        """判断 session_waiter 中是否仍有活跃 Plan 等待器。"""
        return session_id in USER_SESSIONS

    def _get_plan_processing_timeout(self, plan_config: PlanConfig) -> int:
        """Plan 处理阶段超时，覆盖模型思考和生图耗时。"""
        try:
            api_timeout = int(self.config.get("timeout", 600))
        except (TypeError, ValueError):
            api_timeout = 600
        return max(plan_config.timeout, api_timeout + 60)

    def _get_max_input_images(self) -> int:
        """读取最多参考图数量。"""
        try:
            return max(1, int(self.config.get("max_input_images", 4)))
        except (TypeError, ValueError):
            return 4

    @staticmethod
    def _build_plan_user_content(text: str, image_data_urls: list[str]) -> str | list:
        """构建 Responses API 用户输入内容。"""
        if not image_data_urls:
            return text

        content: list[dict[str, str]] = [{"type": "input_text", "text": text}]
        for data_url in image_data_urls:
            content.append({"type": "input_image", "image_url": data_url})
        return content

    async def _collect_plan_reference_images(
        self,
        event: AstrMessageEvent,
        session: PlanSession,
    ) -> list[str]:
        """从 Plan 消息中收集参考图，返回本轮新增 data URLs。"""
        remaining = self._get_max_input_images() - len(session.reference_data_urls)
        if remaining <= 0:
            return []

        images = extract_images_from_event(event.get_messages(), remaining)
        if not images:
            return []

        data_urls: list[str] = []
        for idx, image in enumerate(images, start=1):
            try:
                data_url = await image_to_data_url(image)
            except Exception as e:
                logger.warning(
                    "[GPTImage2] plan reference image conversion failed "
                    f"{self._event_context(event)} index={idx} "
                    f"error={type(e).__name__}: {e}"
                )
                continue
            session.reference_images.append(image)
            session.reference_data_urls.append(data_url)
            data_urls.append(data_url)
            logger.debug(
                "[GPTImage2] plan reference image collected "
                f"{self._event_context(event)} index={idx} chars={len(data_url)} "
                f"total={len(session.reference_data_urls)}"
            )
        return data_urls

    async def _generate_draw_chain(
        self,
        event: AstrMessageEvent,
        prompt: str,
        action: str = "draw",
        reference_images: list | None = None,
        reference_data_urls: list[str] | None = None,
        send_ack: bool = True,
    ) -> list:
        """生成图片并返回消息组件链。

        封装：客户端创建 → 处理中 ACK → API 调用 → 链构建。
        失败时通过 event.send() 发送错误提示，返回空列表。

        Args:
            event: 用于回应的消息事件（draw handler 或 waiter 的 next_event）
            prompt: 生图提示词
            action: 日志标识，'draw' 或 'plan'
            reference_images: 参考图 Image 组件，存在时走编辑/参考图生成路径
            reference_data_urls: 参考图 data URLs，Responses API 使用
            send_ack: 是否在 API 调用前主动发送处理中提示

        Returns:
            消息组件列表（可空），供 caller yield chain_result 或 event.send
        """
        try:
            client = self._get_client()
        except ValueError as e:
            await self._send_text(event, str(e), action=f"{action}-config-error")
            return []

        params = self._get_params()
        api_mode = self.config.get("api_mode", "images")
        reference_images = reference_images or []
        reference_data_urls = reference_data_urls or []
        reference_count = max(len(reference_images), len(reference_data_urls))

        if send_ack:
            if reference_count:
                ack_text = (
                    f"✅ 已识别 {reference_count} 张参考图，正在使用 {api_mode} "
                    "模式生成图片，请稍候…"
                )
            else:
                ack_text = f"✅ 正在使用 {api_mode} 模式生成图片，请稍候…"

            await self._send_processing_ack(
                event,
                ack_text,
                action=action,
            )

        started = perf_counter()
        try:
            if reference_count and api_mode == "images":
                image_paths: list[str] = []
                if reference_images:
                    for idx, image in enumerate(reference_images, start=1):
                        path = await image_to_file_path(image)
                        image_paths.append(path)
                        logger.debug(
                            "[GPTImage2] reference image converted to file "
                            f"action={action} index={idx} path={path} "
                            f"bytes={self._file_size(path)}"
                        )
                else:
                    output_dir = self._get_output_dir()
                    for idx, data_url in enumerate(reference_data_urls, start=1):
                        path = save_base64_to_file(data_url, output_dir, "png")
                        image_paths.append(path)
                        logger.debug(
                            "[GPTImage2] reference data URL saved to file "
                            f"action={action} index={idx} path={path} "
                            f"bytes={self._file_size(path)}"
                        )
                results = await client.edit_images_api(prompt, image_paths, params)
            elif reference_count:
                data_urls = list(reference_data_urls)
                if not data_urls:
                    for idx, image in enumerate(reference_images, start=1):
                        data_url = await image_to_data_url(image)
                        data_urls.append(data_url)
                        logger.debug(
                            "[GPTImage2] reference image converted to data URL "
                            f"action={action} index={idx} chars={len(data_url)}"
                        )
                results = await client.edit_responses_api(prompt, data_urls, params)
            elif api_mode == "images":
                results = await client.generate_images_api(prompt, params)
            else:
                results = await client.generate_responses_api(prompt, params)
        except RuntimeError as e:
            logger.warning(f"[GPTImage2] draw API failed action={action} error={e}")
            await self._send_text(
                event,
                f"GPT Image2 调用失败：{e}",
                action=f"{action}-api-error",
            )
            return []
        except Exception as e:
            logger.error(
                "[GPTImage2] draw unexpected error "
                f"action={action} error={type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            await self._send_text(
                event,
                f"GPT Image2 调用失败：{e}",
                action=f"{action}-unexpected-error",
            )
            return []

        logger.info(
            "[GPTImage2] draw API success "
            f"action={action} results={len(results)} elapsed_ms={self._elapsed_ms(started)}"
        )

        chain = self._build_image_chain(results, params)
        if not chain:
            await self._send_text(
                event,
                "GPT Image2 未返回任何可显示的图片。",
                action=f"{action}-empty-result",
            )
        return chain

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
            "  /image2 plan             进入 Plan 多轮图文会话，辅助优化生图提示词",
            "  /image2 plan confirm     在 Plan 中确认生成（自动带参考图）",
            "  /image2 plan quit        退出 Plan 会话（cancel 也可用）",
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
        lines.append(f"  Plan 模型       : {cfg.get('plan_model', 'gpt-5.4')}")
        lines.append(f"  Plan 空闲超时   : {cfg.get('plan_timeout', 300)} 秒")
        lines.append(f"  图片尺寸        : {cfg.get('size', 'auto')}")
        lines.append(f"  图片质量        : {cfg.get('quality', 'auto')}")
        lines.append(f"  输出格式        : {cfg.get('output_format', 'png')}")
        lines.append(f"  生成数量 n      : {cfg.get('n', 1)}")
        lines.append(
            "  文本回复转图片  : "
            f"{'是' if self._render_text_as_image_enabled() else '否'}"
        )
        lines.append(
            f"  保存输出        : {'是' if cfg.get('save_outputs', True) else '否'}"
        )

        yield await self._text_result(event, "\n".join(lines), action="help")

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
            yield await self._text_result(
                event,
                "当前 API 模式："
                f"{current_mode}\n"
                "可用模式：images / responses\n"
                "用法：/image2 mode <images|responses>",
                action="mode-help",
            )
            return

        next_mode = str(mode).strip().lower()
        if next_mode not in {"images", "responses"}:
            yield await self._text_result(
                event,
                "API 模式无效。可用模式：images / responses\n"
                "用法：/image2 mode <images|responses>",
                action="mode-invalid",
            )
            return

        if next_mode == current_mode:
            yield await self._text_result(
                event,
                f"API 模式已经是：{current_mode}",
                action="mode-unchanged",
            )
            return

        self.config["api_mode"] = next_mode
        saved = self._save_config()
        logger.info(
            "[GPTImage2] API mode switched "
            f"{self._event_context(event)} from={current_mode} to={next_mode} saved={saved}"
        )
        suffix = "已保存到插件配置。" if saved else "但当前配置对象不支持自动保存。"
        yield await self._text_result(
            event,
            f"API 模式已从 {current_mode} 切换为 {next_mode}，{suffix}",
            action="mode-switched",
        )

    # ── Plan 模式 ────────────────────────────────────────────

    @image2.command("plan")
    async def plan(self, event: AstrMessageEvent, action: str | None = None):
        """Plan 多轮图文会话：辅助用户优化生图提示词"""
        action = str(action or "").strip().lower()
        session_id = self._plan_session_id(event)

        if action == "confirm":
            if self._has_active_plan_waiter(session_id):
                return
            if session_id in self._plan_sessions:
                self._cleanup_plan(session_id)
                yield await self._text_result(
                    event,
                    "Plan 会话已失效，请重新使用 /image2 plan 进入。",
                    action="plan-confirm-stale",
                )
            else:
                yield await self._text_result(
                    event,
                    "当前没有进行中的 Plan 会话。请先使用 /image2 plan 进入。",
                    action="plan-confirm-missing",
                )
            return

        if action in {"quit", "cancel"}:
            if self._has_active_plan_waiter(session_id):
                return
            had_session = session_id in self._plan_sessions
            self._cleanup_plan(session_id)
            logger.info(
                "[GPTImage2] plan quit external trigger "
                f"{self._event_context(event)} action={action} had_session={had_session}"
            )
            yield await self._text_result(
                event,
                "已尝试退出 Plan 会话。如果没有响应中的 Plan 会话，则当前无需操作。",
                action="plan-quit-external",
            )
            return

        if action:
            yield await self._text_result(
                event,
                "Plan 子命令无效。用法：/image2 plan、/image2 plan confirm、"
                "/image2 plan quit",
                action="plan-invalid-action",
            )
            return

        if not bool(self.config.get("plan_enabled", True)):
            yield await self._text_result(
                event,
                "Plan 模式当前未启用。",
                action="plan-disabled",
            )
            return

        plan_config = self._get_plan_config()
        processing_timeout = self._get_plan_processing_timeout(plan_config)
        owner_sender_id = event.get_sender_id() or ""

        # 防止同一会话重复进入
        if session_id in self._plan_sessions:
            yield await self._text_result(
                event,
                "当前会话已有进行中的 Plan 会话。"
                "请先使用 /image2 plan confirm 或 /image2 plan quit 结束当前会话。",
                action="plan-duplicate",
            )
            return

        # 提前验证 Plan 客户端配置（快速失败）
        try:
            plan_client = self._get_plan_client(plan_config)
        except ValueError as e:
            yield await self._text_result(event, str(e), action="plan-config-error")
            return

        # 创建会话，并记录随 plan 命令附带/引用的参考图。
        session = PlanSession(owner_sender_id=owner_sender_id)
        self._plan_sessions[session_id] = session
        await self._collect_plan_reference_images(event, session)
        self._reset_plan_timeout_watchdog(
            session_id,
            event.unified_msg_origin,
            plan_config.timeout,
        )

        logger.info(
            "[GPTImage2] plan start "
            f"{self._event_context(event)} timeout={plan_config.timeout}s "
            f"processing_timeout={processing_timeout}s "
            f"max_rounds={plan_config.max_rounds} model={plan_config.model}"
        )

        await self._send_text(
            event,
            "🧠 已进入 Plan 模式。\n"
            "请直接发送文字或参考图描述你想要的图像，我会用 Responses API 帮你优化提示词。\n"
            f"当前已记录 {len(session.reference_data_urls)} 张参考图。\n"
            f"空闲超过 {plan_config.timeout} 秒会自动退出 Plan 模式。\n"
            "发送 /image2 plan confirm 用当前提示词生成图片。\n"
            "发送 /image2 plan quit 退出。",
            action="plan-enter",
        )

        @session_waiter(
            timeout=self._waiter_timeout(plan_config.timeout),
            record_history_chains=False,
        )
        async def plan_waiter(
            controller: SessionController,
            next_event: AstrMessageEvent,
        ) -> None:
            text = next_event.message_str.strip()
            next_session_id = self._plan_session_id(next_event)
            if next_session_id != session_id:
                return

            # 收到同一 Plan 会话内的任何输入后，先延长 watchdog，避免处理阶段误报空闲超时。
            self._reset_plan_timeout_watchdog(
                session_id,
                next_event.unified_msg_origin,
                processing_timeout,
            )

            # ── 退出 ────────────────────────────────────────
            if text.lower() in {
                "/image2 plan quit",
                "image2 plan quit",
                "/image2 plan cancel",
                "image2 plan cancel",
            }:
                await self._send_text(
                    next_event,
                    "✅ 已退出 Plan 模式。",
                    action="plan-quit",
                )
                self._cleanup_plan(session_id)
                controller.stop()
                return

            # ── 确认生成 ────────────────────────────────────
            if text.lower() in {"/image2 plan confirm", "image2 plan confirm"}:
                session = self._plan_sessions.get(session_id)
                if session and session.final_prompt:
                    controller.keep(
                        timeout=processing_timeout,
                        reset_timeout=True,
                    )
                    self._reset_plan_timeout_watchdog(
                        session_id,
                        next_event.unified_msg_origin,
                        processing_timeout,
                    )
                    reference_count = max(
                        len(session.reference_images),
                        len(session.reference_data_urls),
                    )
                    api_mode = self.config.get("api_mode", "images")
                    if reference_count:
                        confirm_ack = (
                            "✅ 已确认 Plan 提示词，"
                            f"正在使用 {api_mode} 模式携带 {reference_count} 张参考图生成图片，"
                            "请稍候…"
                        )
                    else:
                        confirm_ack = (
                            "✅ 已确认 Plan 提示词，"
                            f"正在使用 {api_mode} 模式生成图片，请稍候…"
                        )
                    prompt_text = (
                        "🧾 将用于生成的完整提示词\n\n"
                        f"{session.final_prompt}\n\n"
                        "提示：中文文字内容会按原文保留，不强制翻译为英文。"
                    )
                    await self._send_text(
                        next_event,
                        prompt_text,
                        action="plan-final-prompt",
                        prefer_image=True,
                    )
                    await self._send_processing_ack(
                        next_event,
                        confirm_ack,
                        action="plan-confirm",
                    )
                    chain = await self._generate_draw_chain(
                        next_event,
                        session.final_prompt,
                        action="plan",
                        reference_images=session.reference_images,
                        reference_data_urls=session.reference_data_urls,
                        send_ack=False,
                    )
                    if chain:
                        await next_event.send(MessageChain(chain=chain))
                else:
                    await self._send_text(
                        next_event,
                        "还没有准备好的提示词。"
                        "请继续描述你的图像需求，"
                        "或发送 /image2 plan quit 退出。",
                        action="plan-confirm-no-prompt",
                    )
                    controller.keep(
                        timeout=self._waiter_timeout(plan_config.timeout),
                        reset_timeout=True,
                    )
                    self._reset_plan_timeout_watchdog(
                        session_id,
                        next_event.unified_msg_origin,
                        plan_config.timeout,
                    )
                    return
                self._cleanup_plan(session_id)
                controller.stop()
                return

            # ── 其他 /image2 命令：拦截提示 ─────────────────
            if text.lower().startswith("/image2") or text.lower().startswith("image2"):
                await self._send_text(
                    next_event,
                    "当前处于 Plan 会话中。"
                    "请先使用 /image2 plan confirm 生成图片"
                    "或 /image2 plan quit 退出。",
                    action="plan-command-blocked",
                )
                controller.keep(
                    timeout=self._waiter_timeout(plan_config.timeout),
                    reset_timeout=True,
                )
                self._reset_plan_timeout_watchdog(
                    session_id,
                    next_event.unified_msg_origin,
                    plan_config.timeout,
                )
                return

            # ── 普通文本/参考图：调用 Responses API 规划 ─────
            session = self._plan_sessions.get(session_id)
            if session is None:
                await self._send_text(
                    next_event,
                    "Plan 会话已失效，请重新使用 /image2 plan 进入。",
                    action="plan-session-missing",
                )
                controller.stop()
                return

            controller.keep(timeout=processing_timeout, reset_timeout=True)
            self._reset_plan_timeout_watchdog(
                session_id,
                next_event.unified_msg_origin,
                processing_timeout,
            )

            new_reference_urls = await self._collect_plan_reference_images(
                next_event,
                session,
            )

            # ── 空文本 ──────────────────────────────────────
            if not text and not new_reference_urls:
                await self._send_text(
                    next_event,
                    "请发送文字描述或参考图来说明你的图像需求。",
                    action="plan-empty-input",
                )
                controller.keep(
                    timeout=self._waiter_timeout(plan_config.timeout),
                    reset_timeout=True,
                )
                self._reset_plan_timeout_watchdog(
                    session_id,
                    next_event.unified_msg_origin,
                    plan_config.timeout,
                )
                return

            if not text:
                text = (
                    f"我刚刚发送了 {len(new_reference_urls)} 张参考图。"
                    "请结合这些参考图继续帮我明确图像需求。"
                )

            session.round_count += 1

            # 构造 Responses API input
            messages = list(session.history)
            if not messages:
                messages.append({"role": "developer", "content": PLAN_SYSTEM_PROMPT})

            image_urls_for_message = new_reference_urls
            if not session.history and session.reference_data_urls:
                image_urls_for_message = list(session.reference_data_urls)

            user_message = {
                "role": "user",
                "content": self._build_plan_user_content(text, image_urls_for_message),
            }
            messages.append(user_message)

            # 检查轮数上限
            reached_max = session.round_count >= plan_config.max_rounds
            if reached_max:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Please provide the final image generation prompt "
                            "using [FINAL_PROMPT] format now."
                        ),
                    }
                )

            # 调用 Responses API 规划
            try:
                reply = await plan_client.plan_responses(
                    messages,
                    model=plan_config.model,
                )
            except RuntimeError as e:
                logger.warning(
                    "[GPTImage2] plan Responses failed "
                    f"{self._event_context(next_event)} round={session.round_count} "
                    f"error={e}"
                )
                await self._send_text(
                    next_event,
                    f"模型调用失败：{e}\n请重试或发送 /image2 plan quit 退出。",
                    action="plan-model-error",
                )
                controller.keep(
                    timeout=self._waiter_timeout(plan_config.timeout),
                    reset_timeout=True,
                )
                self._reset_plan_timeout_watchdog(
                    session_id,
                    next_event.unified_msg_origin,
                    plan_config.timeout,
                )
                return

            # 解析 final prompt
            final_prompt = parse_final_prompt(reply)
            session.history = messages + [{"role": "assistant", "content": reply}]
            if final_prompt:
                session.final_prompt = final_prompt

            # 构建展示文本：用户说明保持中文，最终 prompt 可中英混合，
            # 需要出现在图中的文字应保留原文。
            display = remove_final_prompt_section(reply)
            if final_prompt:
                summary = display or "我已根据你的描述和参考图整理好图像需求。"
                display = (
                    f"✅ 我已整理好图像需求。\n\n{summary}\n\n"
                    "完整生成提示词已保存，确认生成时会单独发送。\n"
                    "发送 /image2 plan confirm 生成图片，"
                    "或 /image2 plan quit 退出。"
                )
            elif reached_max:
                display += (
                    "\n\n已达到最大对话轮数。"
                    "但模型还没有给出可确认的最终提示词。"
                    "请继续补充一句关键信息让我再次整理，"
                    "或发送 /image2 plan quit 退出。"
                )

            await self._send_text(next_event, display, action="plan-display")
            controller.keep(
                timeout=self._waiter_timeout(plan_config.timeout),
                reset_timeout=True,
            )
            self._reset_plan_timeout_watchdog(
                session_id,
                next_event.unified_msg_origin,
                plan_config.timeout,
            )

        try:
            await plan_waiter(event, session_filter=SenderSessionFilter())
        except TimeoutError:
            logger.info(
                "[GPTImage2] plan timeout "
                f"{self._event_context(event)} timeout={plan_config.timeout}s"
            )
            sent = await self._send_proactive_message(
                event.unified_msg_origin,
                "⌛ Plan 会话等待超时，已自动退出。",
                action="plan-timeout",
            )
            if not sent:
                await self._send_text(
                    event,
                    "⌛ Plan 会话等待超时，已自动退出。",
                    action="plan-timeout-fallback",
                )
        except Exception as e:
            logger.error(
                "[GPTImage2] plan error "
                f"{self._event_context(event)} error={type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            sent = await self._send_proactive_message(
                event.unified_msg_origin,
                f"Plan 模式发生错误：{e}",
                action="plan-error",
            )
            if not sent:
                await self._send_text(
                    event,
                    f"Plan 模式发生错误：{e}",
                    action="plan-error-fallback",
                )
        finally:
            self._cleanup_plan(session_id)
            event.stop_event()

    # ── 文生图 / 编辑 ──────────────────────────────────────────

    @image2.command("draw")
    async def draw(self, event: AstrMessageEvent):
        """文生图"""
        started = perf_counter()
        prompt = self._extract_prompt(event, "draw")
        if not prompt or not prompt.strip():
            logger.info(
                f"[GPTImage2] draw rejected empty prompt {self._event_context(event)}"
            )
            yield await self._text_result(
                event,
                "请提供图片描述提示词。用法：/image2 draw <提示词>",
                action="draw-empty-prompt",
            )
            return

        prompt = prompt.strip()
        api_mode = self.config.get("api_mode", "images")

        logger.info(
            "[GPTImage2] draw start "
            f"{self._event_context(event)} mode={api_mode} prompt_len={len(prompt)} "
            f"{self._params_summary(self._get_params())} "
            f"save_outputs={self.config.get('save_outputs', True)}"
        )

        chain = await self._generate_draw_chain(event, prompt, action="draw")
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
                f"{self._event_context(event)} prompt_len={len(prompt)}"
            )

    @image2.command("edit")
    async def edit(self, event: AstrMessageEvent):
        """从当前消息或引用消息提取图片后编辑"""
        started = perf_counter()
        prompt = self._extract_prompt(event, "edit")
        if not prompt or not prompt.strip():
            logger.info(
                f"[GPTImage2] edit rejected empty prompt {self._event_context(event)}"
            )
            yield await self._text_result(
                event,
                "请提供编辑提示词。用法：/image2 edit <提示词>"
                "（请附带图片或引用包含图片的消息）",
                action="edit-empty-prompt",
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
            yield await self._text_result(
                event,
                "没有找到可编辑的图片。请附带图片，"
                "或引用一条包含图片的消息后使用 /image2 edit <提示词>。",
                action="edit-no-images",
            )
            return

        try:
            client = self._get_client()
        except ValueError as e:
            yield await self._text_result(event, str(e), action="edit-config-error")
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
            yield await self._text_result(
                event,
                f"GPT Image2 调用失败：{e}",
                action="edit-api-error",
            )
            return
        except Exception as e:
            logger.error(
                "[GPTImage2] edit unexpected error "
                f"{self._event_context(event)} mode={api_mode} "
                f"elapsed_ms={self._elapsed_ms(started)} error={type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            yield await self._text_result(
                event,
                f"GPT Image2 调用失败：{e}",
                action="edit-unexpected-error",
            )
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
            yield await self._text_result(
                event,
                "GPT Image2 未返回任何可显示的图片。",
                action="edit-empty-result",
            )

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
