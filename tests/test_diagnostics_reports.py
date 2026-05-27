"""诊断统计报表测试。"""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock

# 测试环境没有 AstrBot 运行时，导入 provider/report 前先 mock astrbot。
astrbot_api = MagicMock()
astrbot_api.logger = MagicMock()
sys.modules["astrbot"] = MagicMock()
sys.modules["astrbot.api"] = astrbot_api

from image2_core.api.client import HTTPDiagnostics, ImageAPIError  # noqa: E402
from image2_core.diagnostics.reports import build_stats_summary_markdown  # noqa: E402
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
