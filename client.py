"""GPT Image2 插件 - API 客户端

职责：
- 封装 Images API 文生图与图像编辑
- 封装 Responses API 图像生成工具调用
- 解析 API 返回
- 归一化错误信息
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


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
        timeout: int = 600,
        response_format_b64_json: bool = True,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.responses_model = responses_model
        self.timeout = timeout
        self.response_format_b64_json = response_format_b64_json

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
        }

    def _build_error_msg(self, status_code: int, body: Any) -> str:
        """构建不泄露 API Key 的错误消息"""
        try:
            if isinstance(body, str) and body:
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
        return f"HTTP {status_code}"

    # ── Images API ──────────────────────────────────────────────

    async def generate_images_api(
        self,
        prompt: str,
        params: ImageParams,
    ) -> list[ImageResult]:
        """Images API 文生图

        POST {base_url}/images/generations
        """
        url = f"{self.base_url}/images/generations"
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
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

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
            )

        if not resp.is_success:
            raise RuntimeError(self._build_error_msg(resp.status_code, resp.text))

        data = resp.json()
        return self._parse_images_api_response(data)

    async def edit_images_api(
        self,
        prompt: str,
        image_paths: list[str],
        params: ImageParams,
    ) -> list[ImageResult]:
        """Images API 图像编辑

        POST {base_url}/images/edits
        Content-Type: multipart/form-data
        """
        url = f"{self.base_url}/images/edits"

        multipart_data: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
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
        for idx, path in enumerate(image_paths):
            with open(path, "rb") as f:
                file_bytes = f.read()
            ext = self._guess_ext(path)
            mime_ext = "jpeg" if ext == "jpg" else ext
            files.append(
                (
                    "image[]",
                    (f"input-{idx + 1}.{ext}", file_bytes, f"image/{mime_ext}"),
                )
            )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                headers=self._headers(),
                data=multipart_data,
                files=files,
            )

        if not resp.is_success:
            raise RuntimeError(self._build_error_msg(resp.status_code, resp.text))

        data = resp.json()
        return self._parse_images_api_response(data)

    def _parse_images_api_response(self, data: dict | list) -> list[ImageResult]:
        """解析 Images API 返回

        data[*].b64_json
        data[*].url
        data[*].revised_prompt
        """
        items = data if isinstance(data, list) else data.get("data", [])
        if not isinstance(items, list) or not items:
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
        n = max(1, int(params.n or 1))
        if n == 1:
            return await self._call_responses_api(
                prompt, image_data_urls, params, action
            )

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
            return results
        if first_error:
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
        guarded_prompt = f"{PROMPT_REWRITE_GUARD_PREFIX}\n{prompt}"

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
            "tool_choice": "required",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
            )

        if not resp.is_success:
            raise RuntimeError(self._build_error_msg(resp.status_code, resp.text))

        data = resp.json()
        return self._parse_responses_api_response(data)

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
            raw = json.dumps(data, ensure_ascii=False)[:500]
            raise RuntimeError(
                f"API 返回结构异常：未找到 image_generation_call 结果。原始响应：{raw}"
            )

        return results

    # ── 工具方法 ────────────────────────────────────────────────

    @staticmethod
    def _guess_ext(filepath: str) -> str:
        ext = os.path.splitext(filepath)[1].lstrip(".").lower()
        if ext in ("png", "jpeg", "jpg", "webp"):
            return ext
        return "png"
