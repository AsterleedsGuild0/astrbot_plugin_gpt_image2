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
from image2_core.billing.config import BillingConfig  # noqa: E402
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


def _provider(
    provider_id: str = "pid-a",
    billing: BillingConfig | None = None,
) -> ImageAPIProviderConfig:
    """构造报表测试用 Provider 配置。"""
    return ImageAPIProviderConfig(
        name="站点A",
        api_key="",
        base_url="https://example.com/v1",
        model="gpt-image-2",
        responses_model="gpt-5.5",
        provider_id=provider_id,
        configured_order=0,
        billing=billing,
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
            "HTTP 200 55.0%, network_timeout 12.0%, "
            "network_connect 8.0%, HTTP 400 5.0% |",
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

    def test_failure_distribution_labels_collapsed_tail_count(self):
        """失败分类过多时展示被折叠的剩余分类数量。"""
        status_counts = {str(code): 1 for code in range(400, 409)}
        stats_data = {
            "providers": {
                "pid-a": {
                    "name": "站点A",
                    "role": "primary",
                    "success_count": 0,
                    "failure_count": 9,
                    "failure_status_codes": status_counts,
                    "failure_reasons": {f"http_{code}": 1 for code in range(400, 409)},
                }
            }
        }

        markdown = build_stats_summary_markdown(stats_data, [_provider()])

        self.assertIn("其余 2 项 22.2%", markdown)
        self.assertNotIn("其他", markdown)

    def test_failure_distribution_sorts_large_network_reason_before_tail(self):
        """大占比网络原因不会因 HTTP 优先展示而被折叠进尾部。"""
        stats_data = {
            "providers": {
                "pid-a": {
                    "name": "站点A",
                    "role": "primary",
                    "success_count": 178,
                    "failure_count": 738,
                    "failure_status_codes": {
                        "500": 265,
                        "403": 25,
                        "502": 16,
                        "503": 1,
                    },
                    "failure_reasons": {
                        "http_5xx": 282,
                        "http_403": 25,
                        "network_timeout": 70,
                        "network_connect": 360,
                        "network_protocol": 1,
                    },
                }
            }
        }

        markdown = build_stats_summary_markdown(stats_data, [_provider()])

        self.assertIn("network_connect 39.3%", markdown)
        self.assertNotIn("其余 2 项 39.4%", markdown)

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
                    "last_balance_after": 39.9,
                    "last_converted_balance": 39.9,
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
        self.assertIn("| 站点A | 39.9 CNY | 自动更新 |", markdown)
        self.assertIn("### 各站点费用周期", markdown)
        self.assertIn(
            "| 站点A | 1.2 CNY | 0.1 CNY | 0.2 CNY | 0.3 CNY | 0.7 CNY |", markdown
        )

    def test_billing_stats_manual_anchor_estimate(self):
        """手动锚点估算站点的余额不带标注，更新方式显示'手动更新'。"""
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
                    "last_balance_after": 77.06,
                    "last_converted_balance": 77.06,
                    "total_cost": 5.0,
                    "last_cost": 0.5,
                    "balance_source": "manual_anchor_estimate",
                }
            }
        }
        markdown = build_stats_summary_markdown(
            stats_data,
            [_provider()],
            billing_stats=billing_stats,
        )

        self.assertIn("### 各站点余额", markdown)
        # 余额列只显示金额，不再拼接"（手动锚点估算）"
        self.assertIn("| 站点A | 77.06 CNY | 手动更新 |", markdown)

    def test_billing_update_method_uses_config_balance_when_no_cache(self):
        """balance/total_usage billing 配置，无缓存余额时仍显示自动更新。"""
        billing_config = BillingConfig(
            type="balance",
            balance_url="https://api.example.com/balance",
        )
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
        markdown = build_stats_summary_markdown(
            stats_data,
            [_provider(billing=billing_config)],
            billing_stats={},
        )
        self.assertIn("| 站点A | - | 自动更新 |", markdown)

    def test_billing_update_method_uses_config_total_usage_when_no_cache(self):
        """total_usage billing 配置，无缓存余额时仍显示自动更新。"""
        billing_config = BillingConfig(
            type="total_usage",
            total_url="https://api.example.com/total",
            usage_url="https://api.example.com/usage",
        )
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
        markdown = build_stats_summary_markdown(
            stats_data,
            [_provider(billing=billing_config)],
            billing_stats={},
        )
        self.assertIn("| 站点A | - | 自动更新 |", markdown)

    def test_billing_update_method_uses_config_fixed_when_no_cache(self):
        """fixed billing 配置，无缓存余额时仍显示手动更新。"""
        billing_config = BillingConfig(
            type="fixed",
            success_cost=0.1,
            failure_cost=0.0,
        )
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
        markdown = build_stats_summary_markdown(
            stats_data,
            [_provider(billing=billing_config)],
            billing_stats={},
        )
        self.assertIn("| 站点A | - | 手动更新 |", markdown)

    def test_billing_update_method_manual_anchor_still_manual(self):
        """history-only provider（无配置）的 manual_anchor_estimate 缓存仍显示手动更新。"""
        billing_config = BillingConfig(
            type="balance",
            balance_url="https://api.example.com/balance",
        )
        stats_data = {
            "providers": {
                "pid-a": {
                    "name": "站点A",
                    "role": "primary",
                    "success_count": 1,
                    "failure_count": 0,
                },
                "pid-history": {
                    "name": "历史站点",
                    "role": "primary",
                    "success_count": 1,
                    "failure_count": 0,
                },
            }
        }
        billing_stats = {
            "providers": {
                "pid-history": {
                    "currency": "CNY",
                    "last_balance_after": 50.0,
                    "last_converted_balance": 50.0,
                    "total_cost": 2.0,
                    "balance_source": "manual_anchor_estimate",
                }
            }
        }
        # show_all 下 pid-history 没有对应的 provider config
        markdown = build_stats_summary_markdown(
            stats_data,
            [_provider(provider_id="pid-a", billing=billing_config)],
            billing_stats=billing_stats,
            show_all=True,
        )
        self.assertIn("| 历史站点 | 50 CNY | 手动更新 |", markdown)

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
