"""GPT Image2 AstrBot 插件

命令组 /image2：
  /image2 draw <prompt>  文生图
  /image2 edit <prompt>  从消息/引用消息提取图片并编辑
  /image2 plan           进入 Plan 多轮会话，辅助优化生图提示词
  /image2 plan confirm   在 Plan 中确认生成图片
  /image2 plan retry     重试上一条失败的 Plan 输入
  /image2 plan quit      退出 Plan 会话
  /image2 mode [模式]    查看/切换 API 模式（管理员）
  /image2 guard          查看/切换 Prompt Guard（管理员）
  /image2 retry          查看/切换备用站点重试提示（管理员）
  /image2 providers      查看生图站点状态（管理员）
  /image2 stats          查看 Provider 统计与诊断（管理员）
  /image2 diag           生成诊断包（管理员）
  /image2 help           展示用法和配置摘要
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import time as _time_module
from time import perf_counter, time
import traceback
import uuid

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    Image as CompImage,
    Node,
    Nodes,
    Plain,
    Reply,
)
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    USER_SESSIONS,
    session_waiter,
)
from PIL import Image as PILImage

from .card_renderer import build_markdown_card
from .client import GPTImageClient, ImageParams, ImageResult
from .config_redact import redact_config_value
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
    parse_final_prompt_zh,
)
from .providers import (
    ImageAPIProviderConfig,
    ProviderManager,
    FAILURE_REASON_ORDER,
    classify_failure_reason,
    classify_http_status_code,
    failure_reason_is_retryable,
    should_try_next_image_provider,
    is_image_input_unsupported,
    normalize_api_mode,
    normalize_provider_role,
    normalize_bool,
    build_provider_id,
    parse_fallback_api_provider_string,
    provider_stat_int,
    provider_stat_float,
    provider_health_score,
    provider_error_summary,
    provider_user_label,
    safe_text_preview,
    safe_markdown_preview,
    format_duration,
    prompt_rewrite_guard_config_key,
    prompt_rewrite_guard_default,
    prompt_rewrite_guard_status,
    parse_bool_switch,
    trim_jsonl,
    read_recent_failure_records,
    redact_provider_stats,
)


class PlanSessionFilter(SessionFilter):
    """只拦截当前发送者的 Plan 前缀消息，普通群聊消息放行。"""

    def filter(self, event: AstrMessageEvent) -> str:
        sender_id = event.get_sender_id() or "-"
        session_id = f"{event.unified_msg_origin}:sender:{sender_id}"
        text = (event.message_str or "").strip().lower()
        if self._should_capture(event, text):
            return session_id
        return f"{session_id}:pass"

    @classmethod
    def _should_capture(cls, event: AstrMessageEvent, text: str) -> bool:
        if cls.extract_plan_input(event) is not None:
            return True
        return cls._is_image2_plan_command(text)

    @staticmethod
    def _is_image2_plan_command(text: str) -> bool:
        """Only capture `/image2 plan ...` while a Plan waiter is active.

        Other `/image2 ...` commands such as `/image2 stats` and
        `/image2 providers` must continue to reach their normal handlers even
        while `/plan confirm` is generating images.
        """
        value = text.strip().lower()
        return (
            value == "/image2 plan"
            or value == "image2 plan"
            or value.startswith(("/image2 plan ", "image2 plan "))
        )

    @classmethod
    def extract_plan_input(cls, event: AstrMessageEvent) -> str | None:
        """Extract `/plan ...` from Plain components, falling back to message_str."""
        for comp in event.get_messages():
            if isinstance(comp, Plain):
                parsed = cls.strip_plan_prefix(comp.text or "")
                if parsed is not None:
                    return parsed

        parsed = cls.strip_plan_prefix(event.message_str or "")
        if parsed is not None:
            return parsed

        return cls.strip_embedded_plan_prefix(event.message_str or "")

    @staticmethod
    def strip_plan_prefix(text: str) -> str | None:
        value = text.strip()
        lower = value.lower()
        if lower == "/plan":
            return ""
        if not lower.startswith("/plan"):
            return None
        rest = value[len("/plan") :]
        if rest and not rest[0].isspace():
            return None
        return rest.strip()

    @classmethod
    def strip_embedded_plan_prefix(cls, text: str) -> str | None:
        """Handle adapters that render image placeholders before the Plain text."""
        value = text.strip()
        for marker in ("\n/plan", " /plan", "]/plan", "] /plan"):
            index = value.lower().find(marker)
            if index < 0:
                continue
            start = index + len(marker) - len("/plan")
            parsed = cls.strip_plan_prefix(value[start:])
            if parsed is not None:
                return parsed
        return None


@register(
    "gpt_image2",
    "233",
    "通过 OpenAI 兼容 API 调用 GPT Image2 完成图片生成与编辑",
    "0.4.3",
)
class GPTImage2Plugin(Star):
    PLAN_WAITER_TIMEOUT_GRACE = 10

    def __init__(self, context: Context, config: dict) -> None:
        super().__init__(context)
        self.config = config
        self.plugin_name = "astrbot_plugin_gpt_image2"
        self._output_dir: str | None = None
        self._text_image_dir: str | None = None
        self._plan_sessions: dict[str, PlanSession] = {}
        self._provider_manager = ProviderManager(
            config,
            lambda: str(getattr(self, "name", self.plugin_name)),
        )
        # Retry notice throttling state (shared ref; also accessible via manager)
        self._provider_retry_notice_state = (
            self._provider_manager._provider_retry_notice_state
        )

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
    def _strip_plan_prefix(text: str) -> str | None:
        """提取 `/plan ...` 后面的 Plan 输入；非 Plan 前缀返回 None。"""
        return PlanSessionFilter.strip_plan_prefix(text)

    @staticmethod
    def _extract_plan_input(event: AstrMessageEvent) -> str | None:
        """从消息链中提取 `/plan ...` 内容，支持图片在前、文字在后的消息。"""
        return PlanSessionFilter.extract_plan_input(event)

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
        prefer_image: bool | None = None,
        task_anchor: bool = False,
    ) -> None:
        """主动发送处理中提示，避免在 handler 中途 yield 打断处理流程。

        When *task_anchor* is ``True`` a visible ``[任务 #TAG]`` prefix and
        a ``Reply`` component are added to tie the message to the original
        user command.
        """
        try:
            await self._send_text(
                event,
                text,
                action=action,
                prefer_image=prefer_image,
                task_anchor=task_anchor,
            )
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
        """构建文本回复消息链；使用 image2 Markdown 卡片，失败则回退纯文本。"""
        use_image = self._render_text_as_image_enabled()
        if prefer_image is not None:
            use_image = prefer_image

        if use_image:
            card_chain = await self._build_image2_card_chain(text, action=action)
            if card_chain is not None:
                return card_chain

            logger.warning(
                "[GPTImage2] image2 markdown card render failed, "
                f"fallback to plain text action={action}"
            )

        return MessageChain().message(text)

    async def _build_image2_card_chain(
        self,
        text: str,
        *,
        action: str,
    ) -> MessageChain | None:
        """Render Markdown with image2's self-contained HTML card template."""
        payload = build_markdown_card(text)
        try:
            rendered = await self.html_render(
                payload.template,
                payload.data,
                return_url=False,
                options=payload.options,
            )
        except Exception as e:
            logger.warning(
                "[GPTImage2] image2 markdown card render failed, "
                "fallback to plain text "
                f"action={action} error={type(e).__name__}: {e}"
            )
            return None

        rendered = self._crop_image2_card_rendered(rendered, action=action)
        chain = self._message_chain_from_rendered_text_image(rendered, action=action)
        if chain is None:
            logger.warning(
                "[GPTImage2] image2 markdown card returned invalid image, "
                "fallback to plain text "
                f"action={action} {self._rendered_image_diagnostic(rendered)}"
            )
            return None

        logger.info(
            "[GPTImage2] image2 markdown card rendered "
            f"action={action} type={type(rendered).__name__}"
        )
        return chain

    def _crop_image2_card_rendered(self, rendered: object, *, action: str) -> object:
        """Crop viewport-height blank space from image2 card renders when possible."""
        try:
            if isinstance(rendered, bytes):
                cropped = self._crop_card_image_bytes(rendered)
                if cropped is not None:
                    logger.info(
                        "[GPTImage2] image2 markdown card cropped bytes "
                        f"action={action} before={len(rendered)} after={len(cropped)}"
                    )
                    return cropped
                return rendered

            if not isinstance(rendered, str) or not rendered.strip():
                return rendered

            value = rendered.strip()
            if value.startswith("base64://"):
                payload = value.removeprefix("base64://")
                try:
                    image_bytes = base64.b64decode(payload)
                except Exception:
                    return rendered
                cropped = self._crop_card_image_bytes(image_bytes)
                if cropped is None:
                    return rendered
                logger.info(
                    "[GPTImage2] image2 markdown card cropped base64 "
                    f"action={action} before_chars={len(payload)} after={len(cropped)}"
                )
                return cropped

            if value.startswith("http://") or value.startswith("https://"):
                return rendered

            source = Path(
                value.removeprefix("file:///")
                if value.startswith("file:///")
                else value
            )
            if not source.is_file() or not self._is_valid_image_file(str(source)):
                return rendered

            cropped = self._crop_card_image_file(source)
            if cropped is None:
                return rendered

            logger.info(
                "[GPTImage2] image2 markdown card cropped file "
                f"action={action} source={source} target={cropped}"
            )
            return str(cropped)
        except Exception as e:
            logger.debug(
                "[GPTImage2] image2 markdown card crop skipped "
                f"action={action} error={type(e).__name__}: {e}"
            )
            return rendered

    def _crop_card_image_file(self, source: Path) -> Path | None:
        with PILImage.open(source) as image:
            cropped = self._crop_card_image(image)
        if cropped is None:
            return None

        target = Path(self._get_text_image_dir()) / f"card-{uuid.uuid4().hex}.png"
        cropped.save(target, format="PNG", optimize=True)
        return target

    def _crop_card_image_bytes(self, data: bytes) -> bytes | None:
        with PILImage.open(io.BytesIO(data)) as image:
            cropped = self._crop_card_image(image)
        if cropped is None:
            return None

        output = io.BytesIO()
        cropped.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def _crop_card_image(image: PILImage.Image) -> PILImage.Image | None:
        """Trim bottom viewport blank space while keeping a small card margin."""
        width, height = image.size
        if height < 360 or width < 320:
            return None

        rgb = image.convert("RGB")
        left = max(0, int(width * 0.05))
        right = min(width, int(width * 0.95))
        step = max(4, (right - left) // 160)
        xs = list(range(left, right, step)) or [width // 2]

        def average_row(y: int) -> tuple[float, float, float]:
            totals = [0, 0, 0]
            for x in xs:
                pixel = rgb.getpixel((x, y))
                if isinstance(pixel, tuple):
                    red = int(pixel[0] if pixel[0] is not None else 0)
                    green = int(pixel[1] if pixel[1] is not None else 0)
                    blue = int(pixel[2] if pixel[2] is not None else 0)
                else:
                    red = green = blue = int(pixel if pixel is not None else 0)
                totals[0] += red
                totals[1] += green
                totals[2] += blue
            count = len(xs)
            return totals[0] / count, totals[1] / count, totals[2] / count

        bottom_rows = range(max(0, height - 20), height)
        bg_totals = [0.0, 0.0, 0.0]
        bg_count = 0
        for y in bottom_rows:
            row = average_row(y)
            bg_totals[0] += row[0]
            bg_totals[1] += row[1]
            bg_totals[2] += row[2]
            bg_count += 1
        bg = tuple(value / max(1, bg_count) for value in bg_totals)

        consecutive = 0
        detected_y: int | None = None
        for y in range(height - 1, -1, -1):
            row = average_row(y)
            diff = sum(abs(row[index] - bg[index]) for index in range(3))
            if diff >= 8.0:
                consecutive += 1
                if consecutive >= 4:
                    detected_y = y + 3
                    break
            else:
                consecutive = 0

        if detected_y is None:
            return None

        crop_bottom = min(height, detected_y + 42)
        if height - crop_bottom < 90:
            return None

        return image.crop((0, 0, width, crop_bottom)).copy()

    def _message_chain_from_rendered_text_image(
        self,
        rendered: object,
        *,
        action: str,
    ) -> MessageChain | None:
        """将 AstrBot 文转图结果转换成消息链，并校验本地图片 magic number。"""
        if isinstance(rendered, bytes):
            if self._is_image_bytes(rendered):
                return MessageChain(chain=[CompImage.fromBytes(rendered)])
            logger.debug(
                "[GPTImage2] AstrBot text-to-image bytes are not an image "
                f"action={action} size={len(rendered)}"
            )
            return None

        if not isinstance(rendered, str) or not rendered.strip():
            return None

        value = rendered.strip()
        if value.startswith("http://") or value.startswith("https://"):
            return MessageChain(chain=[CompImage.fromURL(value)])
        if value.startswith("base64://"):
            return MessageChain(
                chain=[CompImage.fromBase64(value.removeprefix("base64://"))]
            )

        path = value.removeprefix("file:///") if value.startswith("file:///") else value
        if self._is_valid_image_file(path):
            return MessageChain(chain=[CompImage.fromFileSystem(path)])
        return None

    @classmethod
    def _rendered_image_diagnostic(cls, rendered: object) -> str:
        """Build a compact, safe diagnostic summary for invalid render outputs."""
        if isinstance(rendered, bytes):
            head = rendered[:16]
            return (
                f"type=bytes size={len(rendered)} "
                f"content_hint={cls._bytes_content_hint(rendered)} "
                f"magic={head.hex() or '-'} "
                f"preview={cls._safe_bytes_preview(rendered)}"
            )

        if not isinstance(rendered, str):
            return f"type={type(rendered).__name__}"

        value = rendered.strip()
        if not value:
            return "type=str empty=true"
        if value.startswith("http://") or value.startswith("https://"):
            return f"type=str url={cls._safe_text_preview(value, limit=120)}"
        if value.startswith("base64://"):
            payload = value.removeprefix("base64://")
            return f"type=str base64_chars={len(payload)}"

        path = Path(
            value.removeprefix("file:///") if value.startswith("file:///") else value
        )
        if not path.exists():
            return (
                "type=str file_exists=false "
                f"value={cls._safe_text_preview(value, limit=160)}"
            )

        try:
            stat = path.stat()
            with path.open("rb") as image_file:
                sample = image_file.read(256)
        except OSError as e:
            return (
                "type=str file_readable=false "
                f"path={cls._safe_text_preview(str(path), limit=160)} "
                f"error={type(e).__name__}: {e}"
            )

        return (
            "type=str file_exists=true "
            f"path={cls._safe_text_preview(str(path), limit=160)} "
            f"size={stat.st_size} content_hint={cls._bytes_content_hint(sample)} "
            f"magic={sample[:16].hex() or '-'} "
            f"preview={cls._safe_bytes_preview(sample)}"
        )

    @staticmethod
    def _bytes_content_hint(data: bytes) -> str:
        stripped = data.lstrip()
        if data.startswith(b"\xff\xd8"):
            return "jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "webp"
        if stripped[:15].lower().startswith(b"<!doctype html") or stripped[
            :5
        ].lower().startswith(b"<html"):
            return "html"
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            return "json"
        if not stripped:
            return "empty"
        return "unknown"

    @staticmethod
    def _safe_bytes_preview(data: bytes, *, limit: int = 160) -> str:
        if not data:
            return "-"
        text = data[:limit].decode("utf-8", errors="replace")
        return safe_text_preview(text, limit=limit)

    @staticmethod
    def _safe_text_preview(text: str, *, limit: int = 160) -> str:
        return safe_text_preview(text, limit=limit)

    @staticmethod
    def _safe_markdown_preview(text: str, *, limit: int = 160) -> str:
        """Compact provider errors for Markdown cards without inline-code breakage."""
        return safe_markdown_preview(text, limit=limit)

    @staticmethod
    def _task_tag(event: AstrMessageEvent) -> str:
        """Generate a short, opaque task tag for draw/edit flow anchoring.

        The tag is a 6-char hex hash derived from the event's message origin
        and source-message metadata.  It is stable for a given message and
        does *not* leak the raw message ID or any user secret.
        """
        msg_obj = getattr(event, "message_obj", None)
        message_id = getattr(msg_obj, "message_id", "") or ""
        timestamp = getattr(msg_obj, "timestamp", "") or ""
        fallback_text = getattr(event, "message_str", "") or ""
        raw = f"{event.unified_msg_origin}:{message_id}:{timestamp}:{fallback_text}"
        return hashlib.sha256(raw.encode()).hexdigest()[:6].upper()

    @staticmethod
    def _build_reply(event: AstrMessageEvent) -> Reply | None:
        """Build a ``Reply`` component anchored to *event*'s source message.

        Returns ``None`` when the message object lacks the metadata needed
        to construct a meaningful reply — the caller should degrade gracefully
        by omitting the reply anchor.
        """
        try:
            msg_obj = event.message_obj
            message_id = getattr(msg_obj, "message_id", "")
            if not message_id:
                return None
            return Reply(
                id=message_id,
                sender_id=event.get_sender_id(),
                sender_nickname=event.get_sender_name(),
                # Do not attach the inbound message chain here.  Incoming
                # chains can contain Image/File component objects, and some
                # platform adapters serialize outbound components to JSON.
                # A send-side Reply only needs the source message ID to render
                # a native quote bubble when the adapter supports it.
                message_str=getattr(msg_obj, "message_str", "") or "",
                time=getattr(msg_obj, "timestamp", 0) or 0,
            )
        except Exception:
            return None

    @staticmethod
    def _is_valid_image_file(path: str) -> bool:
        try:
            with Path(path).open("rb") as image_file:
                return GPTImage2Plugin._is_image_bytes(image_file.read(16))
        except OSError:
            return False

    @staticmethod
    def _is_image_bytes(data: bytes) -> bool:
        return (
            data.startswith(b"\xff\xd8")
            or data.startswith(b"\x89PNG\r\n\x1a\n")
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        )

    async def _send_text(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        action: str,
        prefer_image: bool | None = None,
        task_anchor: bool = False,
    ) -> None:
        """发送文本类回复；默认文转图，失败回退文字。

        When *task_anchor* is ``True`` a visible ``[任务 #TAG]`` prefix is
        injected into *text* and a ``Reply`` component is prepended so the
        platform can render a native reply bubble.
        """
        if task_anchor:
            tag = self._task_tag(event)
            text = f"[任务 #{tag}]\n\n{text}"
        chain = await self._build_text_chain(
            text,
            action=action,
            prefer_image=prefer_image,
        )
        if task_anchor:
            reply = self._build_reply(event)
            if reply is not None:
                chain.chain.insert(0, reply)
        await event.send(chain)

    async def _text_result(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        action: str,
        prefer_image: bool | None = None,
        task_anchor: bool = False,
    ):
        """构建可 yield 的文本回复结果；默认文转图，失败回退文字。

        When *task_anchor* is ``True`` a visible ``[任务 #TAG]`` prefix is
        injected and a ``Reply`` component is prepended.
        """
        if task_anchor:
            tag = self._task_tag(event)
            text = f"[任务 #{tag}]\n\n{text}"
        chain = await self._build_text_chain(
            text,
            action=action,
            prefer_image=prefer_image,
        )
        result_chain = chain.chain
        if task_anchor:
            reply = self._build_reply(event)
            if reply is not None:
                result_chain = list(result_chain)
                result_chain.insert(0, reply)
        return event.chain_result(result_chain)

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
        providers = self._get_image_api_provider_configs()
        return self._build_image_api_client(providers[0])

    def _build_image_api_client(
        self,
        provider: ImageAPIProviderConfig,
    ) -> GPTImageClient:
        return GPTImageClient(
            api_key=provider.api_key,
            base_url=provider.base_url,
            model=provider.model,
            responses_model=provider.responses_model,
            timeout=self.config.get("timeout", 120),
            response_format_b64_json=self.config.get("response_format_b64_json", True),
            images_prompt_rewrite_guard=self._prompt_rewrite_guard_enabled("images"),
            responses_prompt_rewrite_guard=self._prompt_rewrite_guard_enabled(
                "responses"
            ),
        )

    def _get_image_api_provider_configs(self) -> list[ImageAPIProviderConfig]:
        return self._provider_manager.get_image_api_provider_configs()

    def _get_fallback_api_provider_items(self) -> list:
        return self._provider_manager.get_fallback_api_provider_items()

    def _resolve_fallback_capabilities(self, data: dict) -> str:
        return self._provider_manager.resolve_fallback_capabilities(data)

    def _parse_fallback_api_provider(
        self,
        item: object,
        *,
        index: int,
        default_api_key: str,
        default_base_url: str,
        default_model: str,
        default_responses_model: str,
    ) -> ImageAPIProviderConfig | None:
        return self._provider_manager.parse_fallback_api_provider(
            item,
            index=index,
            default_api_key=default_api_key,
            default_base_url=default_base_url,
            default_model=default_model,
            default_responses_model=default_responses_model,
        )

    @staticmethod
    def _normalize_api_mode(value: object) -> str:
        return normalize_api_mode(value)

    @staticmethod
    def _normalize_provider_role(value: object) -> str:
        return normalize_provider_role(value)

    @staticmethod
    def _normalize_bool(value: object, *, default: bool) -> bool:
        return normalize_bool(value, default=default)

    @staticmethod
    def _prompt_rewrite_guard_config_key(api_mode: str) -> str:
        return prompt_rewrite_guard_config_key(api_mode)

    @staticmethod
    def _prompt_rewrite_guard_default(api_mode: str) -> bool:
        return prompt_rewrite_guard_default(api_mode)

    def _prompt_rewrite_guard_enabled(self, api_mode: str) -> bool:
        return self._provider_manager.prompt_rewrite_guard_enabled(api_mode)

    @staticmethod
    def _prompt_rewrite_guard_status(enabled: bool) -> str:
        return prompt_rewrite_guard_status(enabled)

    @staticmethod
    def _parse_bool_switch(value: object) -> bool | None:
        return parse_bool_switch(value)

    @staticmethod
    def _build_provider_id(
        name: str,
        base_url: str,
        model: str,
        responses_model: str,
    ) -> str:
        return build_provider_id(name, base_url, model, responses_model)

    def _adaptive_provider_priority_enabled(self) -> bool:
        return self._provider_manager.adaptive_provider_priority_enabled()

    def _send_copyable_prompt_after_success_enabled(self) -> bool:
        return self._normalize_bool(
            self.config.get("send_copyable_prompt_after_success"),
            default=True,
        )

    def _provider_retry_notice_global_enabled(self) -> bool:
        return self._provider_manager.provider_retry_notice_global_enabled()

    def _provider_retry_notice_session_config(self) -> dict[str, bool]:
        return self._provider_manager.provider_retry_notice_session_config()

    def _set_provider_retry_notice_session_enabled(
        self,
        session_key: str,
        enabled: bool,
    ) -> None:
        self._provider_manager.set_provider_retry_notice_session_enabled(
            session_key, enabled
        )

    def _provider_retry_notice_session_key(self, event: AstrMessageEvent) -> str:
        return str(event.get_group_id() or event.unified_msg_origin or "-")

    def _provider_retry_notice_session_enabled(self, event: AstrMessageEvent) -> bool:
        session_key = self._provider_retry_notice_session_key(event)
        sessions = self._provider_retry_notice_session_config()
        return sessions.get(session_key, True)

    def _provider_retry_notice_enabled(self, event: AstrMessageEvent) -> bool:
        return (
            self._provider_retry_notice_global_enabled()
            and self._provider_retry_notice_session_enabled(event)
        )

    def _provider_retry_notice_interval(self) -> int:
        return self._provider_manager.provider_retry_notice_interval()

    @staticmethod
    def _format_duration(seconds: int) -> str:
        return format_duration(seconds)

    def _provider_retry_notice_status_text(self, event: AstrMessageEvent) -> str:
        global_enabled = self._provider_retry_notice_global_enabled()
        session_enabled = self._provider_retry_notice_session_enabled(event)
        effective = global_enabled and session_enabled
        session_key = self._provider_retry_notice_session_key(event)
        interval = self._provider_retry_notice_interval()
        return (
            "## 🔁 备用站点重试提示\n\n"
            f"- 全局开关：{self._prompt_rewrite_guard_status(global_enabled)}\n"
            f"- 当前会话开关：{self._prompt_rewrite_guard_status(session_enabled)}\n"
            f"- 当前实际状态：{self._prompt_rewrite_guard_status(effective)}\n"
            f"- 当前会话键：`{session_key}`\n"
            f"- 合并提示最短间隔：{self._format_duration(interval)}\n\n"
            "用法：\n"
            "- `/image2 retry` — 查看当前状态\n"
            "- `/image2 retry global <on|off>` — 切换全局重试提示\n"
            "- `/image2 retry here <on|off>` — 切换当前群/会话重试提示\n"
            "- `/image2 retry interval <秒>` — 设置合并提示最短间隔"
        )

    def _provider_failure_cooldown(self) -> int:
        return self._provider_manager.provider_failure_cooldown()

    def _provider_stats_path(self) -> Path:
        return self._provider_manager.provider_stats_path()

    def _provider_failures_jsonl_path(self) -> Path:
        return self._provider_manager.provider_failures_jsonl_path()

    def _append_provider_failure_record(
        self,
        provider: ImageAPIProviderConfig,
        *,
        error_msg: str,
        action: str,
        attempt_index: int,
        attempt_total: int,
        elapsed_ms: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Append a sanitized failure record to provider_failures.jsonl."""
        self._provider_manager.append_provider_failure_record(
            provider,
            error_msg=error_msg,
            action=action,
            attempt_index=attempt_index,
            attempt_total=attempt_total,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    @staticmethod
    def _trim_jsonl(path: Path, max_lines: int = 5000) -> None:
        trim_jsonl(path, max_lines=max_lines)

    def _load_provider_stats(self) -> dict:
        return self._provider_manager.load_provider_stats()

    def _save_provider_stats(self) -> None:
        self._provider_manager.save_provider_stats()

    @staticmethod
    def _provider_stat_int(item: dict, key: str) -> int:
        return provider_stat_int(item, key)

    @staticmethod
    def _provider_stat_float(item: dict, key: str) -> float:
        return provider_stat_float(item, key)

    def _provider_health_score(self, item: dict, now: float) -> float:
        return provider_health_score(item, now)

    def _adaptive_sort_normal_providers(
        self,
        normal: list[ImageAPIProviderConfig],
    ) -> list[ImageAPIProviderConfig]:
        return self._provider_manager.adaptive_sort_normal_providers(normal)

    def _rank_image_api_provider_configs(
        self,
        configs: list[ImageAPIProviderConfig],
    ) -> list[ImageAPIProviderConfig]:
        return self._provider_manager.rank_image_api_provider_configs(configs)

    def _record_image_provider_result(
        self,
        provider: ImageAPIProviderConfig,
        *,
        success: bool,
        error_msg: str = "",
    ) -> None:
        self._provider_manager.record_image_provider_result(
            provider, success=success, error_msg=error_msg
        )

    def _update_provider_stats_summary(self) -> None:
        self._provider_manager.update_provider_stats_summary()

    @staticmethod
    def _parse_fallback_api_provider_string(value: str) -> dict[str, str]:
        return parse_fallback_api_provider_string(value)

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

    def _get_text_image_dir(self) -> str:
        """获取插件文转图输出目录（惰性创建）。"""
        if self._text_image_dir is None:
            plugin_name = getattr(self, "name", self.plugin_name)
            text_image_dir = (
                Path(get_astrbot_data_path())
                / "plugin_data"
                / plugin_name
                / "text_images"
            )
            text_image_dir.mkdir(parents=True, exist_ok=True)
            self._text_image_dir = str(text_image_dir)
            logger.debug(
                f"[GPTImage2] text image directory ready path={self._text_image_dir}"
            )
        return self._text_image_dir

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

    def _get_edit_aliases(self) -> list[str]:
        """读取 `/image2 edit` 的自定义触发别名。"""
        raw_value = self.config.get("edit_aliases", [])
        if isinstance(raw_value, str):
            raw_items = raw_value.replace(";", "\n").replace(",", "\n").splitlines()
        elif isinstance(raw_value, (list, tuple, set)):
            raw_items = [str(item) for item in raw_value]
        else:
            raw_items = []

        aliases: list[str] = []
        seen: set[str] = set()
        reserved = {"/image2", "image2", "/image2 edit", "image2 edit"}
        for item in raw_items:
            alias = " ".join(str(item or "").split())
            normalized = alias.lower()
            if not alias or normalized in seen or normalized in reserved:
                continue
            aliases.append(alias)
            seen.add(normalized)
        aliases.sort(key=len, reverse=True)
        return aliases

    @staticmethod
    def _match_alias_prefix(text: str, alias: str) -> str | None:
        """Return prompt text when *text* starts with *alias* as a command."""
        value = text.strip()
        alias = alias.strip()
        if not value or not alias:
            return None

        value_lower = value.lower()
        alias_lower = alias.lower()
        if value_lower == alias_lower:
            return ""
        if not value_lower.startswith(alias_lower):
            return None

        rest = value[len(alias) :]
        if rest and rest[0].isspace():
            return rest.strip()
        return None

    def _extract_edit_alias_prompt(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str, str] | None:
        """Extract prompt from configured edit aliases.

        Returns ``(prompt, alias)`` when the event text starts with a configured
        alias. Plain components are checked in addition to ``message_str`` so
        aliases still work when images appear before text in the message chain.
        """
        aliases = self._get_edit_aliases()
        if not aliases:
            return None

        candidates = [event.message_str or ""]
        for comp in event.get_messages():
            if isinstance(comp, Plain):
                candidates.append(comp.text or "")

        seen_candidates: set[str] = set()
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text or text in seen_candidates:
                continue
            seen_candidates.add(text)
            lower = text.lower()
            if lower.startswith(("/image2 ", "image2 ")):
                continue
            for alias in aliases:
                prompt = self._match_alias_prefix(text, alias)
                if prompt is not None:
                    return prompt, alias
        return None

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
            timeout=self.config.get("timeout", 120),
            response_format_b64_json=self.config.get("response_format_b64_json", True),
            images_prompt_rewrite_guard=self._prompt_rewrite_guard_enabled("images"),
            responses_prompt_rewrite_guard=self._prompt_rewrite_guard_enabled(
                "responses"
            ),
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
    def _cancel_plan_timeout_watchdog(session: PlanSession) -> None:
        """Plan confirm 生图期间暂停空闲超时 watchdog。"""
        if session.timeout_task and not session.timeout_task.done():
            session.timeout_task.cancel()
        session.timeout_task = None

    @staticmethod
    def _has_plan_retry_snapshot(session: PlanSession) -> bool:
        """Whether the Plan session has a failed model-call input to retry."""
        return session.last_failed_input_text is not None

    @staticmethod
    def _store_plan_retry_snapshot(
        session: PlanSession,
        *,
        text: str,
        image_urls: list[str],
        reached_max: bool,
        round_count: int,
    ) -> None:
        """Remember the exact failed Plan input so the user can `/plan retry`."""
        session.last_failed_input_text = text
        session.last_failed_image_urls = list(image_urls)
        session.last_failed_reached_max = reached_max
        session.last_failed_round_count = round_count

    @staticmethod
    def _clear_plan_retry_snapshot(session: PlanSession) -> None:
        """Clear any pending failed Plan input snapshot after a successful round."""
        session.last_failed_input_text = None
        session.last_failed_image_urls.clear()
        session.last_failed_reached_max = False
        session.last_failed_round_count = 0

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
                    "## ⌛ Plan 会话等待超时\n\n已自动退出。",
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
            api_timeout = int(self.config.get("timeout", 120))
        except (TypeError, ValueError):
            api_timeout = 120
        return max(plan_config.timeout, api_timeout + 60)

    def _get_plan_confirm_processing_timeout(self, plan_config: PlanConfig) -> int:
        """Plan confirm 生图阶段的 session_waiter 保活时长。"""
        try:
            api_timeout = max(1, int(self.config.get("timeout", 120)))
        except (TypeError, ValueError):
            api_timeout = 120

        try:
            global_mode = self._normalize_api_mode(
                self.config.get("api_mode", "images")
            )
            provider_count = sum(
                1
                for provider in self._get_image_api_provider_configs()
                if provider.supports_mode(global_mode)
            )
        except ValueError:
            provider_count = 1

        attempts = max(1, provider_count)
        return max(plan_config.timeout, attempts * (api_timeout + 30) + 60)

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

    @staticmethod
    def _single_line_command_prompt(prompt: str) -> str:
        """Collapse a final prompt into one line so it can be copied as a command."""
        return " ".join(str(prompt or "").replace("\x00", " ").split())

    @staticmethod
    def _split_text_for_forward(text: str, *, limit: int = 1200) -> list[str]:
        """Split long prompt text into merged-forward friendly chunks."""
        value = str(text or "").strip()
        if not value:
            return []

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for raw_line in value.splitlines() or [value]:
            line = raw_line.rstrip()
            while len(line) > limit:
                if current:
                    chunks.append("\n".join(current).strip())
                    current = []
                    current_len = 0
                chunks.append(line[:limit].strip())
                line = line[limit:]

            addition_len = len(line) + (1 if current else 0)
            if current and current_len + addition_len > limit:
                chunks.append("\n".join(current).strip())
                current = [line]
                current_len = len(line)
            else:
                current.append(line)
                current_len += addition_len

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _forward_node(
        title: str,
        text: str,
        *,
        part: int | None = None,
        total: int | None = None,
    ) -> Node:
        suffix = f"（{part}/{total}）" if part is not None and total else ""
        return Node(
            name="GPT Image2",
            uin="233",
            content=[Plain(f"{title}{suffix}\n\n{text}".strip())],
        )

    def _plan_prompt_forward_nodes(self, session: PlanSession) -> list[Node]:
        """Build merged-forward nodes for Chinese and English final prompts."""
        self._dedupe_plan_reference_images(session)
        zh_prompt = (session.final_prompt_zh or "").strip()
        final_prompt = (session.final_prompt or "").strip()

        nodes = [
            self._forward_node(
                "🧾 Plan 最终提示词",
                "完整提示词已收纳为合并转发，避免长英文提示词刷屏。\n"
                "建议优先复制中文提示词复现；英文/混合提示词保留为完整生成依据。",
            )
        ]

        if zh_prompt:
            nodes.extend(
                self._prompt_text_nodes("🇨🇳 中文提示词（推荐复制）", zh_prompt)
            )
        else:
            nodes.append(
                self._forward_node(
                    "🇨🇳 中文提示词",
                    "模型本轮没有返回 FINAL_PROMPT_ZH；请优先使用后续英文/混合提示词。",
                )
            )

        if final_prompt:
            nodes.extend(
                self._prompt_text_nodes(
                    "🌐 英文/混合提示词（完整生成依据）", final_prompt
                )
            )
        return nodes

    async def _send_plan_final_prompt_forward(
        self,
        event: AstrMessageEvent,
        session: PlanSession,
    ) -> None:
        """Send final prompts as merged-forward nodes, with a plain-text fallback."""
        nodes = self._plan_prompt_forward_nodes(session)
        try:
            await event.send(MessageChain(chain=[Nodes(nodes)]))
            logger.info(
                "[GPTImage2] plan final prompt sent as merged forward "
                f"nodes={len(nodes)}"
            )
        except Exception as e:
            logger.warning(
                "[GPTImage2] plan final prompt merged forward failed "
                f"error={type(e).__name__}: {e}"
            )
            zh_prompt = session.final_prompt_zh or "（模型未返回中文提示词）"
            final_prompt = session.final_prompt or "（模型未返回英文/混合提示词）"
            await self._send_text(
                event,
                "## 🧾 将用于生成的完整提示词\n\n"
                "合并转发发送失败，已回退为普通文本。\n\n"
                f"### 中文提示词\n\n{zh_prompt}\n\n"
                f"### 英文/混合提示词\n\n{final_prompt}",
                action="plan-final-prompt-forward-fallback",
                prefer_image=False,
            )

    def _prompt_text_nodes(self, title: str, text: str) -> list[Node]:
        chunks = self._split_text_for_forward(text)
        total = len(chunks)
        if total <= 1:
            return [self._forward_node(title, chunks[0] if chunks else "-")]
        return [
            self._forward_node(title, chunk, part=index, total=total)
            for index, chunk in enumerate(chunks, start=1)
        ]

    def _plan_copyable_prompt(self, session: PlanSession) -> str:
        """Prefer the exact Chinese prompt for copyable IM commands."""
        return session.final_prompt_zh or session.final_prompt or ""

    def _build_plan_copyable_command_text(
        self,
        session: PlanSession,
        *,
        succeeded: bool,
    ) -> str:
        """Build a plain-text command for reusing a Plan final prompt."""
        self._dedupe_plan_reference_images(session)
        prompt = self._single_line_command_prompt(self._plan_copyable_prompt(session))
        reference_count = len(session.reference_data_urls)
        if reference_count:
            command = f"/image2 edit {prompt}"
            if succeeded:
                usage_note = (
                    f"这条命令需要配合 {reference_count} 张参考图使用。"
                    "之后如需在其他地方复现效果，请附带相同参考图，"
                    "或引用包含参考图的消息发送下面这条命令。"
                )
            else:
                usage_note = (
                    f"这条命令需要配合 {reference_count} 张参考图使用。"
                    "请先发送 /plan quit 退出当前 Plan 会话，"
                    "然后附带参考图或引用包含参考图的消息发送下面这条命令。"
                )
        else:
            command = f"/image2 draw {prompt}"
            if succeeded:
                usage_note = (
                    "当前 Plan 会话没有参考图。之后可直接发送下面这条命令复现效果。"
                )
            else:
                usage_note = (
                    "当前 Plan 会话没有参考图。请先发送 /plan quit 退出当前 Plan 会话，"
                    "然后直接发送下面这条命令。"
                )

        title = (
            "Plan 生图成功，可复制到其他地方复用的命令："
            if succeeded
            else "Plan 最终生图失败时可复制的直接重试命令："
        )
        return f"{title}\n\n{usage_note}\n\n{command}"

    def _build_plan_direct_retry_command_text(self, session: PlanSession) -> str:
        """Build a plain-text command for retrying outside Plan without re-planning."""
        return self._build_plan_copyable_command_text(session, succeeded=False)

    def _plan_copyable_command_forward_nodes(self, session: PlanSession) -> list[Node]:
        """Build merged-forward nodes for a long copyable command after success."""
        text = self._build_plan_copyable_command_text(session, succeeded=True)
        title, _, rest = text.partition("\n\n")
        usage_note, _, command = rest.partition("\n\n")

        nodes = [
            self._forward_node(
                "✅ Plan 生图成功：复用说明",
                usage_note or "可复制后续节点中的命令，在其他地方复用相同提示词。",
            )
        ]
        command_text = command or text
        nodes.extend(self._prompt_text_nodes(title or "可复制命令", command_text))
        return nodes

    async def _send_plan_copyable_success_command(
        self,
        event: AstrMessageEvent,
        session: PlanSession,
    ) -> None:
        """Send the success copy command as merged forward to avoid chat spam."""
        nodes = self._plan_copyable_command_forward_nodes(session)
        try:
            await event.send(MessageChain(chain=[Nodes(nodes)]))
            logger.info(
                "[GPTImage2] plan copyable success command sent as merged forward "
                f"nodes={len(nodes)}"
            )
        except Exception as e:
            logger.warning(
                "[GPTImage2] plan copyable success command merged forward failed "
                f"error={type(e).__name__}: {e}"
            )
            await self._send_text(
                event,
                self._build_plan_copyable_command_text(session, succeeded=True),
                action="plan-copyable-success-command-fallback",
                prefer_image=False,
            )

    def _revised_prompt_forward_nodes(
        self,
        results: list[ImageResult],
        *,
        action: str,
    ) -> list[Node]:
        """Build merged-forward nodes for long revised prompts from image APIs."""
        prompts = [
            (index, item.revised_prompt.strip())
            for index, item in enumerate(results, start=1)
            if item.revised_prompt and item.revised_prompt.strip()
        ]
        if not prompts:
            return []

        nodes = [
            self._forward_node(
                "📝 生图提示词 / 模型改写提示词",
                "图片会单独发送；这里收纳模型返回的 revised_prompt，"
                "避免 draw/edit 成功消息被长提示词刷屏。",
            )
        ]
        for index, prompt in prompts:
            nodes.extend(
                self._prompt_text_nodes(
                    f"第 {index} 张图片 revised_prompt（{action}）",
                    prompt,
                )
            )
        return nodes

    async def _send_revised_prompt_forward(
        self,
        event: AstrMessageEvent,
        results: list[ImageResult],
        *,
        action: str,
    ) -> None:
        """Send revised_prompt via merged forward for draw/edit successes."""
        nodes = self._revised_prompt_forward_nodes(results, action=action)
        if not nodes:
            return
        try:
            await event.send(MessageChain(chain=[Nodes(nodes)]))
            logger.info(
                "[GPTImage2] revised prompts sent as merged forward "
                f"action={action} nodes={len(nodes)}"
            )
        except Exception as e:
            logger.warning(
                "[GPTImage2] revised prompt merged forward failed "
                f"action={action} error={type(e).__name__}: {e}"
            )
            fallback = "\n\n".join(
                f"第 {idx} 张图片 revised_prompt:\n{prompt}"
                for idx, prompt in [
                    (index, item.revised_prompt.strip())
                    for index, item in enumerate(results, start=1)
                    if item.revised_prompt and item.revised_prompt.strip()
                ]
            )
            if fallback:
                await self._send_text(
                    event,
                    "## 📝 生图提示词 / 模型改写提示词\n\n"
                    "合并转发发送失败，已回退为普通文本。\n\n"
                    f"{fallback}",
                    action=f"{action}-revised-prompt-forward-fallback",
                    prefer_image=False,
                )

    async def _collect_plan_reference_images(
        self,
        event: AstrMessageEvent,
        session: PlanSession,
    ) -> list[str]:
        """从 Plan 消息中收集参考图，返回本轮新增 data URLs。"""
        self._dedupe_plan_reference_images(session)
        remaining = self._get_max_input_images() - len(session.reference_data_urls)
        if remaining <= 0:
            return []

        images = extract_images_from_event(event.get_messages(), remaining)
        if not images:
            return []

        data_urls: list[str] = []
        existing_data_urls = set(session.reference_data_urls)
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
            if data_url in existing_data_urls:
                logger.info(
                    "[GPTImage2] plan duplicate reference image skipped "
                    f"{self._event_context(event)} index={idx} "
                    f"total={len(session.reference_data_urls)}"
                )
                continue
            session.reference_images.append(image)
            session.reference_data_urls.append(data_url)
            data_urls.append(data_url)
            existing_data_urls.add(data_url)
            logger.debug(
                "[GPTImage2] plan reference image collected "
                f"{self._event_context(event)} index={idx} chars={len(data_url)} "
                f"total={len(session.reference_data_urls)}"
            )
        return data_urls

    def _dedupe_plan_reference_images(self, session: PlanSession) -> None:
        """去重 Plan 会话参考图，避免同一图片跨轮次或引用链重复计数。"""
        if not session.reference_data_urls:
            return

        before_urls = len(session.reference_data_urls)
        before_images = len(session.reference_images)
        dedup_images: list = []
        dedup_urls: list[str] = []
        seen: set[str] = set()

        for index, data_url in enumerate(session.reference_data_urls):
            if not data_url or data_url in seen:
                continue
            seen.add(data_url)
            dedup_urls.append(data_url)
            if index < len(session.reference_images):
                dedup_images.append(session.reference_images[index])

        session.reference_data_urls = dedup_urls
        session.reference_images = dedup_images

        if len(dedup_urls) != before_urls or len(dedup_images) != before_images:
            logger.info(
                "[GPTImage2] plan reference images deduplicated "
                f"before_images={before_images} before_urls={before_urls} "
                f"after_images={len(dedup_images)} after_urls={len(dedup_urls)}"
            )

    @staticmethod
    def _is_image_input_unsupported(error_msg: str) -> bool:
        return is_image_input_unsupported(error_msg)

    # ── Failure classification ──────────────────────────────────

    FAILURE_REASON_ORDER = FAILURE_REASON_ORDER

    @staticmethod
    def _classify_failure_reason(error_msg: str) -> str:
        return classify_failure_reason(error_msg)

    @staticmethod
    def _classify_http_status_code(error_msg: str) -> int | None:
        return classify_http_status_code(error_msg)

    @staticmethod
    def _failure_reason_is_retryable(reason_key: str) -> bool:
        return failure_reason_is_retryable(reason_key)

    @staticmethod
    def _should_try_next_image_provider(error_msg: str) -> bool:
        return should_try_next_image_provider(error_msg)

    @staticmethod
    def _provider_error_summary(provider_errors: list[tuple[str, str]]) -> str:
        return provider_error_summary(provider_errors)

    @staticmethod
    def _provider_user_label(
        provider: ImageAPIProviderConfig,
        global_mode: str = "",
    ) -> str:
        return provider_user_label(provider, global_mode)

    async def _send_provider_switch_notice(
        self,
        event: AstrMessageEvent,
        *,
        action: str,
        failed_provider: ImageAPIProviderConfig,
        next_provider: ImageAPIProviderConfig,
        error_msg: str,
        next_index: int,
        total: int,
        global_mode: str = "",
    ) -> None:
        """Notify users before trying the next provider without spamming chats."""
        if not self._provider_retry_notice_enabled(event):
            return

        state_key = self._provider_retry_notice_session_key(event)
        now = time()
        state = self._provider_retry_notice_state.setdefault(
            state_key,
            {"last_sent_at": 0.0, "pending": []},
        )
        interval = self._provider_retry_notice_interval()
        switch_summary = (
            f"{self._provider_user_label(failed_provider, global_mode)} → "
            f"{self._provider_user_label(next_provider, global_mode)}："
            f"{self._safe_markdown_preview(error_msg, limit=120)}"
        )
        try:
            last_sent_at = float(str(state.get("last_sent_at") or 0.0))
        except (TypeError, ValueError):
            last_sent_at = 0.0
        elapsed = now - last_sent_at
        if last_sent_at > 0 and elapsed < interval:
            pending = state.get("pending")
            if not isinstance(pending, list):
                pending = []
            pending.append(switch_summary)
            state["pending"] = pending[-10:]
            logger.debug(
                "[GPTImage2] provider switch notice suppressed "
                f"action={action} session={state_key} elapsed_ms={int(elapsed * 1000)} "
                f"interval={interval}s pending={len(state['pending'])}"
            )
            return

        pending = state.get("pending")
        if not isinstance(pending, list):
            pending = []
        pending_items = [str(item) for item in pending if str(item or "").strip()]
        pending_display = pending_items[-5:]
        hidden_pending = max(0, len(pending_items) - len(pending_display))
        pending_lines = "\n".join(f"  - {item}" for item in pending_display)
        hidden_text = (
            f"\n  - ……另有 {hidden_pending} 条已省略" if hidden_pending else ""
        )
        suppressed_text = (
            "\n- 距上次提示后已合并省略："
            f"**{len(pending_items)}** 条切换提示\n"
            f"- 合并摘要：\n{pending_lines}{hidden_text}"
            if pending_items
            else ""
        )
        interval_text = "无限流" if interval <= 0 else self._format_duration(interval)
        state["last_sent_at"] = now
        state["pending"] = []

        await self._send_processing_ack(
            event,
            "## 🔁 正在切换备用 API 站点\n\n"
            f"- 已失败：**{self._provider_user_label(failed_provider, global_mode)}**\n"
            f"- 失败原因：{self._safe_markdown_preview(error_msg, limit=220)}\n"
            f"- 即将尝试：**{self._provider_user_label(next_provider, global_mode)}**"
            f"（第 {next_index}/{total} 个站点）"
            f"{suppressed_text}\n"
            f"- 当前合并提示间隔：{interval_text}",
            action=f"{action}-provider-switch",
            task_anchor=True,
        )

    def _provider_success_notice(
        self,
        provider: ImageAPIProviderConfig,
        *,
        attempt_index: int,
        total: int,
        failed_count: int,
        elapsed_ms: int,
        global_mode: str = "",
        task_tag: str = "",
    ) -> Plain:
        retry_text = f"，已跳过 {failed_count} 个失败站点" if failed_count else ""
        tag_prefix = f"[任务 #{task_tag}]\n\n" if task_tag else ""
        return Plain(
            tag_prefix + "✅ 生图成功："
            f"{self._provider_user_label(provider, global_mode)}"
            f"（第 {attempt_index}/{total} 个站点{retry_text}，耗时 {elapsed_ms}ms）\n"
        )

    async def _call_draw_api(
        self,
        client: GPTImageClient,
        prompt: str,
        params: ImageParams,
        api_mode: str,
        reference_images: list,
        reference_data_urls: list[str],
        reference_count: int,
        action: str,
    ) -> list[ImageResult]:
        """调用生图 API 并返回结果列表。

        根据 api_mode 和参考图数量选择正确的 API 路径。
        """
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
            return await client.edit_images_api(prompt, image_paths, params)

        if reference_count:
            data_urls = list(reference_data_urls)
            if not data_urls:
                for idx, image in enumerate(reference_images, start=1):
                    data_url = await image_to_data_url(image)
                    data_urls.append(data_url)
                    logger.debug(
                        "[GPTImage2] reference image converted to data URL "
                        f"action={action} index={idx} chars={len(data_url)}"
                    )
            return await client.edit_responses_api(prompt, data_urls, params)

        if api_mode == "images":
            return await client.generate_images_api(prompt, params)
        return await client.generate_responses_api(prompt, params)

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
            provider_configs = self._get_image_api_provider_configs()
        except ValueError as e:
            await self._send_text(event, str(e), action=f"{action}-config-error")
            return []

        global_mode = self._normalize_api_mode(self.config.get("api_mode", "images"))

        # Filter providers by global mode support
        viable = [p for p in provider_configs if p.supports_mode(global_mode)]
        if not viable:
            other_mode = "responses" if global_mode == "images" else "images"
            await self._send_text(
                event,
                f"## ⚠️ 当前 `{global_mode}` 模式下无可用生图站点\n\n"
                f"请使用 `/image2 mode {other_mode}` 切换到另一模式，"
                "或配置至少一个支持当前模式的站点。",
                action=f"{action}-no-viable-provider",
            )
            return []

        params = self._get_params()
        reference_images = reference_images or []
        reference_data_urls = reference_data_urls or []
        reference_count = max(len(reference_images), len(reference_data_urls))

        if send_ack:
            if action == "edit":
                ack_text = (
                    f"✅ 已收到图像编辑请求，已识别 {reference_count} 张参考图，"
                    f"正在使用 {global_mode} 模式处理，请稍候…"
                )
            elif reference_count:
                ack_text = (
                    f"✅ 已识别 {reference_count} 张参考图，正在使用 {global_mode} "
                    "模式生成图片，请稍候…"
                )
            else:
                ack_text = f"✅ 正在使用 {global_mode} 模式生成图片，请稍候…"

            await self._send_processing_ack(
                event,
                ack_text,
                action=action,
                task_anchor=True,
            )

        started = perf_counter()
        results: list[ImageResult] | None = None
        selected_provider: ImageAPIProviderConfig | None = None
        selected_attempt_index = 0
        provider_errors: list[tuple[str, str]] = []

        for index, provider in enumerate(viable, start=1):
            client = self._build_image_api_client(provider)
            provider_action = f"{action}:{provider.name}" if len(viable) > 1 else action
            attempt_start = perf_counter()
            logger.info(
                "[GPTImage2] image provider attempt "
                f"action={action} provider={provider.name} index={index}/"
                f"{len(viable)} mode={global_mode} "
                f"role={provider.role} adaptive={provider.adaptive}"
            )
            try:
                results = await self._call_draw_api(
                    client=client,
                    prompt=prompt,
                    params=params,
                    api_mode=global_mode,
                    reference_images=reference_images,
                    reference_data_urls=reference_data_urls,
                    reference_count=reference_count,
                    action=provider_action,
                )
                self._record_image_provider_result(provider, success=True)
                selected_provider = provider
                selected_attempt_index = index
                break
            except RuntimeError as e:
                error_msg = str(e)
                attempt_elapsed = self._elapsed_ms(attempt_start)
                self._record_image_provider_result(
                    provider,
                    success=False,
                    error_msg=error_msg,
                )
                self._append_provider_failure_record(
                    provider,
                    error_msg=error_msg,
                    action=action,
                    attempt_index=index,
                    attempt_total=len(viable),
                    elapsed_ms=attempt_elapsed,
                    error=e,
                )
                provider_errors.append(
                    (f"{provider.name}/{global_mode}/{provider.role}", error_msg)
                )
                logger.warning(
                    "[GPTImage2] image provider failed "
                    f"action={action} provider={provider.name} index={index}/"
                    f"{len(viable)} role={provider.role} "
                    f"elapsed_ms={attempt_elapsed} error={e}"
                )
                if index < len(viable) and self._should_try_next_image_provider(
                    error_msg
                ):
                    await self._send_provider_switch_notice(
                        event,
                        action=action,
                        failed_provider=provider,
                        next_provider=viable[index],
                        error_msg=error_msg,
                        next_index=index + 1,
                        total=len(viable),
                        global_mode=global_mode,
                    )
                    continue

                provider_summary = self._provider_error_summary(provider_errors)
                if reference_count and self._is_image_input_unsupported(error_msg):
                    await self._send_text(
                        event,
                        "## ⚠️ 上游拒绝读取参考图\n\n"
                        f"错误：{self._safe_markdown_preview(error_msg, limit=320)}\n\n"
                        f"- 当前 API 模式：`{global_mode}`\n"
                        f"- 参考图数量：`{reference_count}`\n"
                        f"- 生图模型：`{provider.model}`\n"
                        f"- Responses 模型：`{provider.responses_model}`"
                        f"{provider_summary}\n\n"
                        "这通常表示上游服务实际路由到的模型不支持图片输入，"
                        "或服务商对当前 endpoint/model 的图像输入兼容性有问题。"
                        "插件不会自动丢弃参考图重试，以免生成结果偏离 Plan。",
                        action=f"{action}-image-input-error",
                        task_anchor=True,
                    )
                    return []
                await self._send_text(
                    event,
                    "## ⚠️ GPT Image2 调用失败\n\n"
                    f"最后错误：{self._safe_markdown_preview(error_msg, limit=320)}"
                    f"{provider_summary}",
                    action=f"{action}-api-error",
                    task_anchor=True,
                )
                return []
            except Exception as e:
                logger.error(
                    "[GPTImage2] draw unexpected error "
                    f"action={action} provider={provider.name} "
                    f"error={type(e).__name__}: {e}\n"
                    f"{traceback.format_exc()}"
                )
                await self._send_text(
                    event,
                    f"## ⚠️ GPT Image2 调用失败\n\n`{e}`",
                    action=f"{action}-unexpected-error",
                    task_anchor=True,
                )
                return []

        if results is None or selected_provider is None:
            await self._send_text(
                event,
                "## ⚠️ GPT Image2 调用失败\n\n所有 API 站点均未返回结果。"
                f"{self._provider_error_summary(provider_errors)}",
                action=f"{action}-provider-empty",
                task_anchor=True,
            )
            return []

        logger.info(
            "[GPTImage2] image API success "
            f"action={action} provider={selected_provider.name} "
            f"mode={global_mode} role={selected_provider.role} "
            f"adaptive={selected_provider.adaptive} results={len(results)} "
            f"elapsed_ms={self._elapsed_ms(started)}"
        )

        if action in {"draw", "edit"}:
            await self._send_revised_prompt_forward(event, results, action=action)

        tag = self._task_tag(event)
        chain = self._build_image_chain(results, params)
        if chain:
            chain.insert(
                0,
                self._provider_success_notice(
                    selected_provider,
                    attempt_index=selected_attempt_index,
                    total=len(viable),
                    failed_count=len(provider_errors),
                    elapsed_ms=self._elapsed_ms(started),
                    global_mode=global_mode,
                    task_tag=tag,
                ),
            )
            reply = self._build_reply(event)
            if reply is not None:
                chain.insert(0, reply)
        if not chain:
            await self._send_text(
                event,
                "## ⚠️ GPT Image2 未返回任何可显示的图片",
                action=f"{action}-empty-result",
                task_anchor=True,
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
        cfg = self.config
        api_key_set = "✅ 已设置" if cfg.get("api_key") else "❌ 未设置"
        t2i_status = "✅ 开启" if self._render_text_as_image_enabled() else "关闭"
        save_status = "✅ 开启" if cfg.get("save_outputs", True) else "关闭"
        adaptive_status = (
            "✅ 开启" if self._adaptive_provider_priority_enabled() else "关闭"
        )
        edit_aliases = self._get_edit_aliases()
        edit_aliases_status = (
            "、".join(f"`{alias}`" for alias in edit_aliases) or "未配置"
        )
        retry_notice_status = self._prompt_rewrite_guard_status(
            self._provider_retry_notice_global_enabled()
        )
        retry_notice_interval = self._provider_retry_notice_interval()
        images_guard_status = self._prompt_rewrite_guard_status(
            self._prompt_rewrite_guard_enabled("images")
        )
        responses_guard_status = self._prompt_rewrite_guard_status(
            self._prompt_rewrite_guard_enabled("responses")
        )
        current_mode = self._normalize_api_mode(cfg.get("api_mode", "images"))
        primary_provider_name = str(cfg.get("primary_provider_name", "") or "primary")
        authoritative_enabled = self._normalize_bool(
            cfg.get("authoritative_fallback_enabled"), default=False
        )
        authoritative_status = "✅ 开启" if authoritative_enabled else "❌ 关闭"
        try:
            provider_configs = self._get_image_api_provider_configs()
            image_provider_count = len(provider_configs)
            viable_provider_count = sum(
                1 for p in provider_configs if p.supports_mode(current_mode)
            )
        except ValueError:
            image_provider_count = 0
            viable_provider_count = 0

        help_md = (
            "## 📋 GPT Image2 使用说明\n\n"
            "### 命令\n\n"
            "- `/image2 draw <提示词>` — 文生图\n"
            "- `/image2 edit <提示词>` — 编辑图片（附带图片或引用图片消息）\n"
            "- 自定义 edit 别名 — 可在配置 `edit_aliases` 中添加，"
            "用于触发 `/image2 edit`\n"
            "- `/image2 plan` — 进入 Plan 多轮图文会话，辅助优化生图提示词\n"
            "  - `/plan <描述>` — 在 Plan 会话中继续交流（群聊普通消息不会被拦截）\n"
            "  - `/plan confirm` — 在 Plan 会话中确认生成\n"
            "  - `/plan retry` — 重试上一条失败的 Plan 输入\n"
            "  - `/plan quit` — 退出当前 Plan 会话\n"
            "  - `/image2 plan confirm` — 在 Plan 中确认生成（自动带参考图）\n"
            "  - `/image2 plan retry` — 重试上一条失败的 Plan 输入\n"
            "  - `/image2 plan quit` — 退出 Plan 会话（`cancel` 也可用）\n"
            "- `/image2 mode [模式]` — 查看/切换全局 API 模式（管理员）\n"
            "- `/image2 guard [images|responses|all] [on|off]` — "
            "查看/切换 Prompt Guard（管理员）\n"
            "- `/image2 retry [global|here|interval] ...` — "
            "查看/切换备用站点重试提示（管理员）\n"
            "- `/image2 providers` — 查看生图站点状态与当前模式可用性（管理员）\n"
            "- `/image2 stats` — 查看 Provider 统计、失败原因、成功率（管理员）\n"
            "- `/image2 stats recent [N]` — 查看最近 N 条失败记录（管理员）\n"
            "- `/image2 diag` — 生成诊断包（管理员）\n"
            "- `/image2 help` — 显示本帮助\n\n"
            "### 当前配置\n\n"
            f"| 项目 | 值 |\n"
            f"|------|------|\n"
            f"| API Key | {api_key_set} |\n"
            f"| Base URL | `{cfg.get('base_url', '未设置')}` |\n"
            f"| 全局 API 模式 | `{current_mode}` |\n"
            f"| 主站点名称 | `{primary_provider_name}` |\n"
            f"| Images 模型 | `{cfg.get('model', 'gpt-image-2')}` |\n"
            f"| Responses 模型 | `{cfg.get('responses_model', 'gpt-5.5')}` |\n"
            f"| Images Prompt Guard | {images_guard_status} |\n"
            f"| Responses Prompt Guard | {responses_guard_status} |\n"
            f"| 权威兜底 | {authoritative_status} |\n"
            f"| 生图 API 站点 | {image_provider_count} 个（当前模式可用 {viable_provider_count} 个） |\n"
            f"| 自适应站点优先级 | {adaptive_status} |\n"
            f"| 备用站点重试提示 | {retry_notice_status}，间隔 {self._format_duration(retry_notice_interval)} |\n"
            f"| edit 别名 | {edit_aliases_status} |\n"
            f"| Plan 模型 | `{cfg.get('plan_model', 'gpt-5.4')}` |\n"
            f"| Plan 空闲超时 | {cfg.get('plan_timeout', 300)} 秒 |\n"
            f"| 图片尺寸 | `{cfg.get('size', 'auto')}` |\n"
            f"| 图片质量 | `{cfg.get('quality', 'auto')}` |\n"
            f"| 输出格式 | `{cfg.get('output_format', 'png')}` |\n"
            f"| 生成数量 n | {cfg.get('n', 1)} |\n"
            f"| 文本回复转图片 | {t2i_status} |\n"
            f"| 保存输出 | {save_status} |\n\n"
            "### 说明\n\n"
            f"- `/image2 mode` 是全局模式，会影响 draw/edit 的站点过滤。\n"
            f"- draw/edit 只会尝试支持当前模式的站点；不支持的站点会被跳过。\n"
            f"- `/image2 retry here off` 可关闭当前群/会话的备用站点切换提示。\n"
            f"- edit 别名只在消息开头匹配，且要求别名后接空白和提示词。\n"
            f"- 站点明细请使用 `/image2 providers` 查看。\n"
            f"- `/image2 plan` 进入后，下面缩进的是 Plan 子命令。"
        )

        yield await self._text_result(event, help_md, action="help")

    @image2.command("mode")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mode(self, event: AstrMessageEvent, mode: str | None = None):
        """查看或切换 API 模式（管理员）"""
        current_mode = self._normalize_api_mode(self.config.get("api_mode", "images"))
        logger.info(
            "[GPTImage2] mode command received "
            f"{self._event_context(event)} current_mode={current_mode} "
            f"requested_mode={mode or '-'}"
        )
        if mode is None or not str(mode).strip():
            yield await self._text_result(
                event,
                "## API 模式\n\n"
                f"当前模式：**`{current_mode}`**\n\n"
                "可用模式：`images` / `responses`\n\n"
                "用法：`/image2 mode <images|responses>`",
                action="mode-help",
            )
            return

        next_mode = str(mode).strip().lower()
        if next_mode not in {"images", "responses"}:
            yield await self._text_result(
                event,
                "## ⚠️ API 模式无效\n\n"
                "可用模式：`images` / `responses`\n\n"
                "用法：`/image2 mode <images|responses>`",
                action="mode-invalid",
            )
            return

        if next_mode == current_mode:
            yield await self._text_result(
                event,
                f"API 模式已经是：**`{current_mode}`**",
                action="mode-unchanged",
            )
            return

        try:
            provider_configs = self._get_image_api_provider_configs()
            provider_count = len(provider_configs)
            viable_provider_count = sum(
                1 for p in provider_configs if p.supports_mode(next_mode)
            )
        except ValueError:
            provider_count = 0
            viable_provider_count = 0

        self.config["api_mode"] = next_mode
        saved = self._save_config()
        logger.info(
            "[GPTImage2] API mode switched "
            f"{self._event_context(event)} from={current_mode} to={next_mode} saved={saved}"
        )
        suffix = "已保存到插件配置。" if saved else "但当前配置对象不支持自动保存。"
        if provider_count == 0:
            availability_note = (
                "\n\n⚠️ 当前还没有配置任何可用站点。请先配置主站点或备用站点。"
            )
        elif viable_provider_count == 0:
            availability_note = (
                f"\n\n⚠️ 当前没有任何站点支持 `{next_mode}` 模式。"
                "请先使用 `/image2 providers` 检查配置。"
            )
        else:
            availability_note = (
                f"\n\n当前模式可用站点：**{viable_provider_count}/{provider_count}**。"
                "更多详情请使用 `/image2 providers`。"
            )
        yield await self._text_result(
            event,
            f"## ✅ API 模式已切换\n\n"
            f"从 **`{current_mode}`** → **`{next_mode}`**\n\n"
            f"{availability_note}\n"
            f"{suffix}",
            action="mode-switched",
        )

    @image2.command("guard")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def guard(
        self,
        event: AstrMessageEvent,
        target: str | None = None,
        state: str | None = None,
    ):
        """查看或切换 Prompt Rewrite Guard（管理员）"""
        target_aliases = {
            "image": "images",
            "images": "images",
            "img": "images",
            "response": "responses",
            "responses": "responses",
            "resp": "responses",
            "all": "all",
            "both": "all",
            "全部": "all",
        }
        current_images = self._prompt_rewrite_guard_enabled("images")
        current_responses = self._prompt_rewrite_guard_enabled("responses")
        status_md = (
            "## Prompt Guard\n\n"
            f"- Images API：{self._prompt_rewrite_guard_status(current_images)}\n"
            f"- Responses API：{self._prompt_rewrite_guard_status(current_responses)}\n\n"
            "用法：`/image2 guard <images|responses|all> <on|off>`"
        )

        logger.info(
            "[GPTImage2] guard command received "
            f"{self._event_context(event)} target={target or '-'} state={state or '-'} "
            f"images={current_images} responses={current_responses}"
        )

        if target is None or not str(target).strip():
            yield await self._text_result(event, status_md, action="guard-help")
            return

        normalized_target = target_aliases.get(str(target).strip().lower())
        if normalized_target is None:
            yield await self._text_result(
                event,
                "## ⚠️ Prompt Guard 目标无效\n\n"
                "可用目标：`images` / `responses` / `all`\n\n"
                "用法：`/image2 guard <images|responses|all> <on|off>`",
                action="guard-invalid-target",
            )
            return

        if state is None or not str(state).strip():
            yield await self._text_result(event, status_md, action="guard-target-help")
            return

        next_value = self._parse_bool_switch(state)
        if next_value is None:
            yield await self._text_result(
                event,
                "## ⚠️ Prompt Guard 状态无效\n\n"
                "可用状态：`on` / `off` / `开启` / `关闭`\n\n"
                "用法：`/image2 guard <images|responses|all> <on|off>`",
                action="guard-invalid-state",
            )
            return

        targets = (
            ["images", "responses"]
            if normalized_target == "all"
            else [normalized_target]
        )
        rows: list[str] = []
        for api_mode in targets:
            key = self._prompt_rewrite_guard_config_key(api_mode)
            old_value = self._prompt_rewrite_guard_enabled(api_mode)
            self.config[key] = next_value
            rows.append(
                f"- {api_mode}：{self._prompt_rewrite_guard_status(old_value)} → "
                f"{self._prompt_rewrite_guard_status(next_value)}"
            )

        saved = self._save_config()
        logger.info(
            "[GPTImage2] prompt guard switched "
            f"{self._event_context(event)} target={normalized_target} "
            f"value={next_value} saved={saved}"
        )
        suffix = "已保存到插件配置。" if saved else "但当前配置对象不支持自动保存。"
        yield await self._text_result(
            event,
            "## ✅ Prompt Guard 已更新\n\n" + "\n".join(rows) + f"\n\n{suffix}",
            action="guard-switched",
        )

    @image2.command("retry")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def retry(
        self,
        event: AstrMessageEvent,
        target: str | None = None,
        state: str | None = None,
    ):
        """查看或切换备用站点重试提示（管理员）"""
        target_text = str(target or "").strip().lower()
        state_text = str(state or "").strip()
        logger.info(
            "[GPTImage2] retry notice command received "
            f"{self._event_context(event)} target={target_text or '-'} "
            f"state={state_text or '-'}"
        )

        if not target_text:
            yield await self._text_result(
                event,
                self._provider_retry_notice_status_text(event),
                action="retry-help",
            )
            return

        if target_text in {"global", "all", "全局"}:
            next_value = self._parse_bool_switch(state_text)
            if next_value is None:
                yield await self._text_result(
                    event,
                    "## ⚠️ 重试提示状态无效\n\n用法：`/image2 retry global <on|off>`",
                    action="retry-global-invalid",
                )
                return
            old_value = self._provider_retry_notice_global_enabled()
            self.config["provider_retry_notice_enabled"] = next_value
            self._provider_retry_notice_state.clear()
            saved = self._save_config()
            suffix = "已保存到插件配置。" if saved else "但当前配置对象不支持自动保存。"
            yield await self._text_result(
                event,
                "## ✅ 全局备用站点重试提示已更新\n\n"
                f"{self._prompt_rewrite_guard_status(old_value)} → "
                f"{self._prompt_rewrite_guard_status(next_value)}\n\n"
                f"{suffix}",
                action="retry-global-switched",
            )
            return

        if target_text in {"here", "group", "session", "当前", "本群"}:
            next_value = self._parse_bool_switch(state_text)
            if next_value is None:
                yield await self._text_result(
                    event,
                    "## ⚠️ 当前会话重试提示状态无效\n\n"
                    "用法：`/image2 retry here <on|off>`",
                    action="retry-here-invalid",
                )
                return
            session_key = self._provider_retry_notice_session_key(event)
            old_value = self._provider_retry_notice_session_enabled(event)
            self._set_provider_retry_notice_session_enabled(session_key, next_value)
            self._provider_retry_notice_state.pop(session_key, None)
            saved = self._save_config()
            suffix = "已保存到插件配置。" if saved else "但当前配置对象不支持自动保存。"
            yield await self._text_result(
                event,
                "## ✅ 当前会话备用站点重试提示已更新\n\n"
                f"- 会话键：`{session_key}`\n"
                f"- 状态：{self._prompt_rewrite_guard_status(old_value)} → "
                f"{self._prompt_rewrite_guard_status(next_value)}\n\n"
                f"{suffix}",
                action="retry-here-switched",
            )
            return

        if target_text in {"interval", "cooldown", "间隔"}:
            try:
                next_interval = max(0, int(state_text))
            except (TypeError, ValueError):
                yield await self._text_result(
                    event,
                    "## ⚠️ 合并提示间隔无效\n\n"
                    "用法：`/image2 retry interval <秒>`，例如 `300` 表示 5 分钟。",
                    action="retry-interval-invalid",
                )
                return
            old_interval = self._provider_retry_notice_interval()
            self.config["provider_retry_notice_interval"] = next_interval
            self._provider_retry_notice_state.clear()
            saved = self._save_config()
            suffix = "已保存到插件配置。" if saved else "但当前配置对象不支持自动保存。"
            yield await self._text_result(
                event,
                "## ✅ 备用站点重试提示间隔已更新\n\n"
                f"{self._format_duration(old_interval)} → "
                f"{self._format_duration(next_interval)}\n\n"
                f"{suffix}",
                action="retry-interval-switched",
            )
            return

        yield await self._text_result(
            event,
            "## ⚠️ 重试提示目标无效\n\n"
            "可用目标：`global` / `here` / `interval`\n\n"
            + self._provider_retry_notice_status_text(event),
            action="retry-invalid-target",
        )

    @image2.command("providers")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def providers(self, event: AstrMessageEvent):
        """显示当前生图站点状态（管理员）"""
        global_mode = self._normalize_api_mode(self.config.get("api_mode", "images"))

        try:
            configs = self._get_image_api_provider_configs()
        except ValueError as e:
            yield await self._text_result(
                event,
                f"## ⚠️ 无法获取站点配置\n\n{str(e)}",
                action="providers-config-error",
            )
            return

        stats = self._load_provider_stats().get("providers", {})
        now = time()

        primary = [c for c in configs if c.role == "primary"]
        normal = [c for c in configs if c.role == "normal"]
        authoritative = [c for c in configs if c.role == "authoritative_fallback"]

        def _mode_status(p: ImageAPIProviderConfig) -> str:
            img = "✅" if p.images_supported else "❌"
            resp = "✅" if p.responses_supported else "❌"
            return f"`images {img} / responses {resp}`"

        def _viable_marker(p: ImageAPIProviderConfig) -> str:
            return "✅" if p.supports_mode(global_mode) else "❌"

        def _health_str(p: ImageAPIProviderConfig) -> str:
            item = stats.get(p.provider_id, {})
            if not isinstance(item, dict):
                item = {}
            success = item.get("success_count", 0)
            failure = item.get("failure_count", 0)
            cooldown_until = item.get("cooldown_until", 0)
            remaining = max(0, int(cooldown_until - now)) if cooldown_until > now else 0
            parts = [f"成功 {success} 次 / 失败 {failure} 次"]
            if remaining > 0:
                parts.append(f"冷却 {remaining}s ⚠️")
            return "，".join(parts)

        def _model_str(p: ImageAPIProviderConfig) -> str:
            parts = []
            if p.model:
                parts.append(f"images=`{p.model}`")
            if p.responses_model:
                parts.append(f"responses=`{p.responses_model}`")
            return "，".join(parts)

        lines: list[str] = [
            "## 📡 生图站点状态\n\n",
            f"全局模式：`{global_mode}`\n\n",
            "---\n\n",
        ]

        if primary:
            lines.append("### ⭐ 主站点（始终优先）\n\n")
            for p in primary:
                lines.append(
                    f"**{p.name}** {_viable_marker(p)} {_mode_status(p)}\n\n"
                    f"- 模型：{_model_str(p)}\n"
                    f"- URL：`{p.base_url}`\n"
                    f"- 健康：{_health_str(p)}\n\n"
                )
        else:
            lines.append("### ⭐ 主站点\n\n（未配置）\n\n")

        if normal:
            lines.append("### 🔄 普通备用站点（按当前优先级排列）\n\n")
            for idx, p in enumerate(normal, start=1):
                cooldown_str = ""
                item = stats.get(p.provider_id, {})
                if isinstance(item, dict):
                    cooldown_until = item.get("cooldown_until", 0)
                    remaining = (
                        max(0, int(cooldown_until - now)) if cooldown_until > now else 0
                    )
                    if remaining > 0:
                        cooldown_str = f" ⚠️冷却 {remaining}s"
                lines.append(
                    f"{idx}. **{p.name}** {_viable_marker(p)} "
                    f"{_mode_status(p)}{cooldown_str}\n\n"
                    f"   模型：{_model_str(p)}\n"
                    f"   URL：`{p.base_url}`\n"
                    f"   健康：{_health_str(p)}\n\n"
                )
        else:
            lines.append("### 🔄 普通备用站点\n\n（无）\n\n")

        if authoritative:
            lines.append("### 🛡️ 权威兜底站点（始终最后）\n\n")
            for p in authoritative:
                lines.append(
                    f"**{p.name}** {_viable_marker(p)} {_mode_status(p)}\n\n"
                    f"- 模型：{_model_str(p)}\n"
                    f"- URL：`{p.base_url}`\n"
                    f"- 健康：{_health_str(p)}\n\n"
                )
        else:
            lines.append("### 🛡️ 权威兜底站点\n\n（未配置）\n\n")

        viable_count = sum(1 for p in configs if p.supports_mode(global_mode))
        lines.append(
            f"---\n\n共 {len(configs)} 个站点，"
            f"当前 `{global_mode}` 模式可用：{viable_count} 个\n\n"
            "`✅` = 当前模式可用  `❌` = 当前模式不可用"
        )

        yield await self._text_result(event, "".join(lines), action="providers")

    # ── Telemetry diagnostics commands ──────────────────────────

    @image2.command("stats")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def stats(self, event: AstrMessageEvent, sub: str | None = None):
        """显示生图站点统计和诊断信息（管理员）"""
        stats_data = self._load_provider_stats()
        providers_data = stats_data.get("providers", {})
        if not isinstance(providers_data, dict):
            providers_data = {}

        # ── Recent JSONL records (unchanged) ──────────────────────
        sub_str = (sub or "").strip().lower()
        if sub_str == "recent" or sub_str.startswith("recent "):
            # Parse optional count
            count = 10
            parts = sub_str.split(maxsplit=1)
            if len(parts) > 1:
                try:
                    count = max(1, min(50, int(parts[1].strip())))
                except (TypeError, ValueError):
                    count = 10
            records = self._read_recent_failure_records_inst(count)
            if not records:
                yield await self._text_result(
                    event,
                    "## 📊 最近失败记录\n\n（无记录）",
                    action="stats-recent-empty",
                )
                return
            lines = [f"## 📊 最近 {len(records)} 条失败记录\n\n"]
            for rec in records:
                ts = rec.get("timestamp", 0)
                ts_str = (
                    _time_module.strftime(
                        "%Y-%m-%d %H:%M:%S", _time_module.localtime(ts)
                    )
                    if ts
                    else "-"
                )
                provider_name = rec.get("provider_name", "-")
                reason = rec.get("reason_key", "unknown")
                action_rec = rec.get("action", "-")
                status = rec.get("status_code", "-")
                ctype = rec.get("response_content_type", "")
                rid = rec.get("request_ids", "")
                preview = rec.get("response_preview", "")
                preview_truncated = rec.get("response_preview_truncated", False)
                resp_bytes = rec.get("response_bytes", "")

                rec_lines = [
                    f"- **{ts_str}** | {provider_name} | "
                    f"{action_rec} | `{reason}` | HTTP {status}"
                ]
                # Show content-type and request IDs when available
                meta_parts = []
                if ctype:
                    meta_parts.append(f"ctype={ctype}")
                if rid and rid != "-":
                    meta_parts.append(f"rid={rid}")
                if resp_bytes != "":
                    meta_parts.append(f"bytes={resp_bytes}")
                if preview_truncated:
                    meta_parts.append("preview_truncated")
                if meta_parts:
                    rec_lines.append(f"  - {', '.join(meta_parts)}")
                # Show response preview when available — clipped to 240 chars
                # for readability in Markdown
                if preview and preview != "repr('')":
                    preview_short = (
                        preview[:240] + "…" if len(preview) > 240 else preview
                    )
                    rec_lines.append(f"  - preview: `{preview_short}`")
                lines.append("\n".join(rec_lines))
            lines.append("\n\n完整记录见 `provider_failures.jsonl`。")
            yield await self._text_result(
                event,
                "".join(lines),
                action="stats-recent",
            )
            return

        # ── Determine provider set ────────────────────────────────
        show_all = sub_str == "all"

        try:
            configs = self._get_image_api_provider_configs()
        except ValueError:
            configs = []
        configured_ids: set[str] = {c.provider_id for c in configs}

        if show_all:
            displayed_providers = providers_data
            scope_tag = "（所有历史记录）"
        else:
            displayed_providers = {
                pid: item
                for pid, item in providers_data.items()
                if pid in configured_ids
            }
            scope_tag = "（当前配置的 Provider）"

        # ── Aggregate from displayed providers ────────────────────
        total_success = 0
        total_failure = 0
        all_reasons: dict[str, int] = {}
        all_codes: dict[str, int] = {}

        for pid, item in displayed_providers.items():
            if not isinstance(item, dict):
                continue
            total_success += self._provider_stat_int(item, "success_count")
            total_failure += self._provider_stat_int(item, "failure_count")

            reasons = item.get("failure_reasons", {})
            if isinstance(reasons, dict):
                for k, v in reasons.items():
                    all_reasons[k] = all_reasons.get(k, 0) + self._provider_stat_int(
                        reasons, k
                    )

            codes = item.get("failure_status_codes", {})
            if isinstance(codes, dict):
                for k, v in codes.items():
                    all_codes[k] = all_codes.get(k, 0) + self._provider_stat_int(
                        codes, k
                    )

        total = total_success + total_failure
        sr_pct = f"{round(total_success / total * 100, 1)}%" if total > 0 else "-"

        lines: list[str] = [
            "## 📊 Provider 生图统计\n\n",
            f"总请求：**{total}** 次 | "
            f"成功：**{total_success}** | "
            f"失败：**{total_failure}** | "
            f"成功率：**{sr_pct}**\n\n",
            f"*{scope_tag}*\n\n",
        ]

        # Top failure reasons
        if all_reasons:
            lines.append("### 🔴 主要失败原因\n\n")
            seen_order = [r for r in self.FAILURE_REASON_ORDER if r in all_reasons]
            seen_rest = [r for r in sorted(all_reasons.keys()) if r not in seen_order]
            for reason in seen_order + seen_rest:
                count_val = all_reasons.get(reason, 0)
                if count_val > 0:
                    lines.append(f"- `{reason}`：{count_val} 次\n")
            lines.append("\n")

        # Top status codes
        if all_codes:
            lines.append("### 🔴 主要失败状态码\n\n")
            for code, count_val in sorted(all_codes.items(), key=lambda x: -x[1]):
                lines.append(f"- HTTP `{code}`：{count_val} 次\n")
            lines.append("\n")

        # ── Per-provider table (sorted by success rate desc) ──────
        if displayed_providers:
            lines.append("### 各站点统计\n\n")
            lines.append(
                "| 站点 | 成功 | 失败 | 成功率 | 模式 | 主要失败原因 | 最近错误 |\n"
                "|------|------|------|--------|------|-------------|----------|\n"
            )

            table_rows: list[tuple[float, str]] = []
            for pid, item in displayed_providers.items():
                if not isinstance(item, dict):
                    continue
                p_name = item.get("name", pid)
                p_success = self._provider_stat_int(item, "success_count")
                p_failure = self._provider_stat_int(item, "failure_count")
                p_total = p_success + p_failure
                p_sr = f"{round(p_success / p_total * 100, 1)}%" if p_total > 0 else "-"
                p_mode = item.get("role", "-")
                p_reasons = item.get("failure_reasons", {})
                top_reason = (
                    max(p_reasons, key=lambda k: p_reasons.get(k, 0))
                    if isinstance(p_reasons, dict) and p_reasons
                    else "-"
                )
                last_err = (
                    self._safe_text_preview(
                        str(item.get("last_error", "") or ""), limit=60
                    )
                    or "-"
                )
                sort_key = p_success / p_total if p_total > 0 else -1.0
                row = (
                    f"| {p_name} | {p_success} | {p_failure} | {p_sr} "
                    f"| {p_mode} | {top_reason} | {last_err} |\n"
                )
                table_rows.append((sort_key, row))

            # Sort descending by success rate
            table_rows.sort(key=lambda x: x[0], reverse=True)
            for _, row in table_rows:
                lines.append(row)
            lines.append("\n")

            # ── Supplementary status-code table ───────────────────
            lines.append("### 各站点状态码分布\n\n")
            lines.append("| 站点 | 200 | 非 200 分布 |\n|------|-----|------------|\n")

            sc_rows: list[tuple[float, str]] = []
            for pid, item in displayed_providers.items():
                if not isinstance(item, dict):
                    continue
                p_name = item.get("name", pid)
                p_success = self._provider_stat_int(item, "success_count")
                p_failure = self._provider_stat_int(item, "failure_count")
                p_total = p_success + p_failure

                p200_pct = (
                    f"{round(p_success / p_total * 100, 1)}%" if p_total > 0 else "-"
                )

                fsc = item.get("failure_status_codes", {})
                if isinstance(fsc, dict) and fsc and p_total > 0:
                    non200_parts: list[str] = []
                    sorted_codes = sorted(fsc.items(), key=lambda x: -x[1])
                    for code, cnt in sorted_codes[:4]:
                        prob = cnt / p_total * 100
                        non200_parts.append(f"{code} {prob:.1f}%")
                    if len(sorted_codes) > 4:
                        non200_parts.append(f"等 {len(sorted_codes)} 类")
                    non200_str = ", ".join(non200_parts)
                elif p_total > 0 and p_failure > 0:
                    non200_str = f"其他 {round(p_failure / p_total * 100, 1)}%"
                else:
                    non200_str = "-"

                sort_key = p_success / p_total if p_total > 0 else -1.0
                sc_rows.append(
                    (
                        sort_key,
                        f"| {p_name} | {p200_pct} | {non200_str} |\n",
                    )
                )

            sc_rows.sort(key=lambda x: x[0], reverse=True)
            for _, row in sc_rows:
                lines.append(row)
            lines.append("\n")

        lines.append(
            "\n---\n`/image2 stats recent [N]` 查看最近失败记录。"
            " `/image2 stats all` 查看全部历史。"
        )
        yield await self._text_result(event, "".join(lines), action="stats")

    @staticmethod
    def _read_recent_failure_records(
        count: int,
        *,
        path: Path | None = None,
    ) -> list[dict]:
        return read_recent_failure_records(count, path=path)

    def _read_recent_failure_records_inst(self, count: int) -> list[dict]:
        return self._provider_manager.read_recent_failure_records_inst(count)

    @image2.command("diag")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def diag(self, event: AstrMessageEvent):
        """生成诊断信息压缩包（管理员）"""
        import zipfile as _zipfile

        plugin_name = getattr(self, "name", self.plugin_name)
        plugin_data_root = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        diag_dir = plugin_data_root / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        timestamp = _time_module.strftime("%Y%m%d-%H%M%S")
        zip_path = diag_dir / f"diag-{plugin_name}-{timestamp}.zip"
        stats_data = self._load_provider_stats()

        try:
            with _zipfile.ZipFile(str(zip_path), "w", _zipfile.ZIP_DEFLATED) as zf:
                # 1. summary.md
                self._write_diag_summary(zf, stats_data)

                # 2. provider_stats.json (redacted - remove API keys from base_url)
                redacted_stats = self._redact_provider_stats(stats_data)
                zf.writestr(
                    "provider_stats.json",
                    json.dumps(redacted_stats, ensure_ascii=False, indent=2),
                )

                # 3. recent failures JSONL (last 100) — always included
                failures_path = self._provider_failures_jsonl_path()
                failures_content = ""
                if failures_path.exists():
                    try:
                        all_lines = failures_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                        recent = all_lines[-100:]
                        failures_content = "\n".join(recent) + "\n" if recent else ""
                    except Exception:
                        pass
                zf.writestr("provider_failures.jsonl", failures_content)

                # 4. config_redacted.json
                self._write_diag_redacted_config(zf)

                # 5. version.txt
                zf.writestr(
                    "version.txt",
                    f"Plugin: {plugin_name}\nVersion: 0.4.1\nGenerated: {timestamp}\n",
                )

            # Try sending as a File component
            try:
                from astrbot.api.message_components import File as AstrFile

                file_size = zip_path.stat().st_size
                yield event.chain_result(
                    [
                        Plain(f"📦 诊断包已生成（{file_size} bytes）："),
                        AstrFile(file=str(zip_path), name=zip_path.name),
                    ]
                )
                logger.info(
                    "[GPTImage2] diagnostic zip sent as File component "
                    f"path={zip_path} size={file_size}"
                )
                return
            except ImportError:
                logger.info(
                    "[GPTImage2] File component not available, "
                    "falling back to text path"
                )
            except Exception as e:
                logger.warning(
                    "[GPTImage2] File component send failed "
                    f"error={type(e).__name__}: {e}"
                )

            # Fallback: return path as text
            yield await self._text_result(
                event,
                f"## 📦 诊断包已生成\n\n路径：`{zip_path}`\n\n"
                f"大小：{zip_path.stat().st_size} bytes\n\n"
                "包含：summary.md, provider_stats.json, "
                "provider_failures.jsonl, config_redacted.json, version.txt",
                action="diag-fallback",
            )
        except Exception as e:
            logger.error(
                "[GPTImage2] diagnostic zip generation failed "
                f"error={type(e).__name__}: {e}\n"
                f"{traceback.format_exc()}"
            )
            yield await self._text_result(
                event,
                "## ⚠️ 诊断包生成失败\n\n"
                f"错误：`{type(e).__name__}: {e}`\n\n"
                "请检查文件权限或磁盘空间。",
                action="diag-error",
            )

    def _write_diag_summary(
        self,
        zf: object,
        stats_data: dict,
    ) -> None:
        """Write summary.md into the diagnostic zip."""
        summary = stats_data.get("summary", {})
        providers = stats_data.get("providers", {})
        lines = [
            "# GPT Image2 诊断摘要\n\n",
            f"生成时间：{_time_module.strftime('%Y-%m-%d %H:%M:%S')}\n\n",
            "---\n\n",
            "## 聚合统计\n\n",
        ]
        if isinstance(summary, dict):
            s_success = self._provider_stat_int(summary, "success_count")
            s_failure = self._provider_stat_int(summary, "failure_count")
            s_total = s_success + s_failure
            s_rate = summary.get("success_rate", 0)
            sr_pct = f"{s_rate * 100:.1f}%" if s_total > 0 else "-"
            lines.extend(
                [
                    f"- 总请求：{s_total}\n",
                    f"- 成功：{s_success}\n",
                    f"- 失败：{s_failure}\n",
                    f"- 成功率：{sr_pct}\n\n",
                ]
            )

            reasons = summary.get("failure_reasons", {})
            if isinstance(reasons, dict) and reasons:
                lines.append("### 失败原因分布\n\n")
                for reason, count2 in sorted(reasons.items(), key=lambda x: -x[1]):
                    lines.append(f"- {reason}: {count2}\n")
                lines.append("\n")

            codes = summary.get("failure_status_codes", {})
            if isinstance(codes, dict) and codes:
                lines.append("### 失败状态码分布\n\n")
                for code, count2 in sorted(codes.items(), key=lambda x: -x[1]):
                    lines.append(f"- HTTP {code}: {count2}\n")
                lines.append("\n")

        lines.append("## 各站点统计\n\n")
        if isinstance(providers, dict):
            for pid, item in providers.items():
                if not isinstance(item, dict):
                    continue
                p_name = item.get("name", pid)
                p_success = self._provider_stat_int(item, "success_count")
                p_failure = self._provider_stat_int(item, "failure_count")
                p_total = p_success + p_failure
                p_rate = f"{round(p_success / p_total * 100, 1)}%" if p_total else "-"
                lines.extend(
                    [
                        f"### {p_name}\n\n",
                        f"- provider_id: {pid}\n",
                        f"- role: {item.get('role', '-')}\n",
                        f"- success_count: {p_success}\n",
                        f"- failure_count: {p_failure}\n",
                        f"- success_rate: {p_rate}\n",
                        f"- last_error: "
                        f"{self._safe_text_preview(str(item.get('last_error', '') or ''), limit=120)}\n\n",
                    ]
                )
        zf.writestr("summary.md", "".join(lines))

    @staticmethod
    def _redact_provider_stats(stats_data: dict) -> dict:
        return redact_provider_stats(stats_data)

    def _write_diag_redacted_config(self, zf: object) -> None:
        """Write a redacted copy of plugin config into the diagnostic zip.

        Uses :func:`redact_config_value` for recursive, pattern-based redaction
        that handles nested dicts/lists, fallback provider strings, JSON-encoded
        values, and URL credentials.
        """
        redacted = redact_config_value(self.config)
        if not isinstance(redacted, dict):
            redacted = {"_error": "redact_config_value returned non-dict"}
        zf.writestr(
            "config_redacted.json",
            json.dumps(redacted, ensure_ascii=False, indent=2),
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
                    "## ⚠️ Plan 会话已失效\n\n请重新使用 `/image2 plan` 进入。",
                    action="plan-confirm-stale",
                )
            else:
                yield await self._text_result(
                    event,
                    "## ⚠️ 没有进行中的 Plan 会话\n\n请先使用 `/image2 plan` 进入。",
                    action="plan-confirm-missing",
                )
            return

        if action in {"retry", "resend", "again", "重试"}:
            if self._has_active_plan_waiter(session_id):
                return
            if session_id in self._plan_sessions:
                self._cleanup_plan(session_id)
                yield await self._text_result(
                    event,
                    "## ⚠️ Plan 会话已失效\n\n请重新使用 `/image2 plan` 进入。",
                    action="plan-retry-stale",
                )
            else:
                yield await self._text_result(
                    event,
                    "## ⚠️ 没有进行中的 Plan 会话\n\n请先使用 `/image2 plan` 进入。",
                    action="plan-retry-missing-external",
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
                "## ✅ 已退出 Plan 会话\n\n"
                "如果没有响应中的 Plan 会话，则当前无需操作。",
                action="plan-quit-external",
            )
            return

        if action:
            yield await self._text_result(
                event,
                "## ⚠️ Plan 子命令无效\n\n"
                "用法：\n"
                "- `/image2 plan` — 进入 Plan\n"
                "- `/plan <描述>` — 在 Plan 会话中继续交流\n"
                "- `/plan confirm` — 确认生成\n"
                "- `/plan retry` — 重试上一条失败的 Plan 输入\n"
                "- `/plan quit` — 退出\n"
                "- `/image2 plan confirm` — 确认生成\n"
                "- `/image2 plan retry` — 重试上一条失败的 Plan 输入\n"
                "- `/image2 plan quit` — 退出",
                action="plan-invalid-action",
            )
            return

        if not bool(self.config.get("plan_enabled", True)):
            yield await self._text_result(
                event,
                "## ⚠️ Plan 模式未启用\n\n请在插件配置中开启 `plan_enabled`。",
                action="plan-disabled",
            )
            return

        plan_config = self._get_plan_config()
        processing_timeout = self._get_plan_processing_timeout(plan_config)
        confirm_processing_timeout = self._get_plan_confirm_processing_timeout(
            plan_config,
        )
        owner_sender_id = event.get_sender_id() or ""

        # 防止同一会话重复进入
        if session_id in self._plan_sessions:
            yield await self._text_result(
                event,
                "## ⚠️ 已有进行中的 Plan 会话\n\n"
                "请先使用 `/plan confirm` 或 `/image2 plan confirm` 生成图片\n"
                "或 `/plan quit` 退出当前会话。",
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
            f"confirm_processing_timeout={confirm_processing_timeout}s "
            f"max_rounds={plan_config.max_rounds} model={plan_config.model}"
        )

        await self._send_text(
            event,
            "## 🧠 已进入 Plan 模式\n\n"
            "群聊中只有带 `/plan` 前缀的消息会进入 Plan 交流，"
            "不带前缀的普通消息会正常发给群友。\n\n"
            "请发送 `/plan <图像需求>`，或在发送参考图时附带 `/plan`，"
            "我会用 Responses API 帮你优化提示词。\n\n"
            f"- 当前参考图：**{len(session.reference_data_urls)}** 张\n"
            f"- 空闲超时：**{plan_config.timeout}** 秒\n\n"
            "发送 `/plan confirm` 或 `/image2 plan confirm` 用当前提示词生成图片。\n"
            "如果模型调用失败，可发送 `/plan retry` 重试上一条 Plan 输入。\n"
            "发送 `/plan quit` 或 `/image2 plan quit` 退出。",
            action="plan-enter",
        )

        async def run_plan_model_round(
            next_event: AstrMessageEvent,
            controller: SessionController,
            session: PlanSession,
            *,
            text: str,
            image_urls_for_message: list[str],
            next_round_count: int,
            reached_max: bool,
            retrying: bool = False,
        ) -> None:
            """Call the Plan model once and keep a retry snapshot on failure."""
            messages = list(session.history)
            if not messages:
                messages.append({"role": "developer", "content": PLAN_SYSTEM_PROMPT})

            user_message = {
                "role": "user",
                "content": self._build_plan_user_content(
                    text,
                    image_urls_for_message,
                ),
            }
            messages.append(user_message)

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

            try:
                reply = await plan_client.plan_responses(
                    messages,
                    model=plan_config.model,
                )
            except RuntimeError as e:
                self._store_plan_retry_snapshot(
                    session,
                    text=text,
                    image_urls=image_urls_for_message,
                    reached_max=reached_max,
                    round_count=next_round_count,
                )
                logger.warning(
                    "[GPTImage2] plan Responses failed "
                    f"{self._event_context(next_event)} round={next_round_count} "
                    f"retrying={retrying} error={e}"
                )
                await self._send_text(
                    next_event,
                    f"## ⚠️ 模型调用失败\n\n`{e}`\n\n"
                    "- 发送 `/plan retry` 重试上一条 Plan 输入\n"
                    "- 或发送 `/plan <内容>` 继续补充\n"
                    "- 或发送 `/plan quit` 退出",
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

            final_prompt = parse_final_prompt(reply)
            final_prompt_zh = parse_final_prompt_zh(reply)
            session.history = messages + [{"role": "assistant", "content": reply}]
            session.round_count = next_round_count
            self._clear_plan_retry_snapshot(session)
            if final_prompt:
                session.final_prompt = final_prompt
                session.final_prompt_zh = final_prompt_zh or final_prompt

            # 构建展示文本：用户说明保持中文，最终 prompt 可中英混合，
            # 需要出现在图中的文字应保留原文。
            display = remove_final_prompt_section(reply)
            if final_prompt:
                summary = display or "我已根据你的描述和参考图整理好图像需求。"
                display = (
                    f"## ✅ 我已整理好图像需求\n\n"
                    f"{summary}\n\n"
                    "完整生成提示词已保存，确认生成时会以合并转发发送。\n"
                    "发送 `/plan confirm` 生成图片，"
                    "或 `/plan quit` 退出。"
                )
            elif reached_max:
                display += (
                    "\n\n**已达到最大对话轮数。**"
                    "但模型还没有给出可确认的最终提示词。"
                    "请使用 `/plan <补充信息>` 继续补充，"
                    "或发送 `/plan quit` 退出。"
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

        @session_waiter(
            timeout=self._waiter_timeout(plan_config.timeout),
            record_history_chains=False,
        )
        async def plan_waiter(
            controller: SessionController,
            next_event: AstrMessageEvent,
        ) -> None:
            raw_text = next_event.message_str.strip()
            text_lower = raw_text.lower()
            plan_input_text = self._extract_plan_input(next_event)
            plan_input_lower = (
                plan_input_text.strip().lower() if plan_input_text is not None else ""
            )
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
            if text_lower in {
                "/image2 plan quit",
                "image2 plan quit",
                "/image2 plan cancel",
                "image2 plan cancel",
            } or plan_input_lower in {"quit", "cancel"}:
                await self._send_text(
                    next_event,
                    "## ✅ 已退出 Plan 模式",
                    action="plan-quit",
                )
                self._cleanup_plan(session_id)
                controller.stop()
                return

            # ── 重试上一条失败的 Plan 输入 ─────────────────────
            if text_lower in {
                "/image2 plan retry",
                "image2 plan retry",
                "/image2 plan resend",
                "image2 plan resend",
                "/image2 plan 重试",
                "image2 plan 重试",
            } or plan_input_lower in {"retry", "resend", "again", "重试"}:
                session = self._plan_sessions.get(session_id)
                if session is None:
                    await self._send_text(
                        next_event,
                        "## ⚠️ Plan 会话已失效\n\n请重新使用 `/image2 plan` 进入。",
                        action="plan-session-missing",
                    )
                    controller.stop()
                    return

                if not self._has_plan_retry_snapshot(session):
                    await self._send_text(
                        next_event,
                        "## ⚠️ 没有可重试的 Plan 输入\n\n"
                        "请使用 `/plan <图像需求>` 继续描述，"
                        "或发送 `/plan quit` 退出。",
                        action="plan-retry-missing",
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

                controller.keep(timeout=processing_timeout, reset_timeout=True)
                self._reset_plan_timeout_watchdog(
                    session_id,
                    next_event.unified_msg_origin,
                    processing_timeout,
                )
                await self._send_processing_ack(
                    next_event,
                    "✅ 正在重试上一条 Plan 输入，请稍候…",
                    action="plan-retry-ack",
                    prefer_image=False,
                )
                await run_plan_model_round(
                    next_event,
                    controller,
                    session,
                    text=session.last_failed_input_text or "",
                    image_urls_for_message=list(session.last_failed_image_urls),
                    next_round_count=(
                        session.last_failed_round_count or session.round_count + 1
                    ),
                    reached_max=session.last_failed_reached_max,
                    retrying=True,
                )
                return

            # ── 确认生成 ────────────────────────────────────
            if (
                text_lower
                in {
                    "/image2 plan confirm",
                    "image2 plan confirm",
                }
                or plan_input_lower == "confirm"
            ):
                session = self._plan_sessions.get(session_id)
                if session and session.final_prompt:
                    self._dedupe_plan_reference_images(session)
                    self._cancel_plan_timeout_watchdog(session)
                    controller.keep(
                        timeout=confirm_processing_timeout,
                        reset_timeout=True,
                    )
                    reference_count = len(session.reference_data_urls)
                    api_mode = self.config.get("api_mode", "images")
                    if reference_count:
                        confirm_ack = (
                            f"✅ 已确认 Plan 提示词，"
                            f"正在使用 {api_mode} 模式携带 {reference_count} 张参考图生成图片，"
                            "请稍候…"
                        )
                    else:
                        confirm_ack = (
                            f"✅ 已确认 Plan 提示词，"
                            f"正在使用 {api_mode} 模式生成图片，请稍候…"
                        )
                    await self._send_plan_final_prompt_forward(next_event, session)
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
                    if self._plan_sessions.get(session_id) is not session:
                        controller.stop()
                        return

                    if chain:
                        await next_event.send(MessageChain(chain=chain))
                        if self._send_copyable_prompt_after_success_enabled():
                            await self._send_plan_copyable_success_command(
                                next_event,
                                session,
                            )
                        self._cleanup_plan(session_id)
                        controller.stop()
                        return

                    await self._send_text(
                        next_event,
                        "## 🔁 Plan 会话已保留\n\n"
                        "本次生图没有成功，但已整理好的完整提示词和参考图仍然保留。\n\n"
                        "- 你可以稍后再次发送 `/plan confirm` 重试生成\n"
                        "- 或发送 `/plan quit` 退出当前 Plan 会话",
                        action="plan-confirm-retry-available",
                    )
                    await self._send_text(
                        next_event,
                        self._build_plan_direct_retry_command_text(session),
                        action="plan-direct-retry-command",
                        prefer_image=False,
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
                else:
                    await self._send_text(
                        next_event,
                        "## ⚠️ 还没有准备好的提示词\n\n"
                        "请使用 `/plan <图像需求>` 继续描述，"
                        "或发送 `/plan quit` 退出。",
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

            # ── 其他 /image2 plan 子命令：拦截提示 ───────────
            if PlanSessionFilter._is_image2_plan_command(text_lower):
                await self._send_text(
                    next_event,
                    "## ⚠️ 当前处于 Plan 会话中\n\n"
                    "请先使用 `/plan confirm` 或 `/image2 plan confirm` 生成图片\n"
                    "或 `/plan retry` 重试上一条失败的 Plan 输入，"
                    "或 `/plan quit` 退出。",
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

            if plan_input_text is None:
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
            text = plan_input_text
            session = self._plan_sessions.get(session_id)
            if session is None:
                await self._send_text(
                    next_event,
                    "## ⚠️ Plan 会话已失效\n\n请重新使用 `/image2 plan` 进入。",
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
                    "## ⚠️ Plan 输入为空\n\n"
                    "请使用 `/plan <描述>` 发送文字，或在发送参考图时附带 `/plan`。",
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

            if new_reference_urls:
                ack_text = (
                    f"✅ 已收到 Plan 输入和 {len(new_reference_urls)} 张参考图，"
                    "正在请求模型整理，请稍候…"
                )
            else:
                ack_text = "✅ 已收到 Plan 输入，正在请求模型整理，请稍候…"
            await self._send_processing_ack(
                next_event,
                ack_text,
                action="plan-input-ack",
                prefer_image=False,
            )

            image_urls_for_message = new_reference_urls
            if not session.history and session.reference_data_urls:
                image_urls_for_message = list(session.reference_data_urls)

            next_round_count = session.round_count + 1
            reached_max = next_round_count >= plan_config.max_rounds
            await run_plan_model_round(
                next_event,
                controller,
                session,
                text=text,
                image_urls_for_message=image_urls_for_message,
                next_round_count=next_round_count,
                reached_max=reached_max,
            )
            return

        try:
            await plan_waiter(event, session_filter=PlanSessionFilter())
        except TimeoutError:
            logger.info(
                "[GPTImage2] plan timeout "
                f"{self._event_context(event)} timeout={plan_config.timeout}s"
            )
            sent = await self._send_proactive_message(
                event.unified_msg_origin,
                "## ⌛ Plan 会话等待超时\n\n已自动退出。",
                action="plan-timeout",
            )
            if not sent:
                await self._send_text(
                    event,
                    "## ⌛ Plan 会话等待超时\n\n已自动退出。",
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
                f"## ⚠️ Plan 模式发生错误\n\n`{e}`",
                action="plan-error",
            )
            if not sent:
                await self._send_text(
                    event,
                    f"## ⚠️ Plan 模式发生错误\n\n`{e}`",
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
                "## ⚠️ 请提供图片描述提示词\n\n用法：`/image2 draw <提示词>`",
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
        async for result in self._handle_edit(
            event, prompt=self._extract_prompt(event, "edit")
        ):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def edit_alias(self, event: AstrMessageEvent):
        """通过配置的别名触发 `/image2 edit`。"""
        matched = self._extract_edit_alias_prompt(event)
        if matched is None:
            return

        prompt, alias = matched
        logger.info(
            "[GPTImage2] edit alias matched "
            f"{self._event_context(event)} alias={alias!r} prompt_len={len(prompt)}"
        )
        async for result in self._handle_edit(event, prompt=prompt):
            yield result
        event.stop_event()

    async def _handle_edit(self, event: AstrMessageEvent, *, prompt: str):
        """Shared implementation for `/image2 edit` and configured aliases."""
        started = perf_counter()
        if not prompt or not prompt.strip():
            logger.info(
                f"[GPTImage2] edit rejected empty prompt {self._event_context(event)}"
            )
            yield await self._text_result(
                event,
                "## ⚠️ 请提供编辑提示词\n\n"
                "用法：`/image2 edit <提示词>`\n\n"
                "请附带图片或引用包含图片的消息。",
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
                "## ⚠️ 没有找到可编辑的图片\n\n"
                "请附带图片，或引用一条包含图片的消息后使用 `/image2 edit <提示词>`。",
                action="edit-no-images",
            )
            return

        params = self._get_params()
        api_mode = self.config.get("api_mode", "images")
        logger.info(
            "[GPTImage2] edit start "
            f"{self._event_context(event)} mode={api_mode} prompt_len={len(prompt)} "
            f"input_images={len(images)} {self._params_summary(params)} "
            f"save_outputs={self.config.get('save_outputs', True)}"
        )

        chain = await self._generate_draw_chain(
            event,
            prompt,
            action="edit",
            reference_images=images,
        )
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
                f"{self._event_context(event)}"
            )

    # ── 回复构建 ────────────────────────────────────────────────

    def _build_image_chain(
        self,
        results: list[ImageResult],
        params: ImageParams,
    ) -> list:
        """构建图片消息链；长 revised_prompt 由合并转发单独发送。"""
        if not results:
            return []

        saved = self._save_results(results, params)
        chain: list = []
        logger.debug(
            "[GPTImage2] building reply chain "
            f"saved_items={len(saved)} result_items={len(results)}"
        )

        for idx, item in enumerate(saved):
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
