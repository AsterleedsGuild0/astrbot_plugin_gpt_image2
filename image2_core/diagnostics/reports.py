"""诊断输出、统计格式化与诊断 zip 构建工具。

纯函数/轻量文件构建函数，不依赖 AstrBot 事件/发送能力。

依赖：
- providers.py（ImageAPIProviderConfig、FAILURE_REASON_ORDER 等工具函数）
- config_redact.py（redact_config_value）
"""

from __future__ import annotations

import json
import time as _time_module
import zipfile
from pathlib import Path

from .redact import redact_config_value
from ..providers.manager import (
    ImageAPIProviderConfig,
    FAILURE_REASON_ORDER,
    provider_stat_int,
    safe_text_preview,
    redact_provider_stats,
)


def _positive_count_items(raw_counts: object) -> list[tuple[str, int]]:
    """从统计字典中提取正整数计数项。"""
    if not isinstance(raw_counts, dict):
        return []
    items: list[tuple[str, int]] = []
    for key in raw_counts:
        count = provider_stat_int(raw_counts, key)
        if count > 0:
            items.append((str(key), count))
    return items


def _failure_reason_sort_key(item: tuple[str, int]) -> tuple[int, int, str]:
    """按计数和预定义原因顺序稳定排序失败原因。"""
    reason, count = item
    try:
        order_index = FAILURE_REASON_ORDER.index(reason)
    except ValueError:
        order_index = len(FAILURE_REASON_ORDER)
    return (order_index, -count, reason)


def _failure_distribution_buckets(
    provider_item: dict, failure_count: int
) -> list[tuple[str, int]]:
    """构建可量化失败分布，忽略早期缺少明细的历史缺口。"""
    failure_count = max(0, failure_count)
    if failure_count <= 0:
        return []

    code_items = _positive_count_items(provider_item.get("failure_status_codes", {}))
    code_items.sort(key=lambda x: (-x[1], x[0]))

    buckets: list[tuple[str, int]] = []
    remaining_failure_count = failure_count
    for code, count in code_items:
        if remaining_failure_count <= 0:
            break
        used = min(count, remaining_failure_count)
        buckets.append((f"HTTP {code}", used))
        remaining_failure_count -= used

    no_status_count = remaining_failure_count
    if no_status_count <= 0:
        return buckets

    reason_items = [
        (reason, count)
        for reason, count in _positive_count_items(
            provider_item.get("failure_reasons", {})
        )
        if not reason.startswith("http_")
    ]
    reason_items.sort(key=_failure_reason_sort_key)

    remaining = no_status_count
    for reason, count in reason_items:
        if remaining <= 0:
            break
        used = min(count, remaining)
        if used > 0:
            buckets.append((reason, used))
            remaining -= used

    return buckets


def _known_unrecorded_http_status_count(provider_item: dict, failure_count: int) -> int:
    """统计已有失败原因但未记录 HTTP 状态的可量化失败数。"""
    return sum(
        count
        for label, count in _failure_distribution_buckets(provider_item, failure_count)
        if not label.startswith("HTTP ")
    )


def _format_failure_distribution(
    provider_item: dict,
    *,
    total_count: int,
    failure_count: int,
    max_parts: int = 8,
) -> str:
    """格式化 Provider 失败分布，只展示可量化失败项。"""
    if total_count <= 0 or failure_count <= 0:
        return "-"

    buckets = _failure_distribution_buckets(provider_item, failure_count)
    if not buckets:
        return "-"
    buckets.sort(key=lambda item: (-item[1], item[0]))

    if len(buckets) > max_parts:
        head = buckets[: max_parts - 1]
        rest_buckets = buckets[max_parts - 1 :]
        rest_count = sum(count for _label, count in rest_buckets)
        buckets = head + [(f"其余 {len(rest_buckets)} 项", rest_count)]

    parts = [f"{label} {count / total_count * 100:.1f}%" for label, count in buckets]
    return ", ".join(parts)


def format_elapsed_ms(elapsed_ms: object) -> str:
    """Format elapsed milliseconds for stats tables, preserving old-file compatibility."""
    if not isinstance(elapsed_ms, (int, float, str)):
        return "-"
    try:
        value = float(elapsed_ms)
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        return "-"
    if value < 1000:
        return f"{int(round(value))}ms"
    return f"{value / 1000:.1f}s"


def _format_money(value: object, currency: str = "") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return f"{text} {currency}".rstrip()


def _aggregate_billing_period_costs(
    events: list[dict] | None,
    *,
    now: float | None = None,
) -> dict[str, dict[str, float]]:
    if not events:
        return {}

    now_value = now if now is not None else _time_module.time()
    local_now = _time_module.localtime(now_value)
    today_start = _time_module.mktime(
        (
            local_now.tm_year,
            local_now.tm_mon,
            local_now.tm_mday,
            0,
            0,
            0,
            local_now.tm_wday,
            local_now.tm_yday,
            local_now.tm_isdst,
        )
    )
    yesterday_start = today_start - 86400
    week_start = now_value - 7 * 86400
    month_start = now_value - 30 * 86400

    result: dict[str, dict[str, float]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        provider_id = str(event.get("provider_id") or "").strip()
        if not provider_id:
            continue
        timestamp_raw = event.get("timestamp")
        cost_raw = event.get("cost")
        if not isinstance(timestamp_raw, (int, float, str)) or not isinstance(
            cost_raw, (int, float, str)
        ):
            continue
        try:
            timestamp = float(timestamp_raw)
            cost = float(cost_raw)
        except (TypeError, ValueError):
            continue
        if cost < 0:
            continue

        item = result.setdefault(
            provider_id,
            {"today": 0.0, "yesterday": 0.0, "week": 0.0, "month": 0.0},
        )
        if timestamp >= today_start:
            item["today"] += cost
        if yesterday_start <= timestamp < today_start:
            item["yesterday"] += cost
        if timestamp >= week_start:
            item["week"] += cost
        if timestamp >= month_start:
            item["month"] += cost
    return result


def build_stats_recent_markdown(records: list[dict]) -> str:
    """构建 ``/image2 stats recent [N]`` 的 Markdown 展示。"""
    if not records:
        return "## 📊 最近失败记录\n\n（无记录）"

    lines = [f"## 📊 最近 {len(records)} 条失败记录\n\n"]
    for rec in records:
        ts = rec.get("timestamp", 0)
        ts_str = (
            _time_module.strftime("%Y-%m-%d %H:%M:%S", _time_module.localtime(ts))
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
        json_summary = rec.get("response_json_summary", "")

        rec_lines = [
            f"- **{ts_str}** | {provider_name} | "
            f"{action_rec} | `{reason}` | HTTP {status}"
        ]
        meta_parts = []
        if ctype:
            meta_parts.append(f"ctype={ctype}")
        if rid and rid != "-":
            meta_parts.append(f"rid={rid}")
        if resp_bytes != "":
            meta_parts.append(f"bytes={resp_bytes}")
        if preview_truncated:
            meta_parts.append("preview_truncated")
        if json_summary:
            meta_parts.append(f"json={json_summary}")
        if meta_parts:
            rec_lines.append(f"  - {', '.join(meta_parts)}")
        if preview and preview != "repr('')":
            preview_short = preview[:240] + "…" if len(preview) > 240 else preview
            rec_lines.append(f"  - preview: `{preview_short}`")
        lines.append("\n".join(rec_lines))
    lines.append("\n\n完整记录见 `provider_failures.jsonl`。")
    return "".join(lines)


def build_stats_summary_markdown(
    stats_data: dict,
    provider_configs: list[ImageAPIProviderConfig],
    *,
    show_all: bool = False,
    billing_stats: dict | None = None,
    billing_events: list[dict] | None = None,
    now: float | None = None,
) -> str:
    """构建 ``/image2 stats``（summary/all）的 Markdown 展示。"""
    providers_data = stats_data.get("providers", {})
    if not isinstance(providers_data, dict):
        providers_data = {}

    configured_ids: set[str] = {c.provider_id for c in provider_configs}

    if show_all:
        displayed_providers = providers_data
        scope_tag = "（所有历史记录）"
    else:
        displayed_providers = {
            pid: item for pid, item in providers_data.items() if pid in configured_ids
        }
        scope_tag = "（当前配置的 Provider）"

    billing_providers: dict = {}
    if isinstance(billing_stats, dict):
        raw_billing_providers = billing_stats.get("providers", {})
        if isinstance(raw_billing_providers, dict):
            billing_providers = raw_billing_providers
    billing_period_costs = _aggregate_billing_period_costs(billing_events, now=now)

    # 汇总当前展示范围内的 Provider 统计。
    total_success = 0
    total_failure = 0
    known_no_status_total = 0
    all_reasons: dict[str, int] = {}
    all_codes: dict[str, int] = {}

    for _pid, item in displayed_providers.items():
        if not isinstance(item, dict):
            continue
        total_success += provider_stat_int(item, "success_count")
        item_failure = provider_stat_int(item, "failure_count")
        total_failure += item_failure
        known_no_status_total += _known_unrecorded_http_status_count(item, item_failure)

        reasons = item.get("failure_reasons", {})
        if isinstance(reasons, dict):
            for k, _v in reasons.items():
                all_reasons[k] = all_reasons.get(k, 0) + provider_stat_int(reasons, k)

        codes = item.get("failure_status_codes", {})
        if isinstance(codes, dict):
            for k, _v in codes.items():
                all_codes[k] = all_codes.get(k, 0) + provider_stat_int(codes, k)

    total = total_success + total_failure
    sr_pct = f"{round(total_success / total * 100, 1)}%" if total > 0 else "-"
    task_summary = stats_data.get("task_summary", {})
    task_avg = "-"
    if isinstance(task_summary, dict):
        task_avg = format_elapsed_ms(task_summary.get("success_elapsed_ms_avg"))

    lines: list[str] = [
        "## 📊 Provider 生图统计\n\n",
        f"总请求：**{total}** 次 | "
        f"成功：**{total_success}** | "
        f"失败：**{total_failure}** | "
        f"成功率：**{sr_pct}** | "
        f"平均任务完成耗时：**{task_avg}**\n\n",
        f"*{scope_tag}*\n\n",
    ]

    # 主要失败原因。
    if all_reasons:
        lines.append("### 🔴 主要失败原因\n\n")
        seen_order = [r for r in FAILURE_REASON_ORDER if r in all_reasons]
        seen_rest = [r for r in sorted(all_reasons.keys()) if r not in seen_order]
        for reason in seen_order + seen_rest:
            count_val = all_reasons.get(reason, 0)
            if count_val > 0:
                lines.append(f"- `{reason}`：{count_val} 次\n")
        lines.append("\n")

    # 主要 HTTP 失败状态码；未记录 HTTP 状态只统计已有失败原因明细的部分。
    if all_codes or known_no_status_total > 0:
        lines.append("### 主要 HTTP 失败状态码\n\n")
        for code, count_val in sorted(all_codes.items(), key=lambda x: -x[1]):
            lines.append(f"- HTTP `{code}`：{count_val} 次\n")
        if known_no_status_total > 0:
            lines.append(f"- 未记录 HTTP 状态：{known_no_status_total} 次\n")
        lines.append("\n")

    # 各 Provider 表格按成功率降序展示，拆成多张窄表，避免卡片横向过宽。
    if displayed_providers:
        provider_rows: list[tuple[float, dict]] = []
        for pid, item in displayed_providers.items():
            if not isinstance(item, dict):
                continue
            p_name = item.get("name", pid)
            p_success = provider_stat_int(item, "success_count")
            p_failure = provider_stat_int(item, "failure_count")
            p_total = p_success + p_failure
            p_sr = f"{round(p_success / p_total * 100, 1)}%" if p_total > 0 else "-"
            p_success_avg = format_elapsed_ms(item.get("success_elapsed_ms_avg"))
            p_failure_avg = format_elapsed_ms(item.get("failure_elapsed_ms_avg"))
            p_mode = item.get("role", "-")
            p_reasons = item.get("failure_reasons", {})
            top_reason = (
                max(p_reasons, key=lambda k: p_reasons.get(k, 0))
                if isinstance(p_reasons, dict) and p_reasons
                else "-"
            )
            raw_last_error = str(item.get("last_error", "") or "")
            last_err = (
                safe_text_preview(raw_last_error, limit=60) if raw_last_error else "-"
            )
            sort_key = p_success / p_total if p_total > 0 else -1.0
            billing_item = billing_providers.get(pid, {})
            if not isinstance(billing_item, dict):
                billing_item = {}
            currency = str(billing_item.get("currency") or "")
            balance = _format_money(
                billing_item.get("last_converted_balance"), currency
            )
            raw_balance = _format_money(billing_item.get("last_balance_after"), "")
            if balance == "-" and raw_balance != "-":
                balance = f"余额数值 {raw_balance}"
            if (
                balance != "-"
                and billing_item.get("balance_source") == "manual_anchor_estimate"
            ):
                balance = f"{balance}（手动锚点估算）"
            total_cost = _format_money(billing_item.get("total_cost"), currency)
            period_costs = billing_period_costs.get(pid, {})
            has_billing = bool(billing_item)
            today_cost = _format_money(
                period_costs.get("today", 0.0) if has_billing else None,
                currency,
            )
            yesterday_cost = _format_money(
                period_costs.get("yesterday", 0.0) if has_billing else None,
                currency,
            )
            week_cost = _format_money(
                period_costs.get("week", 0.0) if has_billing else None,
                currency,
            )
            month_cost = _format_money(
                period_costs.get("month", 0.0) if has_billing else None,
                currency,
            )
            provider_rows.append(
                (
                    sort_key,
                    {
                        "name": p_name,
                        "success": p_success,
                        "failure": p_failure,
                        "success_rate": p_sr,
                        "success_avg": p_success_avg,
                        "failure_avg": p_failure_avg,
                        "mode": p_mode,
                        "top_reason": top_reason,
                        "last_error": last_err,
                        "balance": balance,
                        "total_cost": total_cost,
                        "today_cost": today_cost,
                        "yesterday_cost": yesterday_cost,
                        "week_cost": week_cost,
                        "month_cost": month_cost,
                    },
                )
            )

        provider_rows.sort(key=lambda x: x[0], reverse=True)

        lines.append("### 各站点概览\n\n")
        lines.append(
            "| 站点 | 成功 | 失败 | 成功率 | 模式 |\n"
            "|------|------|------|--------|------|\n"
        )
        for _, row in provider_rows:
            lines.append(
                f"| {row['name']} | {row['success']} | {row['failure']} | "
                f"{row['success_rate']} | {row['mode']} |\n"
            )
        lines.append("\n")

        lines.append("### 各站点耗时\n\n")
        lines.append(
            "| 站点 | 平均成功耗时 | 平均失败耗时 |\n"
            "|------|--------------|--------------|\n"
        )
        for _, row in provider_rows:
            lines.append(
                f"| {row['name']} | {row['success_avg']} | {row['failure_avg']} |\n"
            )
        lines.append("\n")

        lines.append("### 各站点余额\n\n")
        lines.append("| 站点 | 缓存余额 |\n|------|----------|\n")
        for _, row in provider_rows:
            lines.append(f"| {row['name']} | {row['balance']} |\n")
        lines.append("\n")

        lines.append("### 各站点费用周期\n\n")
        lines.append(
            "| 站点 | 累计开销 | 今日 | 昨日 | 近7天 | 近30天 |\n"
            "|------|----------|------|------|-------|--------|\n"
        )
        for _, row in provider_rows:
            lines.append(
                f"| {row['name']} | {row['total_cost']} | "
                f"{row['today_cost']} | {row['yesterday_cost']} | "
                f"{row['week_cost']} | {row['month_cost']} |\n"
            )
        lines.append("\n")

        lines.append("### 各站点失败摘要\n\n")
        lines.append(
            "| 站点 | 主要失败原因 | 最近错误 |\n|------|--------------|----------|\n"
        )
        for _, row in provider_rows:
            lines.append(
                f"| {row['name']} | {row['top_reason']} | {row['last_error']} |\n"
            )
        lines.append("\n")

        # 补充展示各 Provider 的响应/失败分布。
        lines.append("### 各站点响应分布\n\n")
        lines.append("| 站点 | 成功 | 失败分布 |\n|------|------|----------|\n")

        sc_rows: list[tuple[float, str]] = []
        for pid, item in displayed_providers.items():
            if not isinstance(item, dict):
                continue
            p_name = item.get("name", pid)
            p_success = provider_stat_int(item, "success_count")
            p_failure = provider_stat_int(item, "failure_count")
            p_total = p_success + p_failure

            p200_pct = f"{round(p_success / p_total * 100, 1)}%" if p_total > 0 else "-"

            failure_distribution = _format_failure_distribution(
                item,
                total_count=p_total,
                failure_count=p_failure,
            )

            sort_key = p_success / p_total if p_total > 0 else -1.0
            sc_rows.append(
                (
                    sort_key,
                    f"| {p_name} | {p200_pct} | {failure_distribution} |\n",
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
    return "".join(lines)


def build_diag_summary_markdown(stats_data: dict) -> str:
    """构建诊断包中 summary.md 的 Markdown 内容。"""
    summary = stats_data.get("summary", {})
    providers = stats_data.get("providers", {})
    lines = [
        "# GPT Image2 诊断摘要\n\n",
        f"生成时间：{_time_module.strftime('%Y-%m-%d %H:%M:%S')}\n\n",
        "---\n\n",
        "## 聚合统计\n\n",
    ]
    if isinstance(summary, dict):
        s_success = provider_stat_int(summary, "success_count")
        s_failure = provider_stat_int(summary, "failure_count")
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

        code_items = _positive_count_items(summary.get("failure_status_codes", {}))
        known_no_status_total = 0
        if isinstance(providers, dict):
            for item in providers.values():
                if not isinstance(item, dict):
                    continue
                known_no_status_total += _known_unrecorded_http_status_count(
                    item,
                    provider_stat_int(item, "failure_count"),
                )
        if code_items or known_no_status_total > 0:
            lines.append("### HTTP 失败状态码分布\n\n")
            for code, count2 in sorted(code_items, key=lambda x: -x[1]):
                lines.append(f"- HTTP {code}: {count2}\n")
            if known_no_status_total > 0:
                lines.append(f"- 未记录 HTTP 状态: {known_no_status_total}\n")
            lines.append("\n")

    lines.append("## 各站点统计\n\n")
    if isinstance(providers, dict):
        for pid, item in providers.items():
            if not isinstance(item, dict):
                continue
            p_name = item.get("name", pid)
            p_success = provider_stat_int(item, "success_count")
            p_failure = provider_stat_int(item, "failure_count")
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
                    f"{safe_text_preview(str(item.get('last_error', '') or ''), limit=120)}\n\n",
                ]
            )
    return "".join(lines)


def build_diag_redacted_config_json(config: dict) -> str:
    """构建脱敏配置的 JSON 字符串，用于诊断包中的 config_redacted.json。"""
    redacted = redact_config_value(config)
    if not isinstance(redacted, dict):
        redacted = {"_error": "redact_config_value returned non-dict"}
    return json.dumps(redacted, ensure_ascii=False, indent=2)


def build_diag_zip(
    zip_path: Path,
    *,
    stats_data: dict,
    config: dict,
    failures_path: Path,
    plugin_name: str = "astrbot_plugin_gpt_image2",
    plugin_version: str = "0.5.0",
    generated_at: str | None = None,
) -> Path:
    """构建诊断 zip 包。

    写入以下文件到 zip：
    - summary.md
    - provider_stats.json（脱敏后的统计数据）
    - provider_failures.jsonl（最近 100 条）
    - config_redacted.json（脱敏后的配置）
    - version.txt

    Args:
        zip_path: 目标 zip 文件路径。
        stats_data: 原始统计数据字典。
        config: 插件配置（将被脱敏后写入）。
        failures_path: provider_failures.jsonl 文件路径。
        plugin_name: 插件名称，用于 version.txt。
        plugin_version: 插件版本号，用于 version.txt。
        generated_at: 生成时间戳，和 zip 文件名保持一致；为空时现场生成。

    Returns:
        成功时返回 zip_path。

    Raises:
        OSError, zipfile.BadZipFile 等：构建失败时向上层传播。
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. summary.md
        summary_md = build_diag_summary_markdown(stats_data)
        zf.writestr("summary.md", summary_md)

        # 2. provider_stats.json（脱敏，移除 base_url 中可能携带的密钥）
        redacted_stats = redact_provider_stats(stats_data)
        zf.writestr(
            "provider_stats.json",
            json.dumps(redacted_stats, ensure_ascii=False, indent=2),
        )

        # 3. 最近失败 JSONL（最多 100 行）
        failures_content = ""
        if failures_path.exists():
            try:
                all_lines = failures_path.read_text(encoding="utf-8").splitlines()
                recent = all_lines[-100:]
                failures_content = "\n".join(recent) + "\n" if recent else ""
            except Exception:
                pass
        zf.writestr("provider_failures.jsonl", failures_content)

        # 4. config_redacted.json
        zf.writestr("config_redacted.json", build_diag_redacted_config_json(config))

        # 5. version.txt
        timestamp = generated_at or _time_module.strftime("%Y%m%d-%H%M%S")
        zf.writestr(
            "version.txt",
            f"Plugin: {plugin_name}\nVersion: {plugin_version}\nGenerated: {timestamp}\n",
        )

    return zip_path
