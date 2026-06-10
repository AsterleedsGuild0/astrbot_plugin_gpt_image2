"""Provider 状态输出构建工具。

纯函数，用于构建 ``/image2 providers`` 的 Markdown 展示。
不依赖 AstrBot 事件/发送能力。

依赖：
- providers.py（ImageAPIProviderConfig）
"""

from __future__ import annotations

from .manager import ImageAPIProviderConfig


def _format_money(value: object, currency: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return f"{text} {currency}".rstrip()


def build_providers_status_markdown(
    configs: list[ImageAPIProviderConfig],
    stats: dict,
    *,
    global_mode: str,
    now: float,
    billing_stats: dict | None = None,
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

    def _request_policy_str(p: ImageAPIProviderConfig) -> str:
        return "强制单图上游请求" if p.force_single_image_requests else "允许原生 n"

    def _billing_str(p: ImageAPIProviderConfig) -> str:
        stats_root = billing_stats if isinstance(billing_stats, dict) else {}
        providers = (
            stats_root.get("providers", {}) if isinstance(stats_root, dict) else {}
        )
        item = providers.get(p.provider_id, {}) if isinstance(providers, dict) else {}
        item = item if isinstance(item, dict) else {}
        manual_balance_text = ""
        if item.get("balance_source") == "manual_anchor_estimate":
            currency = str(item.get("currency") or "")
            converted = item.get("last_converted_balance")
            if converted is not None:
                manual_balance_text = f"约 {_format_money(converted, currency)}"
            else:
                manual_balance_text = (
                    f"余额数值 {_format_money(item.get('last_balance_after'))}"
                )
            manual_balance_text += "（手动锚点估算）"
        if p.billing is None:
            if manual_balance_text:
                return manual_balance_text
            return "未配置"
        billing_type = p.billing.type
        if billing_type == "fixed":
            text = (
                "fixed"
                f"（成功单张 {_format_money(p.billing.success_cost, p.billing.currency)}"
                f" / 失败单次 {_format_money(p.billing.failure_cost, p.billing.currency)}）"
            )
            return f"{text}，{manual_balance_text}" if manual_balance_text else text
        if billing_type == "total_usage":
            parts = ["total_usage"]
        else:
            parts = ["balance"]
        if p.billing.has_fixed_fallback:
            parts.append(
                "固定参考 "
                f"成功单张 {_format_money(p.billing.success_cost, p.billing.currency)}"
                f" / 失败单次 {_format_money(p.billing.failure_cost, p.billing.currency)}"
            )
        balance = item.get("last_balance_after")
        if balance is not None:
            balance_text = f"余额数值 {_format_money(balance)}"
            if item.get("balance_source") == "manual_anchor_estimate":
                balance_text += "（手动锚点估算）"
            parts.append(balance_text)
        converted = item.get("last_converted_balance")
        if converted is not None:
            parts.append(f"约 {_format_money(converted, p.billing.currency)}")
        total_cost = item.get("total_cost")
        if total_cost is not None:
            parts.append(f"累计 {_format_money(total_cost, p.billing.currency)}")
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
                f"- 请求策略：{_request_policy_str(p)}\n"
                f"- URL：`{p.base_url}`\n"
                f"- 计费：{_billing_str(p)}\n"
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
                f"   请求策略：{_request_policy_str(p)}\n"
                f"   URL：`{p.base_url}`\n"
                f"   计费：{_billing_str(p)}\n"
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
                f"- 请求策略：{_request_policy_str(p)}\n"
                f"- URL：`{p.base_url}`\n"
                f"- 计费：{_billing_str(p)}\n"
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
