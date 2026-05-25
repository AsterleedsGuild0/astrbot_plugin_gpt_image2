"""Plan message-building pure functions for GPT Image2.

Responsibility:
- Build user content dict for the Responses API
- Format/split prompt text for merged-forward nodes
- Construct Node objects for merged-forward messages
- Build Plan final prompt forward nodes
- Build Plan copyable command text and forward nodes
- Build revised prompt forward nodes and fallback text

This module has no dependency on AstrBot event/send, only on
``astrbot.api.message_components`` (Node, Plain) for constructing
message component trees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api.message_components import Node, Plain

if TYPE_CHECKING:
    from .client import ImageResult
    from .plan import PlanSession


# ── User content for Responses API ───────────────────────────────


def build_plan_user_content(text: str, image_data_urls: list[str]) -> str | list:
    """Build the user message content for the Responses API Plan call."""
    if not image_data_urls:
        return text

    content: list[dict[str, str]] = [{"type": "input_text", "text": text}]
    for data_url in image_data_urls:
        content.append({"type": "input_image", "image_url": data_url})
    return content


# ── Prompt text utilities ────────────────────────────────────────


def single_line_command_prompt(prompt: str) -> str:
    """Collapse a final prompt into one line so it can be copied as a command."""
    return " ".join(str(prompt or "").replace("\x00", " ").split())


def split_text_for_forward(text: str, *, limit: int = 1200) -> list[str]:
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


def forward_node(
    title: str,
    text: str,
    *,
    part: int | None = None,
    total: int | None = None,
) -> Node:
    """Build a single merged-forward Node with the given title and text."""
    suffix = f"（{part}/{total}）" if part is not None and total else ""
    return Node(
        name="GPT Image2",
        uin="233",
        content=[Plain(f"{title}{suffix}\n\n{text}".strip())],
    )


def prompt_text_nodes(title: str, text: str) -> list[Node]:
    """Split *text* into chunks and wrap each in a forward Node."""
    chunks = split_text_for_forward(text)
    total = len(chunks)
    if total <= 1:
        return [forward_node(title, chunks[0] if chunks else "-")]
    return [
        forward_node(title, chunk, part=index, total=total)
        for index, chunk in enumerate(chunks, start=1)
    ]


# ── Plan final prompt forward nodes ──────────────────────────────


def plan_prompt_forward_nodes(session: PlanSession) -> list[Node]:
    """Build merged-forward nodes for Chinese and English final prompts.

    .. note::
        Caller should run ``_dedupe_plan_reference_images(session)`` before
        calling this function if session mutations are expected.
    """
    zh_prompt = (session.final_prompt_zh or "").strip()
    final_prompt = (session.final_prompt or "").strip()

    nodes = [
        forward_node(
            "🧾 Plan 最终提示词",
            "完整提示词已收纳为合并转发，避免长英文提示词刷屏。\n"
            "建议优先复制中文提示词复现；英文/混合提示词保留为完整生成依据。",
        )
    ]

    if zh_prompt:
        nodes.extend(
            prompt_text_nodes(
                "🇨🇳 中文提示词（推荐复制）",
                zh_prompt,
            )
        )
    else:
        nodes.append(
            forward_node(
                "🇨🇳 中文提示词",
                "模型本轮没有返回 FINAL_PROMPT_ZH；请优先使用后续英文/混合提示词。",
            )
        )

    if final_prompt:
        nodes.extend(
            prompt_text_nodes(
                "🌐 英文/混合提示词（完整生成依据）",
                final_prompt,
            )
        )
    return nodes


def plan_final_prompt_fallback_text(session: PlanSession) -> str:
    """Build plain-text fallback for when merged-forward sending fails."""
    zh_prompt = session.final_prompt_zh or "（模型未返回中文提示词）"
    final_prompt = session.final_prompt or "（模型未返回英文/混合提示词）"
    return (
        "## 🧾 将用于生成的完整提示词\n\n"
        "合并转发发送失败，已回退为普通文本。\n\n"
        f"### 中文提示词\n\n{zh_prompt}\n\n"
        f"### 英文/混合提示词\n\n{final_prompt}"
    )


# ── Plan copyable command building ───────────────────────────────


def plan_copyable_prompt(session: PlanSession) -> str:
    """Prefer the exact Chinese prompt for copyable IM commands."""
    return session.final_prompt_zh or session.final_prompt or ""


def _build_plan_copyable_command_text_inner(
    session: PlanSession,
    *,
    succeeded: bool,
) -> str:
    """Build the copyable command text without calling dedupe.

    .. note::
        Caller should run ``_dedupe_plan_reference_images(session)`` before
        calling this if session deduplication is desired.
    """
    prompt = single_line_command_prompt(plan_copyable_prompt(session))
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


def build_plan_copyable_command_text(session: PlanSession, *, succeeded: bool) -> str:
    """Build a plain-text command for reusing a Plan final prompt.

    .. note::
        Does **not** call ``_dedupe_plan_reference_images(session)`` internally.
        If deduplication is needed, run it before calling this function.
    """
    return _build_plan_copyable_command_text_inner(session, succeeded=succeeded)


def build_plan_direct_retry_command_text(session: PlanSession) -> str:
    """Build a plain-text command for retrying outside Plan without re-planning.

    .. note::
        Does **not** call dedupe internally.
    """
    return _build_plan_copyable_command_text_inner(session, succeeded=False)


def plan_copyable_command_forward_nodes(session: PlanSession) -> list[Node]:
    """Build merged-forward nodes for a long copyable command after success.

    .. note::
        Does **not** call dedupe internally.
    """
    text = _build_plan_copyable_command_text_inner(session, succeeded=True)
    title, _, rest = text.partition("\n\n")
    usage_note, _, command = rest.partition("\n\n")

    nodes = [
        forward_node(
            "✅ Plan 生图成功：复用说明",
            usage_note or "可复制后续节点中的命令，在其他地方复用相同提示词。",
        )
    ]
    command_text = command or text
    nodes.extend(prompt_text_nodes(title or "可复制命令", command_text))
    return nodes


# ── Revised prompt forward nodes ─────────────────────────────────


def revised_prompt_forward_nodes(
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
        forward_node(
            "📝 生图提示词 / 模型改写提示词",
            "图片会单独发送；这里收纳模型返回的 revised_prompt，"
            "避免 draw/edit 成功消息被长提示词刷屏。",
        )
    ]
    for index, prompt in prompts:
        nodes.extend(
            prompt_text_nodes(
                f"第 {index} 张图片 revised_prompt（{action}）",
                prompt,
            )
        )
    return nodes


def revised_prompt_fallback_text(results: list[ImageResult]) -> str:
    """Build plain-text fallback for revised prompts when merged-forward fails."""
    items = [
        (index, item.revised_prompt.strip())
        for index, item in enumerate(results, start=1)
        if item.revised_prompt and item.revised_prompt.strip()
    ]
    if not items:
        return ""
    return "\n\n".join(
        f"第 {idx} 张图片 revised_prompt:\n{prompt}" for idx, prompt in items
    )
