"""Markdown builders for billing commands."""

from __future__ import annotations

import time as _time_module

from .tracker import BillingObservation


def format_money(value: object, currency: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(number) < 0.000001:
        number = 0.0
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return f"{text} {currency}".rstrip()


def format_observation_cost(obs: BillingObservation | None) -> str:
    if obs is None:
        return ""
    if obs.cost is None:
        return "，开销：未知"
    return f"，开销：{format_money(obs.cost, obs.currency)}"


def build_costs_summary_markdown(stats: dict) -> str:
    providers = stats.get("providers", {}) if isinstance(stats, dict) else {}
    providers = providers if isinstance(providers, dict) else {}
    summary = stats.get("summary", {}) if isinstance(stats, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    lines = ["## 💰 生图费用统计\n\n"]
    totals = summary.get("totals_by_currency", {})
    if isinstance(totals, dict) and totals:
        total_text = " / ".join(format_money(v, k) for k, v in sorted(totals.items()))
    else:
        total_text = format_money(summary.get("total_cost"), "")
    lines.append(
        f"- 累计已知开销：**{total_text}**\n"
        f"- 已知费用事件：**{summary.get('cost_count', 0)}**\n"
        f"- 未知费用事件：**{summary.get('unknown_count', 0)}**\n\n"
    )
    if not providers:
        lines.append("（暂无费用记录）")
        return "".join(lines)
    lines.append(
        "| 站点 | 类型 | 已知开销 | 事件 | 最近余额 |\n|------|------|----------|------|----------|\n"
    )
    for item in providers.values():
        if not isinstance(item, dict):
            continue
        name = item.get("provider_name") or item.get("name") or "-"
        currency = str(item.get("currency") or "")
        balance_unit = str(item.get("balance_unit") or "")
        balance = item.get("last_balance_after")
        converted = item.get("last_converted_balance")
        balance_text = format_money(balance, balance_unit)
        if converted is not None:
            balance_text += f"（约 {format_money(converted, currency)}）"
        lines.append(
            f"| {name} | {item.get('billing_type', '-')} | "
            f"{format_money(item.get('total_cost'), currency)} | "
            f"{item.get('event_count', 0)} | {balance_text} |\n"
        )
    return "".join(lines)


def build_costs_recent_markdown(records: list[dict]) -> str:
    if not records:
        return "## 💰 最近费用事件\n\n（无记录）"
    lines = [f"## 💰 最近 {len(records)} 条费用事件\n\n"]
    for rec in records:
        ts = rec.get("timestamp", 0)
        ts_str = (
            _time_module.strftime("%Y-%m-%d %H:%M:%S", _time_module.localtime(ts))
            if ts
            else "-"
        )
        status = "成功" if rec.get("success") is True else "失败"
        units = rec.get("cost_units")
        units_text = f" | {units} 张" if rec.get("success") is True and units else ""
        source = str(rec.get("cost_source") or "")
        source_text = f" | {source}" if source else ""
        lines.append(
            f"- **{ts_str}** | {rec.get('provider_name', '-')} | {status} | "
            f"{rec.get('billing_type', '-')} | "
            f"{format_money(rec.get('cost'), str(rec.get('currency') or ''))}"
            f"{units_text}{source_text}\n"
        )
    lines.append("\n完整记录见 `billing_events.jsonl`。")
    return "".join(lines)


def build_balance_markdown(observations: list[BillingObservation]) -> str:
    lines = ["## 💳 生图站点余额\n\n"]
    if not observations:
        lines.append("（当前没有配置余额观测站点）")
        return "".join(lines)
    for obs in observations:
        if obs.balance_after is None:
            lines.append(f"- **{obs.provider_name}**：查询失败\n")
            continue
        converted = obs.converted_balance_after
        if obs.raw_balance_after is not None:
            raw_text = format_money(obs.raw_balance_after, "raw")
        else:
            raw_text = "-"
        lines.append(
            f"- **{obs.provider_name}**：{format_money(obs.balance_after, obs.balance_unit)}"
            f"（约 {format_money(converted, obs.currency)}，原始值 {raw_text}）\n"
        )
    return "".join(lines)
