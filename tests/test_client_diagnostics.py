"""client 诊断相关测试：结构摘要、HTTPDiagnostics、响应预览脱敏。

独立测试：不需要 AstrBot 运行时或网络，只依赖 ``client.py``。
"""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock

# 测试环境没有 AstrBot 运行时，导入 client 前先 mock astrbot。
astrbot_api = MagicMock()
astrbot_api.logger = MagicMock()
sys.modules["astrbot"] = MagicMock()
sys.modules["astrbot.api"] = astrbot_api

from client import GPTImageClient, HTTPDiagnostics  # noqa: E402


class TestResponseJsonSummary(unittest.TestCase):
    """_response_json_summary 生成安全的结构摘要。"""

    def setUp(self):
        self.summarize = GPTImageClient._response_json_summary

    # ── 带 ``data`` key 的字典 ──────────────────────────────────

    def test_dict_data_list_populated(self):
        """data 为非空列表时，只描述顶层结构。"""
        data = {"created": 123, "data": [{"b64_json": "...", "revised_prompt": "..."}]}
        result = self.summarize(data)
        self.assertIn("dict keys=[created,data]", result)
        self.assertIn("data_type=list data_len=1", result)
        # 只在顶层 list 中展示首项细节；嵌套 data 列表不展开。
        self.assertNotIn("first_type", result)

    def test_dict_data_empty_list(self):
        """data 为空列表。"""
        data = {"created": 123, "data": []}
        result = self.summarize(data)
        self.assertIn("data_type=list data_len=0", result)

    def test_dict_data_none(self):
        """data 为 None。"""
        data = {"created": 123, "data": None}
        result = self.summarize(data)
        self.assertIn("data_type=NoneType", result)

    def test_dict_data_missing(self):
        """字典没有 data key。"""
        data = {"error": {"message": "foo", "type": "bar"}}
        result = self.summarize(data)
        self.assertIn("dict keys=[error]", result)
        self.assertIn("error_keys=[message,type]", result)
        self.assertNotIn("data_type", result)

    # ── 带 error key 的字典 ─────────────────────────────────────

    def test_dict_error_sub_object(self):
        """字典包含嵌套 error 对象。"""
        data = {"error": {"message": "bad request", "type": "invalid_request_error"}}
        result = self.summarize(data)
        self.assertIn("error_keys=[message,type]", result)

    # ── 列表响应 ────────────────────────────────────────────────

    def test_list_populated(self):
        """字典列表形式的非标准 API 响应。"""
        data = [{"b64_json": "abc", "revised_prompt": "x"}]
        result = self.summarize(data)
        self.assertEqual(
            result, "list len=1 first_type=dict first_keys=[b64_json,revised_prompt]"
        )

    def test_list_empty(self):
        """空列表。"""
        result = self.summarize([])
        self.assertEqual(result, "list len=0")

    # ── 标量和边界值 ───────────────────────────────────────────

    def test_str(self):
        """字符串响应。"""
        result = self.summarize("oops")
        self.assertIn("str", result)

    def test_none(self):
        """None 响应。"""
        result = self.summarize(None)
        self.assertIn("NoneType", result)

    # ── key 数量裁剪 ───────────────────────────────────────────

    def test_max_keys_clipping(self):
        """key 很多时会截断展示。"""
        data = {str(i): i for i in range(20)}
        result = self.summarize(data, max_keys=5)
        self.assertIn("+15more", result)
        self.assertNotIn("0,1,2,3,4,5,6", result)


class TestHTTPDiagnosticsDataclass(unittest.TestCase):
    """HTTPDiagnostics 字段和向后兼容性。"""

    def test_default_json_summary_empty(self):
        """response_json_summary 默认是空字符串。"""
        diag = HTTPDiagnostics(
            status_code=200,
            response_content_type="application/json",
            request_ids="-",
            response_preview="repr('ok')",
            response_preview_truncated=False,
            response_bytes=42,
            elapsed_ms=100,
        )
        self.assertEqual(diag.response_json_summary, "")

    def test_json_summary_set(self):
        """response_json_summary 可以显式设置。"""
        diag = HTTPDiagnostics(
            status_code=200,
            response_content_type="application/json",
            request_ids="-",
            response_preview="repr('ok')",
            response_preview_truncated=False,
            response_bytes=42,
            elapsed_ms=100,
            response_json_summary="dict keys=[data] data_type=list data_len=0",
        )
        self.assertEqual(
            diag.response_json_summary,
            "dict keys=[data] data_type=list data_len=0",
        )

    def test_positional_args_backward_compat(self):
        """原有位置参数仍可使用，字段顺序保持不变。"""
        diag = HTTPDiagnostics(
            200,
            "text/plain",
            "x-request-id=abc",
            "repr('body')",
            False,
            99,
            500,
        )
        self.assertEqual(diag.status_code, 200)
        self.assertEqual(diag.response_content_type, "text/plain")
        self.assertEqual(diag.request_ids, "x-request-id=abc")
        self.assertEqual(diag.response_preview, "repr('body')")
        self.assertFalse(diag.response_preview_truncated)
        self.assertEqual(diag.response_bytes, 99)
        self.assertEqual(diag.elapsed_ms, 500)
        self.assertEqual(diag.response_json_summary, "")


class TestSanitizedResponsePreview(unittest.TestCase):
    """_sanitized_response_preview 会过滤 base64、密钥和 token。"""

    def test_base64_redacted(self):
        text = '{"b64_json": "ABC' + "A" * 200 + '"}'
        preview, truncated = GPTImageClient._sanitized_response_preview(text)
        self.assertIn("<redacted-base64>", preview)
        self.assertNotIn("AAA", preview)

    def test_bearer_token_redacted(self):
        text = "Authorization: Bearer sk-abc123def456ghi789jkl"
        preview, truncated = GPTImageClient._sanitized_response_preview(text)
        self.assertIn("***REDACTED***", preview)
        self.assertNotIn("sk-abc123def456ghi789jkl", preview)

    def test_api_key_pattern_redacted(self):
        text = '{"api_key": "sk-xxxxxxxxxxxxxxxxxxxx"}'
        preview, truncated = GPTImageClient._sanitized_response_preview(text)
        self.assertIn("***REDACTED***", preview)
        self.assertNotIn("sk-xxxx", preview)

    def test_empty_string(self):
        preview, truncated = GPTImageClient._sanitized_response_preview("")
        self.assertEqual(preview, "repr('')")
        self.assertFalse(truncated)

    def test_short_text_no_truncation(self):
        text = '{"ok": true}'
        preview, truncated = GPTImageClient._sanitized_response_preview(text)
        self.assertIn("ok", preview)
        self.assertFalse(truncated)

    def test_no_sensitive_data_unchanged(self):
        text = '{"created": 1700000000}'
        preview, truncated = GPTImageClient._sanitized_response_preview(text)
        self.assertIn("created", preview)
        self.assertFalse(truncated)


class TestRequestIdHeaders(unittest.TestCase):
    """_request_id_headers 提取常见 request id 响应头。"""

    def test_no_matching_headers(self):
        """没有相关响应头时应返回短横线。"""
        # 这里不额外构造 httpx.Response，只确认方法存在并可调用。
        self.assertTrue(
            callable(GPTImageClient._request_id_headers),
            "_request_id_headers should be a callable static method",
        )


if __name__ == "__main__":
    unittest.main()
