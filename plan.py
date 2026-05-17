"""GPT Image2 插件 - Plan 模式定义与工具

提供 PlanSession、PlanConfig 数据类，以及 FINAL_PROMPT 解析工具。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PlanSession:
    """单个 Plan 会话状态"""

    owner_sender_id: str = ""
    final_prompt: Optional[str] = None
    round_count: int = 0
    history: list[dict] = field(default_factory=list)
    reference_images: list = field(default_factory=list)
    reference_data_urls: list[str] = field(default_factory=list)
    timeout_task: asyncio.Task | None = None
    timeout_generation: int = 0


@dataclass
class PlanConfig:
    """Plan 模式配置提取"""

    enabled: bool
    model: str
    timeout: int
    max_rounds: int
    use_custom_api: bool
    base_url: str | None
    api_key: str | None

    @classmethod
    def from_config(cls, config: dict) -> "PlanConfig":
        enabled = bool(config.get("plan_enabled", True))
        model = str(config.get("plan_model", "gpt-5.4"))
        try:
            timeout = max(30, int(config.get("plan_timeout", 300)))
        except (TypeError, ValueError):
            timeout = 300
        try:
            max_rounds = max(1, int(config.get("plan_max_rounds", 6)))
        except (TypeError, ValueError):
            max_rounds = 6
        use_custom_api = bool(config.get("plan_use_custom_api", False))
        base_url = config.get("plan_base_url") or None
        api_key = config.get("plan_api_key") or None
        return cls(
            enabled=enabled,
            model=model,
            timeout=timeout,
            max_rounds=max_rounds,
            use_custom_api=use_custom_api,
            base_url=base_url,
            api_key=api_key,
        )


PLAN_SYSTEM_PROMPT = (
    "You are a professional visual prompt engineer. "
    "Your role is to help the user craft the best possible image generation prompt. "
    "The user may provide text and reference images. Treat reference images as "
    "important visual context for subject identity, style, composition, colors, "
    "materials, mood, or editing intent.\n\n"
    "Critical tool rule: You are in a planning-only conversation. You must not "
    "call image_generation, create images, invoke tools, or return any tool call. "
    "Return text only. The actual image generation will happen later after the "
    "user sends /plan confirm or /image2 plan confirm.\n\n"
    "Rules:\n"
    "1. Only return textual planning content. Never trigger image generation or "
    "any tool call in Plan mode.\n"
    "2. Communicate with the user in Simplified Chinese. Ask clarifying questions "
    "in Chinese if the user's description is vague or incomplete.\n"
    "3. Keep all user-facing explanations, summaries, and next-step guidance in "
    "Simplified Chinese.\n"
    "4. During intermediate planning, only expose Chinese clarification questions, "
    "Chinese summaries, and Chinese checklist items to the user. Do not expose the "
    "full final image generation prompt in the normal conversation text.\n"
    "5. When you are ready to finalize, first write a concise Chinese summary "
    "for the user under the heading `中文摘要：`, explaining the key subject, "
    "reference-image usage, composition, style, and constraints.\n"
    "6. After the Chinese summary, still include the final image generation prompt "
    "inside a clear "
    "FINAL_PROMPT section.\n"
    "7. Use the following format:\n"
    "中文摘要：\n"
    "- 用中文列出关键需求摘要。\n"
    "[FINAL_PROMPT]\n"
    "...your final image generation prompt here...\n"
    "[/FINAL_PROMPT]\n"
    "8. You may also use the inline format: [FINAL_PROMPT: ...]\n"
    "9. Keep your Chinese clarifying responses concise but helpful.\n"
    "10. The content inside FINAL_PROMPT may use English, Chinese, or a mixed "
    "Chinese-English prompt, depending on what best preserves the user's intent. "
    "Do not force everything into English.\n"
    "11. For general visual description, you may use concise image-generation terms "
    "in English, Chinese, or mixed language.\n"
    "12. If any text, title, sign, UI copy, caption, label, logo text, or visible "
    "characters should appear in the image, preserve that text exactly in its "
    "original language and characters. Do not translate, romanize, rewrite, "
    "summarize, or replace it. Quote the exact visible text, for example: "
    "必须保留文字：「正在观察猪」.\n"
    "13. If reference images are provided, the final prompt should explicitly say "
    "how to use them, for example preserving identity, following style, or using "
    "them as visual references."
)

_FINAL_PROMPT_BLOCK_RE = re.compile(
    r"\[FINAL_PROMPT\]\s*(.*?)\s*\[/FINAL_PROMPT\]",
    re.DOTALL,
)
_FINAL_PROMPT_INLINE_RE = re.compile(
    r"\[FINAL_PROMPT:\s*(.*?)\s*\]",
)


def parse_final_prompt(text: str) -> str | None:
    """从模型回复中解析 FINAL_PROMPT 内容。

    支持两种格式：
      [FINAL_PROMPT]
      ...prompt...
      [/FINAL_PROMPT]
      [FINAL_PROMPT: ...prompt...]
    """
    m = _FINAL_PROMPT_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _FINAL_PROMPT_INLINE_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def remove_final_prompt_section(text: str) -> str:
    """移除 FINAL_PROMPT 内容，只保留用户可读的中文说明。"""
    text = _FINAL_PROMPT_BLOCK_RE.sub("", text)
    text = _FINAL_PROMPT_INLINE_RE.sub("", text)
    return text.strip()
