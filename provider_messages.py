"""Provider 状态输出构建工具。

纯函数，用于构建 ``/image2 providers`` 的 Markdown 展示。
不依赖 AstrBot 事件/发送能力。

依赖：
- providers.py（ImageAPIProviderConfig）
"""

from __future__ import annotations

from .providers import ImageAPIProviderConfig


def build_providers_status_markdown(
    configs: list[ImageAPIProviderConfig],
    stats: dict,
    *,
    global_mode: str,
    now: float,
) -> str:
    """构建 ``/image2 providers`` 命令的 Markdown 状态展示。"""
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

    return "".join(lines)
