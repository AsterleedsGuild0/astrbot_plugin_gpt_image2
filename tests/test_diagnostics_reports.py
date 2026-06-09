"""诊断统计报表测试。"""

from __future__ import annotations

import sys
import time as time_module
import unittest
from unittest.mock import MagicMock

# 测试环境没有 AstrBot 运行时，导入 provider/report 前先 mock astrbot。
astrbot_api = MagicMock()
astrbot_api.logger = MagicMock()
sys.modules["astrbot"] = MagicMock()
sys.modules["astrbot.api"] = astrbot_api

from image2_core.api.client import HTTPDiagnostics, ImageAPIError  # noqa: E402
from image2_core.diagnostics.reports import (  # noqa: E402
    build_stats_summary_markdown,
    format_elapsed_ms,
)
from image2_core.providers.manager import (  # noqa: E402
    ImageAPIProviderConfig,
    classify_failure_reason,
    classify_http_status_code,
    diagnostic_http_status_code,
)


def _provider(provider_id: str = "pid-a") -> ImageAPIProviderConfig:
    """构造报表测试用 Provider 配置。"""
    return ImageAPIProviderConfig(
        name="站点A",
        api_key="",
        base_url="https://example.com/v1",
        model="gpt-image-2",
        responses_model="gpt-5.5",
        provider_id=provider_id,
        configured_order=0,
    )


class TestStatsSummaryMarkdown(unittest.TestCase):
    """验证 stats summary 的失败分布口径。"""

    def test_failure_distribution_includes_no_status_reasons(self):
        """响应分布同时展示 HTTP 状态码和超时等无状态失败。"""
        stats_data = {
            "providers": {
                "pid-a": {
                    "name": "站点A",
                    "role": "normal",
                    "success_count": 20,
                    "failure_count": 80,
                    "failure_status_codes": {"200": 55, "400": 5},
                    "failure_reasons": {
                        "api_schema_error": 55,
                        "http_400": 5,
                        "network_timeout": 12,
                        "network_connect": 8,
                    },
                    "last_error": "网络请求失败：超时",
                }
            }
        }

        markdown = build_stats_summary_markdown(stats_data, [_provider()])

        self.assertIn("### 各站点响应分布", markdown)
        self.assertIn(
            "| 站点A | 20.0% | "
            "HTTP 200 55.0%, HTTP 400 5.0%, "
            "network_timeout 12.0%, network_connect 8.0% |",
            markdown,
        )
        self.assertIn("- 未记录 HTTP 状态：20 次", markdown)

    def test_failure_distribution_uses_generic_bucket_for_missing_reasons(self):
        """旧统计缺少原因明细时，不再展示不可量化历史缺口。"""
        stats_data = {
            "providers": {
                "pid-a": {
                    "name": "站点A",
                    "role": "normal",
                    "success_count": 1,
                    "failure_count": 3,
                    "failure_status_codes": {"429": 1},
                    "failure_reasons": {},
                }
            }
        }

        markdown = build_stats_summary_markdown(stats_data, [_provider()])

        self.assertIn("HTTP 429 25.0%", markdown)
        self.assertNotIn("历史未记录/未细分", markdown)

    def test_failure_distribution_labels_historical_unclassified_remainder(self):
        """聚合统计缺少历史原因明细时，失败分布仅展示可量化部分。"""
        stats_data = {
            "providers": {
                "pid-a": {
                    "name": "站点A",
                    "role": "primary",
                    "success_count": 162,
                    "failure_count": 164,
                    "failure_status_codes": {"400": 67, "502": 41},
                    "failure_reasons": {
                        "http_400": 67,
                        "http_5xx": 41,
                        "network_timeout": 17,
                        "network_connect": 4,
                    },
                }
            }
        }

        markdown = build_stats_summary_markdown(stats_data, [_provider()])

        self.assertIn(
            "HTTP 400 20.6%, HTTP 502 12.6%, "
            "network_timeout 5.2%, network_connect 1.2%",
            markdown,
        )
        self.assertNotIn("历史未记录/未细分", markdown)

    def test_elapsed_fields_are_displayed(self):
        """有耗时字段时，顶部和站点表展示平均耗时。"""
        stats_data = {
            "task_summary": {"success_count": 2, "success_elapsed_ms_avg": 1250},
            "providers": {
                "pid-a": {
                    "name": "站点A",
                    "role": "normal",
                    "success_count": 2,
                    "failure_count": 1,
                    "success_elapsed_ms_avg": 500,
                    "failure_elapsed_ms_avg": 2200,
                    "failure_reasons": {"network_timeout": 1},
                    "last_error": "timeout",
                }
            },
        }

        markdown = build_stats_summary_markdown(stats_data, [_provider()])

        self.assertIn("平均任务完成耗时：**1.2s**", markdown)
        self.assertIn("平均成功耗时", markdown)
        self.assertIn("平均失败耗时", markdown)
        self.assertIn("| 站点A | 2 | 1 | 66.7% | normal |", markdown)
        self.assertIn("| 站点A | 500ms | 2.2s |", markdown)

    def test_missing_elapsed_fields_are_compatible(self):
        """旧统计缺少耗时字段时展示 '-' 且不报错。"""
        stats_data = {
            "providers": {
                "pid-a": {
                    "name": "站点A",
                    "role": "normal",
                    "success_count": 1,
                    "failure_count": 0,
                }
            }
        }

        markdown = build_stats_summary_markdown(stats_data, [_provider()])

        self.assertIn("平均任务完成耗时：**-**", markdown)
        self.assertIn("| 站点A | 1 | 0 | 100.0% | normal |", markdown)
        self.assertIn("| 站点A | - | - |", markdown)
        self.assertIn("| 站点A | - | - | - |", markdown)

    def test_billing_stats_are_displayed_in_stats_summary(self):
        """stats 汇总展示缓存余额和周期费用。"""
        stats_data = {
            "providers": {
                "pid-a": {
                    "name": "站点A",
                    "role": "primary",
                    "success_count": 1,
                    "failure_count": 0,
                }
            }
        }
        billing_stats = {
            "providers": {
                "pid-a": {
                    "currency": "CNY",
                    "balance_unit": "CNY",
                    "last_balance_after": 39.9,
                    "total_cost": 1.2,
                    "last_cost": 0.1,
                }
            }
        }
        now = 1_700_000_000.0
        local_now = time_module.localtime(now)
        today_start = time_module.mktime(
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
        billing_events = [
            {"provider_id": "pid-a", "timestamp": now - 60, "cost": 0.1},
            {"provider_id": "pid-a", "timestamp": today_start - 60, "cost": 0.2},
            {"provider_id": "pid-a", "timestamp": now - 10 * 86400, "cost": 0.4},
        ]

        markdown = build_stats_summary_markdown(
            stats_data,
            [_provider()],
            billing_stats=billing_stats,
            billing_events=billing_events,
            now=now,
        )

        self.assertIn("### 各站点余额", markdown)
        self.assertIn("| 站点A | 39.9 CNY |", markdown)
        self.assertIn("### 各站点费用周期", markdown)
        self.assertIn(
            "| 站点A | 1.2 CNY | 0.1 CNY | 0.2 CNY | 0.3 CNY | 0.7 CNY |", markdown
        )

    def test_format_elapsed_ms_units(self):
        """毫秒和秒级耗时格式化符合展示约定。"""
        self.assertEqual(format_elapsed_ms(0), "0ms")
        self.assertEqual(format_elapsed_ms(999), "999ms")
        self.assertEqual(format_elapsed_ms(1200), "1.2s")
        self.assertEqual(format_elapsed_ms(None), "-")


class TestStatusCodeClassification(unittest.TestCase):
    """验证 HTTP 状态码提取。"""

    def test_api_schema_error_classification_is_case_insensitive(self):
        """大写 API 开头的结构异常会归类为结构错误。"""
        message = "API 返回结构异常：data 为空或非数组（顶层字段：created, data）"

        self.assertEqual(classify_failure_reason(message), "api_schema_error")

    def test_embedded_http_status_code_is_extracted(self):
        """结构异常消息中的嵌入式 HTTP 状态码会被提取。"""
        message = (
            "API 返回结构异常：响应不是有效 JSON（HTTP 200；content-type=text/html）"
        )

        self.assertEqual(classify_http_status_code(message), 200)

    def test_diagnostic_http_status_code_is_extracted(self):
        """ImageAPIError 的诊断对象可提供 HTTP 状态码。"""
        error = ImageAPIError(
            "API 返回结构异常：data 为空或非数组",
            diagnostics=HTTPDiagnostics(
                status_code=200,
                response_content_type="application/json",
                request_ids="-",
                response_preview="repr('{}')",
                response_preview_truncated=False,
                response_bytes=2,
                elapsed_ms=123,
            ),
        )

        self.assertEqual(diagnostic_http_status_code(error), 200)


if __name__ == "__main__":
    unittest.main()
