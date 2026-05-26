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

from .config_redact import redact_config_value
from .providers import (
    ImageAPIProviderConfig,
    FAILURE_REASON_ORDER,
    provider_stat_int,
    safe_text_preview,
    redact_provider_stats,
)


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

    # 汇总当前展示范围内的 Provider 统计。
    total_success = 0
    total_failure = 0
    all_reasons: dict[str, int] = {}
    all_codes: dict[str, int] = {}

    for _pid, item in displayed_providers.items():
        if not isinstance(item, dict):
            continue
        total_success += provider_stat_int(item, "success_count")
        total_failure += provider_stat_int(item, "failure_count")

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

    lines: list[str] = [
        "## 📊 Provider 生图统计\n\n",
        f"总请求：**{total}** 次 | "
        f"成功：**{total_success}** | "
        f"失败：**{total_failure}** | "
        f"成功率：**{sr_pct}**\n\n",
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

    # 主要失败状态码。
    if all_codes:
        lines.append("### 🔴 主要失败状态码\n\n")
        for code, count_val in sorted(all_codes.items(), key=lambda x: -x[1]):
            lines.append(f"- HTTP `{code}`：{count_val} 次\n")
        lines.append("\n")

    # 各 Provider 表格按成功率降序展示。
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
            p_success = provider_stat_int(item, "success_count")
            p_failure = provider_stat_int(item, "failure_count")
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
                safe_text_preview(str(item.get("last_error", "") or ""), limit=60)
                or "-"
            )
            sort_key = p_success / p_total if p_total > 0 else -1.0
            row = (
                f"| {p_name} | {p_success} | {p_failure} | {p_sr} "
                f"| {p_mode} | {top_reason} | {last_err} |\n"
            )
            table_rows.append((sort_key, row))

        table_rows.sort(key=lambda x: x[0], reverse=True)
        for _, row in table_rows:
            lines.append(row)
        lines.append("\n")

        # 补充展示各 Provider 的状态码分布。
        lines.append("### 各站点状态码分布\n\n")
        lines.append("| 站点 | 200 | 非 200 分布 |\n|------|-----|------------|\n")

        sc_rows: list[tuple[float, str]] = []
        for pid, item in displayed_providers.items():
            if not isinstance(item, dict):
                continue
            p_name = item.get("name", pid)
            p_success = provider_stat_int(item, "success_count")
            p_failure = provider_stat_int(item, "failure_count")
            p_total = p_success + p_failure

            p200_pct = f"{round(p_success / p_total * 100, 1)}%" if p_total > 0 else "-"

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
    plugin_version: str = "0.4.4",
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
