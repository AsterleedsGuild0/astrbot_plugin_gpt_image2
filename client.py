"""GPT Image2 插件 - API 客户端

职责：
- 封装 Images API 文生图与图像编辑
- 封装 Responses API 图像生成工具调用
- 封装 Responses API Plan 文本/图文规划调用
- 解析 API 返回
- 归一化错误信息
"""

from __future__ import annotations

import asyncio
import json
import os
from time import perf_counter
from dataclasses import dataclass
from typing import Any

import httpx
from astrbot.api import logger


@dataclass
class ImageParams:
    """统一参数模型"""

    size: str = "auto"
    quality: str = "auto"
    output_format: str = "png"
    moderation: str = "auto"
    n: int = 1
    output_compression: int | None = None


@dataclass
class ImageResult:
    """统一结果模型"""

    b64_json: str | None = None
    url: str | None = None
    revised_prompt: str | None = None


PROMPT_REWRITE_GUARD_PREFIX = (
    "Use the following text as the complete prompt. Do not rewrite it:"
)


class GPTImageClient:
    """GPT Image2 API 客户端"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        responses_model: str,
        timeout: int = 120,
        response_format_b64_json: bool = True,
        images_prompt_rewrite_guard: bool = False,
        responses_prompt_rewrite_guard: bool = True,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.responses_model = responses_model
        self.timeout = timeout
        self.response_format_b64_json = response_format_b64_json
        self.images_prompt_rewrite_guard = images_prompt_rewrite_guard
        self.responses_prompt_rewrite_guard = responses_prompt_rewrite_guard

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
        }

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((perf_counter() - start) * 1000)

    @staticmethod
    def _result_summary(results: list[ImageResult]) -> str:
        b64_count = sum(1 for item in results if item.b64_json)
        url_count = sum(1 for item in results if item.url)
        revised_count = sum(1 for item in results if item.revised_prompt)
        return f"results={len(results)} b64={b64_count} url={url_count} revised={revised_count}"

    @staticmethod
    def _apply_prompt_rewrite_guard(prompt: str, enabled: bool) -> str:
        if not enabled:
            return prompt
        if prompt.lstrip().startswith(PROMPT_REWRITE_GUARD_PREFIX):
            return prompt
        return f"{PROMPT_REWRITE_GUARD_PREFIX}\n{prompt}"

    @staticmethod
    def _normalize_n(value: object) -> int:
        try:
            if isinstance(value, (int, float)):
                return max(1, int(value))
            if isinstance(value, str):
                return max(1, int(value.strip() or "1"))
        except (TypeError, ValueError):
            pass
        return 1

    @staticmethod
    def _sanitized_response_preview(text: str, limit: int = 500) -> str:
        """Sanitize and truncate response body for diagnostics logging.

        Strips likely base64 data URLs and truncates to limit chars,
        avoiding logging sensitive data like API keys or image data.
        """
        if not text:
            return "-"
        # Avoid logging huge responses
        preview = text[: limit + 100]
        # Rough removal of base64 blobs in JSON
        import re as _re

        preview = _re.sub(
            r'"(?:b64_json|image|data|image_url)"\s*:\s*"[A-Za-z0-9+/=]{100,}"',
            '"<redacted-base64>"',
            preview,
        )
        if len(preview) > limit:
            preview = preview[:limit] + "…"
        return repr(preview)

    @staticmethod
    def _request_id_headers(resp: httpx.Response) -> str:
        """Extract common request ID headers from response for diagnostics."""
        candidates = (
            "x-request-id",
            "request-id",
            "cf-ray",
            "openai-request-id",
            "x-request-id",
        )
        ids = []
        for header in candidates:
            value = resp.headers.get(header)
            if value:
                ids.append(f"{header}={value}")
        return " ".join(ids) if ids else "-"

    def _log_http_failure(
        self,
        resp: httpx.Response,
        elapsed_ms: int,
        context: str,
    ) -> None:
        """Log detailed non-success HTTP response diagnostics."""
        preview = self._sanitized_response_preview(resp.text)
        request_ids = self._request_id_headers(resp)
        logger.warning(
            f"[GPTImage2] {context} HTTP failure "
            f"status={resp.status_code} elapsed_ms={elapsed_ms} "
            f"response_bytes={len(resp.content)} "
            f"content_type={resp.headers.get('content-type', '-')} "
            f"request_ids=[{request_ids}] "
            f"response_preview={preview}"
        )

    @staticmethod
    def _params_with_n(params: ImageParams, n: int) -> ImageParams:
        return ImageParams(
            size=params.size,
            quality=params.quality,
            output_format=params.output_format,
            moderation=params.moderation,
            n=max(1, n),
            output_compression=params.output_compression,
        )

    @staticmethod
    def _should_fallback_images_native_n_error(error_msg: str) -> bool:
        lower = str(error_msg or "").lower()
        direct_phrases = {
            "does not support n",
            "n is not supported",
            "n must be 1",
            "n should be 1",
            "unsupported parameter: n",
            "unknown parameter: n",
            "invalid parameter: n",
            "unexpected parameter: n",
            "one image can be generated at a time",
            "only one image can be generated",
            "one image generation at a time",
            "multiple output images",
            "multiple generations",
            "more than 1 image",
            "maximum of 1",
            "max 1",
        }
        if any(phrase in lower for phrase in direct_phrases):
            return True

        mentions_n = any(
            marker in lower for marker in ('"n"', "'n'", "`n`", " n ", "parameter n")
        )
        unsupported = any(
            phrase in lower
            for phrase in {
                "unsupported",
                "not supported",
                "unknown",
                "unrecognized",
                "invalid",
                "must be",
                "should be",
                "maximum",
                "not allowed",
                "cannot",
                "不支持",
            }
        )
        parameter_error = lower.startswith("http 400") or lower.startswith("http 422")
        return parameter_error and mentions_n and unsupported

    def _build_error_msg(self, status_code: int, body: Any) -> str:
        """构建不泄露 API Key 的错误消息"""
        if status_code == 524:
            return (
                "HTTP 524 服务商网关等待模型响应超时。"
                "请稍后重试，或切换到更稳定的备用 API 站点。"
            )
        try:
            if isinstance(body, str) and body:
                stripped = body.lstrip()
                lower = stripped[:80].lower()
                if lower.startswith("<!doctype html") or lower.startswith("<html"):
                    return (
                        f"HTTP {status_code} 上游服务返回了 HTML 错误页。"
                        "请稍后重试，或检查服务商状态。"
                    )
                try:
                    body = json.loads(body)
                except json.JSONDecodeError:
                    return f"HTTP {status_code} (响应体首部: {body[:200]})"

            if isinstance(body, dict):
                err = body.get("error", {})
                if isinstance(err, dict):
                    msg = err.get("message", "")
                    if msg:
                        return f"HTTP {status_code} {msg}"
                if "message" in body:
                    return f"HTTP {status_code} {body['message']}"
        except Exception:
            pass
        if status_code == 429:
            return "HTTP 429 请求过于频繁或额度不足，请稍后重试。"
        return f"HTTP {status_code}"

    def _build_network_error_msg(
        self,
        error: httpx.HTTPError,
        *,
        url: str,
        elapsed_ms: int,
    ) -> str:
        """构建包含异常类型和请求上下文的网络错误，不泄露 API Key。"""
        error_type = type(error).__name__
        detail = str(error).strip()

        if isinstance(error, httpx.TimeoutException):
            reason = f"请求超时（timeout={self.timeout}s）"
        elif isinstance(error, httpx.ConnectError):
            reason = "连接失败"
        elif isinstance(error, httpx.ProxyError):
            reason = "代理连接失败"
        elif isinstance(error, httpx.RemoteProtocolError):
            reason = "上游连接协议异常"
        elif isinstance(error, httpx.NetworkError):
            reason = "网络传输异常"
        else:
            reason = "HTTP 客户端异常"

        if detail:
            reason = f"{reason}：{detail}"

        return (
            f"网络请求失败（{error_type}）：{reason}；"
            f"url={url}；elapsed_ms={elapsed_ms}"
        )

    # ── Images API ──────────────────────────────────────────────

    async def generate_images_api(
        self,
        prompt: str,
        params: ImageParams,
    ) -> list[ImageResult]:
        """Images API 文生图。

        优先尝试上游原生 n；若上游不支持 n 或只返回部分结果，
        自动使用多次单图请求补足。
        """
        n = self._normalize_n(params.n)
        params = self._params_with_n(params, n)
        if n == 1:
            return await self._generate_images_api_once(prompt, params)

        try:
            results = await self._generate_images_api_once(prompt, params)
        except RuntimeError as e:
            if not self._should_fallback_images_native_n_error(str(e)):
                raise
            logger.info(
                "[GPTImage2] Images API native n unsupported, "
                f"fallback to batch generate n={n} error={e}"
            )
            return await self._generate_images_api_batch(prompt, params, n=n)

        if len(results) >= n:
            return results

        remaining = n - len(results)
        logger.info(
            "[GPTImage2] Images API native n returned fewer results, "
            f"fallback to batch generate remaining={remaining} "
            f"requested={n} returned={len(results)}"
        )
        try:
            extras = await self._generate_images_api_batch(prompt, params, n=remaining)
        except Exception as e:
            logger.warning(
                "[GPTImage2] Images API generate n supplement failed, "
                f"return partial results requested={n} returned={len(results)} error={e}"
            )
            return results
        return results + extras

    async def _generate_images_api_once(
        self,
        prompt: str,
        params: ImageParams,
    ) -> list[ImageResult]:
        """Images API 单次文生图调用。

        POST {base_url}/images/generations
        """
        url = f"{self.base_url}/images/generations"
        guarded_prompt = self._apply_prompt_rewrite_guard(
            prompt,
            self.images_prompt_rewrite_guard,
        )
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": guarded_prompt,
            "size": params.size,
            "quality": params.quality,
            "output_format": params.output_format,
            "moderation": params.moderation,
        }
        if params.n > 1:
            body["n"] = params.n
        if params.output_format != "png" and params.output_compression is not None:
            body["output_compression"] = params.output_compression
        if self.response_format_b64_json:
            body["response_format"] = "b64_json"

        start = perf_counter()
        logger.info(
            "[GPTImage2] Images API generate request start "
            f"url={url} model={self.model} prompt_len={len(prompt)} "
            f"guard={self.images_prompt_rewrite_guard} "
            f"size={params.size} quality={params.quality} "
            f"format={params.output_format} n={params.n} timeout={self.timeout}s"
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json=body,
                )
        except httpx.HTTPError as e:
            elapsed = self._elapsed_ms(start)
            error_msg = self._build_network_error_msg(e, url=url, elapsed_ms=elapsed)
            logger.warning(
                f"[GPTImage2] Images API generate request failed {error_msg}"
            )
            raise RuntimeError(error_msg) from e

        elapsed = self._elapsed_ms(start)
        logger.debug(
            "[GPTImage2] Images API generate response received "
            f"status={resp.status_code} elapsed_ms={elapsed} "
            f"response_bytes={len(resp.content)}"
        )

        if not resp.is_success:
            self._log_http_failure(resp, elapsed, "Images API generate")
            raise RuntimeError(self._build_error_msg(resp.status_code, resp.text))

        data = resp.json()
        results = self._parse_images_api_response(data)
        logger.info(
            "[GPTImage2] Images API generate request success "
            f"elapsed_ms={elapsed} {self._result_summary(results)}"
        )
        return results

    async def _generate_images_api_batch(
        self,
        prompt: str,
        params: ImageParams,
        *,
        n: int,
    ) -> list[ImageResult]:
        """Images API 按单图请求并发补足文生图数量。"""
        n = self._normalize_n(n)
        single_params = self._params_with_n(params, 1)
        logger.info(f"[GPTImage2] Images API batch generate start n={n}")
        start = perf_counter()
        tasks = [
            self._generate_images_api_once(prompt, single_params) for _ in range(n)
        ]
        settled = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[ImageResult] = []
        first_error: Exception | None = None
        for item in settled:
            if isinstance(item, BaseException):
                if first_error is None:
                    first_error = (
                        item if isinstance(item, Exception) else Exception(str(item))
                    )
                continue
            results.extend(item)
        if results:
            logger.info(
                "[GPTImage2] Images API batch generate success "
                f"elapsed_ms={self._elapsed_ms(start)} {self._result_summary(results)}"
            )
            return results
        if first_error:
            logger.warning(
                "[GPTImage2] Images API batch generate failed "
                f"elapsed_ms={self._elapsed_ms(start)} error={first_error}"
            )
            raise first_error
        raise RuntimeError("Images API 并发文生图请求均未返回图片")

    async def edit_images_api(
        self,
        prompt: str,
        image_paths: list[str],
        params: ImageParams,
    ) -> list[ImageResult]:
        """Images API 图像编辑。

        优先尝试上游原生 n；若上游不支持 n 或只返回部分结果，
        自动使用多次单图请求补足。
        """
        n = self._normalize_n(params.n)
        params = self._params_with_n(params, n)
        if n == 1:
            return await self._edit_images_api_once(prompt, image_paths, params)

        try:
            results = await self._edit_images_api_once(prompt, image_paths, params)
        except RuntimeError as e:
            if not self._should_fallback_images_native_n_error(str(e)):
                raise
            logger.info(
                "[GPTImage2] Images API native n unsupported, "
                f"fallback to batch edit n={n} error={e}"
            )
            return await self._edit_images_api_batch(prompt, image_paths, params, n=n)

        if len(results) >= n:
            return results

        remaining = n - len(results)
        logger.info(
            "[GPTImage2] Images API native n returned fewer edit results, "
            f"fallback to batch edit remaining={remaining} "
            f"requested={n} returned={len(results)}"
        )
        try:
            extras = await self._edit_images_api_batch(
                prompt,
                image_paths,
                params,
                n=remaining,
            )
        except Exception as e:
            logger.warning(
                "[GPTImage2] Images API edit n supplement failed, "
                f"return partial results requested={n} returned={len(results)} error={e}"
            )
            return results
        return results + extras

    async def _edit_images_api_once(
        self,
        prompt: str,
        image_paths: list[str],
        params: ImageParams,
    ) -> list[ImageResult]:
        """Images API 单次图像编辑调用。

        POST {base_url}/images/edits
        Content-Type: multipart/form-data
        """
        url = f"{self.base_url}/images/edits"
        guarded_prompt = self._apply_prompt_rewrite_guard(
            prompt,
            self.images_prompt_rewrite_guard,
        )

        multipart_data: dict[str, Any] = {
            "model": self.model,
            "prompt": guarded_prompt,
            "size": params.size,
            "quality": params.quality,
            "output_format": params.output_format,
            "moderation": params.moderation,
        }
        if params.n > 1:
            multipart_data["n"] = params.n
        if params.output_format != "png" and params.output_compression is not None:
            multipart_data["output_compression"] = params.output_compression
        if self.response_format_b64_json:
            multipart_data["response_format"] = "b64_json"

        files: list[tuple[str, tuple[str, bytes, str]]] = []
        file_sizes: list[int] = []
        for idx, path in enumerate(image_paths):
            with open(path, "rb") as f:
                file_bytes = f.read()
            file_sizes.append(len(file_bytes))
            ext = self._guess_ext(path)
            mime_ext = "jpeg" if ext == "jpg" else ext
            files.append(
                (
                    "image[]",
                    (f"input-{idx + 1}.{ext}", file_bytes, f"image/{mime_ext}"),
                )
            )

        start = perf_counter()
        logger.info(
            "[GPTImage2] Images API edit request start "
            f"url={url} model={self.model} prompt_len={len(prompt)} "
            f"guard={self.images_prompt_rewrite_guard} "
            f"input_images={len(image_paths)} input_bytes={sum(file_sizes)} "
            f"size={params.size} quality={params.quality} "
            f"format={params.output_format} n={params.n} timeout={self.timeout}s"
        )
        logger.debug(f"[GPTImage2] Images API edit input file sizes={file_sizes}")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    headers=self._headers(),
                    data=multipart_data,
                    files=files,
                )
        except httpx.HTTPError as e:
            elapsed = self._elapsed_ms(start)
            error_msg = self._build_network_error_msg(e, url=url, elapsed_ms=elapsed)
            logger.warning(f"[GPTImage2] Images API edit request failed {error_msg}")
            raise RuntimeError(error_msg) from e

        elapsed = self._elapsed_ms(start)
        logger.debug(
            "[GPTImage2] Images API edit response received "
            f"status={resp.status_code} elapsed_ms={elapsed} "
            f"response_bytes={len(resp.content)}"
        )

        if not resp.is_success:
            self._log_http_failure(resp, elapsed, "Images API edit")
            raise RuntimeError(self._build_error_msg(resp.status_code, resp.text))

        data = resp.json()
        results = self._parse_images_api_response(data)
        logger.info(
            "[GPTImage2] Images API edit request success "
            f"elapsed_ms={elapsed} {self._result_summary(results)}"
        )
        return results

    async def _edit_images_api_batch(
        self,
        prompt: str,
        image_paths: list[str],
        params: ImageParams,
        *,
        n: int,
    ) -> list[ImageResult]:
        """Images API 按单图请求并发补足图像编辑数量。"""
        n = self._normalize_n(n)
        single_params = self._params_with_n(params, 1)
        logger.info(
            "[GPTImage2] Images API batch edit start "
            f"n={n} input_images={len(image_paths)}"
        )
        start = perf_counter()
        tasks = [
            self._edit_images_api_once(prompt, image_paths, single_params)
            for _ in range(n)
        ]
        settled = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[ImageResult] = []
        first_error: Exception | None = None
        for item in settled:
            if isinstance(item, BaseException):
                if first_error is None:
                    first_error = (
                        item if isinstance(item, Exception) else Exception(str(item))
                    )
                continue
            results.extend(item)
        if results:
            logger.info(
                "[GPTImage2] Images API batch edit success "
                f"elapsed_ms={self._elapsed_ms(start)} {self._result_summary(results)}"
            )
            return results
        if first_error:
            logger.warning(
                "[GPTImage2] Images API batch edit failed "
                f"elapsed_ms={self._elapsed_ms(start)} error={first_error}"
            )
            raise first_error
        raise RuntimeError("Images API 并发图像编辑请求均未返回图片")

    def _parse_images_api_response(self, data: dict | list) -> list[ImageResult]:
        """解析 Images API 返回

        data[*].b64_json
        data[*].url
        data[*].revised_prompt
        """
        items = data if isinstance(data, list) else data.get("data", [])
        if not isinstance(items, list) or not items:
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict) and err.get("message"):
                    raise RuntimeError(f"API 返回错误：{err['message']}")
                if isinstance(data.get("message"), str):
                    raise RuntimeError(f"API 返回错误：{data['message']}")
                keys = ", ".join(str(key) for key in data.keys()) or "无"
                raise RuntimeError(
                    f"API 返回结构异常：data 为空或非数组（顶层字段：{keys}）"
                )
            raise RuntimeError("API 返回结构异常：data 为空或非数组")

        results: list[ImageResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            results.append(
                ImageResult(
                    b64_json=item.get("b64_json"),
                    url=item.get("url"),
                    revised_prompt=item.get("revised_prompt"),
                )
            )

        if not results:
            raise RuntimeError("API 返回结构异常：无法解析图片数据")

        return results

    # ── Responses API ───────────────────────────────────────────

    async def generate_responses_api(
        self,
        prompt: str,
        params: ImageParams,
    ) -> list[ImageResult]:
        """Responses API 文生图

        POST {base_url}/responses
        """
        return await self._call_responses_api_with_n(
            prompt,
            [],
            params,
            action="generate",
        )

    async def edit_responses_api(
        self,
        prompt: str,
        image_data_urls: list[str],
        params: ImageParams,
    ) -> list[ImageResult]:
        """Responses API 图像编辑

        POST {base_url}/responses
        输入图片使用 data URL
        """
        return await self._call_responses_api_with_n(
            prompt, image_data_urls, params, action="edit"
        )

    async def _call_responses_api_with_n(
        self,
        prompt: str,
        image_data_urls: list[str],
        params: ImageParams,
        action: str,
    ) -> list[ImageResult]:
        """Responses API 不传 n，按 n 并发多次请求。"""
        n = self._normalize_n(params.n)
        if n == 1:
            return await self._call_responses_api(
                prompt, image_data_urls, params, action
            )

        logger.info(
            "[GPTImage2] Responses API batch request start "
            f"action={action} n={n} input_images={len(image_data_urls)}"
        )
        start = perf_counter()
        tasks = [
            self._call_responses_api(prompt, image_data_urls, params, action)
            for _ in range(n)
        ]
        settled = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[ImageResult] = []
        first_error: Exception | None = None
        for item in settled:
            if isinstance(item, BaseException):
                if first_error is None:
                    first_error = (
                        item if isinstance(item, Exception) else Exception(str(item))
                    )
                continue
            results.extend(item)
        if results:
            logger.info(
                "[GPTImage2] Responses API batch request success "
                f"elapsed_ms={self._elapsed_ms(start)} {self._result_summary(results)}"
            )
            return results
        if first_error:
            logger.warning(
                "[GPTImage2] Responses API batch request failed "
                f"elapsed_ms={self._elapsed_ms(start)} error={first_error}"
            )
            raise first_error
        raise RuntimeError("Responses API 并发请求均未返回图片")

    async def _call_responses_api(
        self,
        prompt: str,
        image_data_urls: list[str],
        params: ImageParams,
        action: str = "generate",
    ) -> list[ImageResult]:
        """Responses API 通用调用"""
        url = f"{self.base_url}/responses"
        guarded_prompt = self._apply_prompt_rewrite_guard(
            prompt,
            self.responses_prompt_rewrite_guard,
        )

        # 构建 input
        if not image_data_urls:
            input_data: Any = guarded_prompt
        else:
            content: list[dict[str, Any]] = [
                {"type": "input_text", "text": guarded_prompt}
            ]
            for data_url in image_data_urls:
                content.append({"type": "input_image", "image_url": data_url})
            input_data = [{"role": "user", "content": content}]

        # 构建 tool
        tool: dict[str, Any] = {
            "type": "image_generation",
            "action": action,
            "size": params.size,
            "output_format": params.output_format,
            "quality": params.quality,
        }
        if params.output_format != "png" and params.output_compression is not None:
            tool["output_compression"] = params.output_compression

        body: dict[str, Any] = {
            "model": self.responses_model,
            "input": input_data,
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
        }

        start = perf_counter()
        logger.info(
            "[GPTImage2] Responses API request start "
            f"url={url} model={self.responses_model} action={action} "
            f"prompt_len={len(prompt)} input_images={len(image_data_urls)} "
            f"guard={self.responses_prompt_rewrite_guard} "
            f"size={params.size} quality={params.quality} "
            f"format={params.output_format} timeout={self.timeout}s"
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json=body,
                )
        except httpx.HTTPError as e:
            elapsed = self._elapsed_ms(start)
            error_msg = self._build_network_error_msg(e, url=url, elapsed_ms=elapsed)
            logger.warning(
                f"[GPTImage2] Responses API request failed action={action} {error_msg}"
            )
            raise RuntimeError(error_msg) from e

        elapsed = self._elapsed_ms(start)
        logger.debug(
            "[GPTImage2] Responses API response received "
            f"action={action} status={resp.status_code} elapsed_ms={elapsed} "
            f"response_bytes={len(resp.content)}"
        )

        if not resp.is_success:
            self._log_http_failure(resp, elapsed, f"Responses API {action}")
            raise RuntimeError(self._build_error_msg(resp.status_code, resp.text))

        data = resp.json()
        results = self._parse_responses_api_response(data)
        logger.info(
            "[GPTImage2] Responses API request success "
            f"action={action} elapsed_ms={elapsed} {self._result_summary(results)}"
        )
        return results

    def _parse_responses_api_response(self, data: dict) -> list[ImageResult]:
        """解析 Responses API 返回

        遍历 output，只处理 type == "image_generation_call" 的项
        从 result 中读取 base64 图片数据和 revised_prompt
        """
        output = data.get("output")
        if not isinstance(output, list) or not output:
            raise RuntimeError("API 返回结构异常：output 为空或非数组")

        results: list[ImageResult] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "image_generation_call":
                continue

            result = item.get("result")
            if result is None:
                continue

            b64_json: str | None = None
            if isinstance(result, str) and result.strip():
                b64_json = result
            elif isinstance(result, dict):
                # result 可能是 { "b64_json": ..., "image": ..., "data": ... }
                b64_json = (
                    result.get("b64_json") or result.get("image") or result.get("data")
                )

            if not b64_json:
                continue

            results.append(
                ImageResult(
                    b64_json=b64_json,
                    revised_prompt=item.get("revised_prompt"),
                )
            )

        if not results:
            output_types = [
                item.get("type") for item in output if isinstance(item, dict)
            ]
            logger.warning(
                "[GPTImage2] Responses API parse failed no image_generation_call "
                f"keys={list(data.keys())} output_types={output_types}"
            )
            raise RuntimeError("API 返回结构异常：未找到 image_generation_call 结果")

        return results

    # ── Responses API（Plan 模式） ──────────────────────────────

    async def plan_responses(
        self,
        input_data: list[dict] | str,
        model: str | None = None,
        *,
        temperature: float = 0.7,
        max_output_tokens: int = 700,
    ) -> str:
        """Plan 模式专用：调用 Responses API 做文本/多模态对话。

        显式禁用工具调用，避免规划阶段触发 image_generation。只解析文本输出。
        """
        url = f"{self.base_url}/responses"
        body: dict[str, object] = {
            "model": model or self.responses_model,
            "input": input_data,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "tools": [],
            "tool_choice": "none",
            "parallel_tool_calls": False,
        }

        start = perf_counter()
        logger.info(
            "[GPTImage2] plan Responses request start "
            f"url={url} model={body['model']} input_items={self._input_len(input_data)} "
            f"timeout={self.timeout}s"
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    headers={
                        **self._headers(),
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as e:
            elapsed = self._elapsed_ms(start)
            error_msg = self._build_network_error_msg(e, url=url, elapsed_ms=elapsed)
            logger.warning(f"[GPTImage2] plan Responses request failed {error_msg}")
            raise RuntimeError(error_msg) from e

        elapsed = self._elapsed_ms(start)
        logger.debug(
            "[GPTImage2] plan Responses response received "
            f"status={resp.status_code} elapsed_ms={elapsed} "
            f"response_bytes={len(resp.content)}"
        )

        if not resp.is_success:
            self._log_http_failure(resp, elapsed, "plan Responses")
            raise RuntimeError(self._build_error_msg(resp.status_code, resp.text))

        data = resp.json()
        content = self._parse_plan_responses_text(data)

        logger.info(
            "[GPTImage2] plan Responses request success "
            f"elapsed_ms={elapsed} response_chars={len(content)}"
        )
        return content

    @staticmethod
    def _input_len(input_data: list[dict] | str) -> int:
        return len(input_data) if isinstance(input_data, list) else 1

    def _parse_plan_responses_text(self, data: dict) -> str:
        """解析 Responses API 中的 assistant 文本输出。"""
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        output = data.get("output")
        if not isinstance(output, list) or not output:
            raise RuntimeError("API 返回结构异常：output 为空或非数组")

        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") not in {"output_text", "text"}:
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())

        if texts:
            return "\n".join(texts)

        output_types = [item.get("type") for item in output if isinstance(item, dict)]
        logger.warning(
            "[GPTImage2] plan Responses parse failed no text output "
            f"keys={list(data.keys())} output_types={output_types}"
        )
        if "image_generation_call" in output_types:
            raise RuntimeError(
                "Plan 模型返回了图像生成工具调用，而不是文本规划结果。"
                "请重试，或换用纯文本/多模态理解模型作为 Plan 模型。"
            )
        raise RuntimeError("API 返回结构异常：未找到文本输出")

    # ── 工具方法 ────────────────────────────────────────────────

    @staticmethod
    def _guess_ext(filepath: str) -> str:
        ext = os.path.splitext(filepath)[1].lstrip(".").lower()
        if ext in ("png", "jpeg", "jpg", "webp"):
            return ext
        return "png"
