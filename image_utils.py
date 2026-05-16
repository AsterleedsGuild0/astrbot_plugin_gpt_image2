"""GPT Image2 插件 - 图片处理工具

职责：
- 从消息链和引用消息中提取图片组件
- 图片路径转 data URL
- base64 图片保存为本地文件
- 输出目录和文件名管理
- MIME 类型和扩展名映射
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime

from astrbot.api.message_components import Image, Reply

MIME_MAP: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

EXT_MAP: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
}


def get_mime(output_format: str) -> str:
    """获取 MIME 类型"""
    return MIME_MAP.get(output_format, "image/png")


def get_ext(mime: str) -> str:
    """根据 MIME 获取文件扩展名"""
    return EXT_MAP.get(mime, "png")


def guess_image_mime(image: Image) -> str:
    """根据 Image 元数据尽量推断 MIME，失败时回退 png。"""
    source = getattr(image, "path", "") or getattr(image, "url", "") or image.file or ""
    source = str(source).split("?", 1)[0].lower()
    if source.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if source.endswith(".webp"):
        return "image/webp"
    return "image/png"


def extract_images_from_chain(
    chain: list,
    max_images: int = 4,
) -> list[Image]:
    """从消息链中提取 Image 组件，最多 max_images 张"""
    images: list[Image] = []
    for comp in chain:
        if isinstance(comp, Image):
            images.append(comp)
            if len(images) >= max_images:
                break
    return images


def extract_images_from_event(
    messages: list,
    max_images: int = 4,
) -> list[Image]:
    """从当前消息链提取 Image，然后从 Reply.chain 提取 Image

    顺序：
    1. 当前消息链中的 Image
    2. 当前消息链中的 Reply.chain 内的 Image
    """
    images: list[Image] = []

    # 第一步：当前消息链中的 Image
    images.extend(extract_images_from_chain(messages, max_images))

    if len(images) >= max_images:
        return images[:max_images]

    # 第二步：从 Reply.chain 中提取 Image
    for comp in messages:
        if isinstance(comp, Reply) and comp.chain:
            remaining = max_images - len(images)
            reply_images = extract_images_from_chain(comp.chain, remaining)
            images.extend(reply_images)
            if len(images) >= max_images:
                break

    return images[:max_images]


async def image_to_data_url(image: Image) -> str:
    """将 Image 组件转为 data URL

    Returns:
        str: data:image/{fmt};base64,{data}
    """
    b64 = await image.convert_to_base64()
    if b64.startswith("data:"):
        return b64
    mime = guess_image_mime(image)
    return f"data:{mime};base64,{b64}"


async def image_to_file_path(image: Image) -> str:
    """将 Image 组件转为本地文件路径"""
    return await image.convert_to_file_path()


def ensure_output_dir(plugin_data_dir: str) -> str:
    """确保输出目录存在并返回路径

    data/plugin_data/{plugin_name}/outputs/
    """
    output_dir = os.path.join(plugin_data_dir, "outputs")
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def save_base64_to_file(
    b64_data: str,
    output_dir: str,
    output_format: str = "png",
) -> str:
    """将 base64 图片数据保存到本地文件

    Args:
        b64_data: 纯 base64 字符串（不含 data: URI 前缀）
        output_dir: 输出目录
        output_format: png/jpeg/webp

    Returns:
        str: 保存的文件绝对路径
    """
    b64_data = extract_b64_from_data_url(b64_data)
    ext = output_format
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    unique_id = uuid.uuid4().hex[:8]
    filename = f"{timestamp}-{unique_id}.{ext}"
    filepath = os.path.join(output_dir, filename)

    image_bytes = base64.b64decode(b64_data)
    with open(filepath, "wb") as f:
        f.write(image_bytes)

    return os.path.abspath(filepath)


def extract_b64_from_data_url(data_url: str) -> str:
    """从 data URL 中提取纯 base64 数据

    例如 "data:image/png;base64,abc123" → "abc123"
    如果已经是纯 base64 则原样返回
    """
    if data_url.startswith("data:"):
        _, payload = data_url.split(",", 1)
        return payload
    return data_url
