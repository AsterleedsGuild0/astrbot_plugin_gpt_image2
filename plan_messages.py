"""GPT Image2 的 Plan 消息构建纯函数。

职责：
- 构建 Responses API 的用户输入内容。
- 格式化/切分提示词文本，便于合并转发。
- 构建合并转发消息节点。
- 构建 Plan 最终提示词转发节点。
- 构建 Plan 可复制命令文本和转发节点。
- 构建 revised_prompt 转发节点和回退文本。

本模块不依赖 AstrBot event/send，只依赖 ``astrbot.api.message_components``
中的 Node/Plain 来构建消息组件树。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api.message_components import Node, Plain

if TYPE_CHECKING:
    from .client import ImageResult
    from .plan import PlanSession


# ── Responses API 用户输入内容 ───────────────────────────────────


def build_plan_user_content(text: str, image_data_urls: list[str]) -> str | list:
    """构建 Plan 调用 Responses API 时的用户消息内容。"""
    if not image_data_urls:
        return text

    content: list[dict[str, str]] = [{"type": "input_text", "text": text}]
    for data_url in image_data_urls:
        content.append({"type": "input_image", "image_url": data_url})
    return content


# ── 提示词文本工具 ───────────────────────────────────────────────


def single_line_command_prompt(prompt: str) -> str:
    """把最终提示词压成单行，便于复制为聊天命令。"""
    return " ".join(str(prompt or "").replace("\x00", " ").split())


def split_text_for_forward(text: str, *, limit: int = 1200) -> list[str]:
    """把长提示词切分为适合合并转发的文本块。"""
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
    """使用标题和正文构建单个合并转发节点。"""
    suffix = f"（{part}/{total}）" if part is not None and total else ""
    return Node(
        name="GPT Image2",
        uin="233",
        content=[Plain(f"{title}{suffix}\n\n{text}".strip())],
    )


def prompt_text_nodes(title: str, text: str) -> list[Node]:
    """把文本切块，并分别包装为合并转发节点。"""
    chunks = split_text_for_forward(text)
    total = len(chunks)
    if total <= 1:
        return [forward_node(title, chunks[0] if chunks else "-")]
    return [
        forward_node(title, chunk, part=index, total=total)
        for index, chunk in enumerate(chunks, start=1)
    ]


# ── Plan 最终提示词转发节点 ──────────────────────────────────────


def plan_prompt_forward_nodes(session: PlanSession) -> list[Node]:
    """构建中文和英文/混合最终提示词的合并转发节点。

    .. note::
        如需修改 session 去重，调用方应先执行
        ``_dedupe_plan_reference_images(session)``。
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
    """构建最终提示词合并转发失败时的纯文本回退内容。"""
    zh_prompt = session.final_prompt_zh or "（模型未返回中文提示词）"
    final_prompt = session.final_prompt or "（模型未返回英文/混合提示词）"
    return (
        "## 🧾 将用于生成的完整提示词\n\n"
        "合并转发发送失败，已回退为普通文本。\n\n"
        f"### 中文提示词\n\n{zh_prompt}\n\n"
        f"### 英文/混合提示词\n\n{final_prompt}"
    )


# ── Plan 可复制命令构建 ──────────────────────────────────────────


def plan_copyable_prompt(session: PlanSession) -> str:
    """构建可复制 IM 命令时优先使用中文提示词。"""
    return session.final_prompt_zh or session.final_prompt or ""


def _build_plan_copyable_command_text_inner(
    session: PlanSession,
    *,
    succeeded: bool,
) -> str:
    """构建可复制命令文本；内部不执行参考图去重。

    .. note::
        如需对 session 参考图去重，调用方应先执行
        ``_dedupe_plan_reference_images(session)``。
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
    """构建复用 Plan 最终提示词的纯文本命令。

    .. note::
        内部不会调用 ``_dedupe_plan_reference_images(session)``。
        如需去重，请在调用前完成。
    """
    return _build_plan_copyable_command_text_inner(session, succeeded=succeeded)


def build_plan_direct_retry_command_text(session: PlanSession) -> str:
    """构建跳过重新规划、在 Plan 外直接重试的纯文本命令。

    .. note::
        内部不会执行参考图去重。
    """
    return _build_plan_copyable_command_text_inner(session, succeeded=False)


def plan_copyable_command_forward_nodes(session: PlanSession) -> list[Node]:
    """构建 Plan 成功后长可复制命令的合并转发节点。

    .. note::
        内部不会执行参考图去重。
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


# ── revised_prompt 转发节点 ──────────────────────────────────────


def revised_prompt_forward_nodes(
    results: list[ImageResult],
    *,
    action: str,
) -> list[Node]:
    """为图片 API 返回的长 revised_prompt 构建合并转发节点。"""
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
    """构建 revised_prompt 合并转发失败时的纯文本回退内容。"""
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
