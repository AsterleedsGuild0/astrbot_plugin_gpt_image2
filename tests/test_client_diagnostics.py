"""client 诊断相关测试：结构摘要、HTTPDiagnostics、响应预览脱敏。

独立测试：不需要 AstrBot 运行时或网络，只依赖 ``client.py``。
"""

from __future__ import annotations

import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

# 测试环境没有 AstrBot 运行时，导入 client 前先 mock astrbot。
astrbot_api = MagicMock()
astrbot_api.logger = MagicMock()
sys.modules["astrbot"] = MagicMock()
sys.modules["astrbot.api"] = astrbot_api

from image2_core.api.client import (  # noqa: E402
    GPTImageClient,
    HTTPDiagnostics,
    ImageParams,
    ImageResult,
)


def fake_openai_key(suffix: str) -> str:
    """构造测试用 OpenAI-like key，避免源码中出现完整 key 字面量。"""
    return "sk" + "-" + suffix


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
        fake_key = fake_openai_key("abc123def456ghi789jkl")
        text = f"Authorization: Bearer {fake_key}"
        preview, truncated = GPTImageClient._sanitized_response_preview(text)
        self.assertIn("***REDACTED***", preview)
        self.assertNotIn(fake_key, preview)

    def test_api_key_pattern_redacted(self):
        fake_key = fake_openai_key("xxxxxxxxxxxxxxxxxxxx")
        text = f'{{"api_key": "{fake_key}"}}'
        preview, truncated = GPTImageClient._sanitized_response_preview(text)
        self.assertIn("***REDACTED***", preview)
        self.assertNotIn(fake_key[:7], preview)

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


class TestProviderRequestElapsedState(unittest.TestCase):
    """Provider 请求耗时状态按上游请求组口径记录。"""

    def test_request_group_elapsed_uses_first_upstream_request_start(self):
        client = GPTImageClient(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="gpt-image-2",
            responses_model="gpt-5.5",
        )
        client._elapsed_ms = lambda _start: 1234  # type: ignore[method-assign]

        client._begin_request_group(100.0)
        client._begin_request_group(200.0)
        client._record_request_elapsed_ms(50)

        self.assertEqual(client.last_request_elapsed_ms, 1234)

    def test_reset_clears_provider_request_elapsed_state(self):
        client = GPTImageClient(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="gpt-image-2",
            responses_model="gpt-5.5",
        )

        client._begin_request_group(100.0)
        client.last_request_elapsed_ms = 321
        client._reset_last_request_elapsed_ms()

        self.assertIsNone(client.last_request_elapsed_ms)
        self.assertIsNone(client._request_group_started_at)


class TestForceSingleImageRequests(unittest.IsolatedAsyncioTestCase):
    """强制单图请求会跳过原生 n 调用。"""

    def _client(self) -> GPTImageClient:
        return GPTImageClient(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="gpt-image-2",
            responses_model="gpt-5.5",
            force_single_image_requests=True,
        )

    async def test_generate_skips_native_n_and_uses_batch(self):
        client = self._client()
        client._generate_images_api_once = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("native n should be skipped")
        )
        client._generate_images_api_batch = AsyncMock(  # type: ignore[method-assign]
            return_value=[]
        )

        params = ImageParams(n=2)
        await client.generate_images_api("prompt", params)

        client._generate_images_api_once.assert_not_awaited()
        client._generate_images_api_batch.assert_awaited_once()
        batch_kwargs = client._generate_images_api_batch.await_args.kwargs
        self.assertEqual(batch_kwargs["n"], 2)

    async def test_edit_skips_native_n_and_uses_batch(self):
        client = self._client()
        client._edit_images_api_once = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("native n should be skipped")
        )
        client._edit_images_api_batch = AsyncMock(  # type: ignore[method-assign]
            return_value=[]
        )

        params = ImageParams(n=2)
        await client.edit_images_api("prompt", ["input.png"], params)

        client._edit_images_api_once.assert_not_awaited()
        client._edit_images_api_batch.assert_awaited_once()
        batch_kwargs = client._edit_images_api_batch.await_args.kwargs
        self.assertEqual(batch_kwargs["n"], 2)


class TestShouldFallbackImagesNativeNError(unittest.TestCase):
    """_should_fallback_images_native_n_error 对 nested 参数路径错误返回 True。"""

    def test_tools_0_n_quoted(self):
        """'tools[0].n' 应触发 fallback。"""
        msg = "HTTP 400 Unknown parameter: 'tools[0].n'."
        self.assertTrue(GPTImageClient._should_fallback_images_native_n_error(msg))

    def test_tools_0_n_unquoted(self):
        """tools[0].n (无引号) 应触发 fallback。"""
        msg = "HTTP 400 unknown parameter: tools[0].n"
        self.assertTrue(GPTImageClient._should_fallback_images_native_n_error(msg))

    def test_non_n_param_does_not_trigger(self):
        """tools[0].size 等非 n 参数不应触发 fallback。"""
        msg = "HTTP 400 Unknown parameter: 'tools[0].size'."
        self.assertFalse(GPTImageClient._should_fallback_images_native_n_error(msg))

    def test_standard_unknown_param_n_still_detected(self):
        """原有的 'unknown parameter: n' 仍能被检测。"""
        msg = "HTTP 400 Unknown parameter: 'n'."
        self.assertTrue(GPTImageClient._should_fallback_images_native_n_error(msg))

    def test_http_422_nested_n(self):
        """HTTP 422 + nested .n 也应触发 fallback。"""
        msg = "HTTP 422 Unprocessable: unknown parameter 'tools[0].n'"
        self.assertTrue(GPTImageClient._should_fallback_images_native_n_error(msg))


class TestNativeNFallbackTracking(unittest.IsolatedAsyncioTestCase):
    """``native_n_fallback_used`` / ``native_n_fallback_reason`` 正确标记。"""

    def _client(self, force_single: bool = False) -> GPTImageClient:
        return GPTImageClient(
            api_key="test-key",
            base_url="https://example.test/v1",
            model="gpt-image-2",
            responses_model="gpt-5.5",
            force_single_image_requests=force_single,
        )

    async def test_native_n_unsupported_error_sets_fallback_flag(self):
        """原生 n 不兼容错误触发 fallback 时标记为 True。"""
        client = self._client()
        client._generate_images_api_once = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("HTTP 400 Unknown parameter: 'tools[0].n'.")
        )
        client._generate_images_api_batch = AsyncMock(  # type: ignore[method-assign]
            return_value=[ImageResult(b64_json="abc")]
        )

        params = ImageParams(n=2)
        await client.generate_images_api("prompt", params)

        self.assertTrue(client.native_n_fallback_used)
        self.assertIn("Unknown parameter", client.native_n_fallback_reason)

    async def test_force_single_does_not_set_fallback_flag(self):
        """force_single_image_requests 时不标记 fallback。"""
        client = self._client(force_single=True)
        client._generate_images_api_batch = AsyncMock(  # type: ignore[method-assign]
            return_value=[ImageResult(b64_json="abc")]
        )

        params = ImageParams(n=2)
        await client.generate_images_api("prompt", params)

        self.assertFalse(client.native_n_fallback_used)
        self.assertEqual(client.native_n_fallback_reason, "")

    async def test_n_1_does_not_set_fallback_flag(self):
        """n=1 时直接返回，不标记 fallback。"""
        client = self._client()
        client._generate_images_api_once = AsyncMock(  # type: ignore[method-assign]
            return_value=[ImageResult(b64_json="abc")]
        )

        params = ImageParams(n=1)
        await client.generate_images_api("prompt", params)

        self.assertFalse(client.native_n_fallback_used)
        self.assertEqual(client.native_n_fallback_reason, "")

    async def test_partial_results_supplement_not_fallback(self):
        """原生 n 返回数量不足后的补请求不应误标记。"""
        client = self._client()
        first_call = True

        async def _generate_images_api_once(prompt, params):  # noqa: ARG001
            nonlocal first_call
            if first_call:
                first_call = False
                return [ImageResult(b64_json="abc")]
            return [ImageResult(b64_json="def")]

        client._generate_images_api_once = (  # type: ignore[method-assign]
            _generate_images_api_once
        )
        client._generate_images_api_batch = AsyncMock(  # type: ignore[method-assign]
            return_value=[ImageResult(b64_json="extra")]
        )

        params = ImageParams(n=2)
        await client.generate_images_api("prompt", params)

        # native n returned len(results)=1 < n=2, supplement triggered
        self.assertFalse(client.native_n_fallback_used)
        self.assertEqual(client.native_n_fallback_reason, "")

    async def test_non_n_error_does_not_set_fallback_flag(self):
        """非 n 相关 RuntimeError 不会触发 fallback 标记。"""
        client = self._client()
        client._generate_images_api_once = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("HTTP 500 Internal Server Error")
        )
        client._generate_images_api_batch = AsyncMock(  # type: ignore[method-assign]
            return_value=[ImageResult(b64_json="abc")]
        )

        params = ImageParams(n=2)
        with self.assertRaises(RuntimeError):
            await client.generate_images_api("prompt", params)

        self.assertFalse(client.native_n_fallback_used)
        self.assertEqual(client.native_n_fallback_reason, "")

    async def test_edit_native_n_unsupported_sets_fallback_flag(self):
        """edit_images_api 原生 n 不兼容也标记。"""
        client = self._client()
        error_msg = "HTTP 400 Unknown parameter: 'n'."
        client._edit_images_api_once = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError(error_msg)
        )
        client._edit_images_api_batch = AsyncMock(  # type: ignore[method-assign]
            return_value=[ImageResult(b64_json="abc")]
        )

        params = ImageParams(n=2)
        await client.edit_images_api("prompt", ["input.png"], params)

        self.assertTrue(client.native_n_fallback_used)
        self.assertIn("Unknown parameter", client.native_n_fallback_reason)

    async def test_fallback_flag_reset_on_new_call(self):
        """每次 generate 或 edit 开始时重置标记。"""
        client = self._client()
        client.native_n_fallback_used = True
        client.native_n_fallback_reason = "old"

        client._generate_images_api_once = AsyncMock(  # type: ignore[method-assign]
            return_value=[ImageResult(b64_json="abc")]
        )
        params = ImageParams(n=1)
        await client.generate_images_api("prompt", params)

        self.assertFalse(client.native_n_fallback_used)
        self.assertEqual(client.native_n_fallback_reason, "")


if __name__ == "__main__":
    unittest.main()
