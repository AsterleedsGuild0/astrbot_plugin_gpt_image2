"""Text-to-image rendering for GPT Image2 plugin replies.

The plugin keeps this renderer local because AstrBot's global T2I pipeline may
depend on templates that do not preserve plain-text line breaks well enough for
command/help output. Generated files are cache-like artifacts and should be
stored under AstrBot's data directory, not inside the plugin source directory.
"""

import logging
import os
import re
import shutil
import struct
import subprocess
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


logger = logging.getLogger("astrbot")
_CJK_SAMPLE = "中文测试国图像"
_EMOJI_SAMPLE = "🖼✅⚠🎨🙂"
_FONT_SUPPORT_CACHE: dict[tuple[str, tuple[int, ...]], bool] = {}
_EMOJI_TEXT_FALLBACKS = {
    "🖼": "[图]",
    "✅": "[OK]",
    "⚠": "[!]",
    "🎨": "[图]",
    "🙂": ":)",
    "🧾": "[文]",
    "🔄": "[~]",
    "❌": "[X]",
    "⏳": "[...]",
    "📌": "[-]",
    "💡": "[i]",
    "📷": "[图]",
    "📝": "[文]",
    "🔧": "[设]",
    "🚫": "[X]",
}


@dataclass(frozen=True)
class TextImageOptions:
    """Options for local text image rendering."""

    width: int = 1200
    font_size: int = 32
    padding: int = 48
    line_spacing: int = 14
    background: str = "#ffffff"
    foreground: str = "#111827"
    accent: str = "#60a5fa"
    font_path: str | None = None


class TextImageFontError(RuntimeError):
    """Raised when no usable CJK font can be found for text rendering."""


@dataclass(frozen=True)
class TextFontCascade:
    """Primary CJK font plus optional emoji/symbol fallback fonts."""

    primary: ImageFont.FreeTypeFont
    fallbacks: tuple[ImageFont.FreeTypeFont, ...] = ()

    @property
    def fonts(self) -> tuple[ImageFont.FreeTypeFont, ...]:
        return (self.primary, *self.fallbacks)


def render_text_to_image(
    text: str,
    output_dir: str | Path,
    *,
    options: TextImageOptions | None = None,
) -> str:
    """Render plain text into a PNG image and return the file path.

    Newlines are preserved exactly. Long lines are wrapped by rendered pixel
    width so the result remains readable in group chats.
    """
    options = options or TextImageOptions()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    fonts = _load_font_cascade(options.font_size, options.font_path, text)
    width = max(480, int(options.width))
    padding = max(16, int(options.padding))
    content_width = max(120, width - padding * 2)

    probe = Image.new("RGB", (width, 64), options.background)
    draw = ImageDraw.Draw(probe)
    lines = _wrap_text(text, draw, fonts, content_width)
    line_height = _line_height(fonts, options.line_spacing)
    height = max(
        padding * 2 + line_height,
        padding * 2 + len(lines) * line_height,
    )

    image = Image.new("RGB", (width, height), options.background)
    draw = ImageDraw.Draw(image)
    _draw_accent(draw, height, options)

    y = padding
    for line in lines:
        if line:
            _draw_text_with_fallback(
                draw, (padding, y), line, options.foreground, fonts
            )
        y += line_height

    filename = f"text-{uuid.uuid4().hex}.png"
    target = output_path / filename
    image.save(target, format="PNG", optimize=True)
    return str(target)


def _draw_accent(
    draw: ImageDraw.ImageDraw, height: int, options: TextImageOptions
) -> None:
    """Draw a subtle left accent bar to distinguish bot-rendered text cards."""
    bar_width = max(4, options.padding // 8)
    draw.rounded_rectangle(
        (
            options.padding // 3,
            options.padding,
            options.padding // 3 + bar_width,
            height - options.padding,
        ),
        radius=bar_width,
        fill=options.accent,
    )


def _wrap_text(
    text: str,
    draw: ImageDraw.ImageDraw,
    fonts: TextFontCascade,
    max_width: int,
) -> list[str]:
    """Wrap text by rendered width while preserving explicit newlines."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    wrapped: list[str] = []

    for raw_line in normalized.split("\n"):
        if raw_line == "":
            wrapped.append("")
            continue
        wrapped.extend(_wrap_line(raw_line, draw, fonts, max_width))

    return wrapped or [""]


def _wrap_line(
    line: str,
    draw: ImageDraw.ImageDraw,
    fonts: TextFontCascade,
    max_width: int,
) -> list[str]:
    if _text_width(draw, line, fonts) <= max_width:
        return [line]

    continuation_prefix = _continuation_prefix(line)
    result: list[str] = []
    current = ""

    for cluster in _text_clusters(line):
        candidate = current + cluster
        if current and _text_width(draw, candidate, fonts) > max_width:
            result.append(current.rstrip())
            current = continuation_prefix + cluster
        else:
            current = candidate

    if current:
        result.append(current.rstrip())
    return result or [line]


def _continuation_prefix(line: str) -> str:
    """Indent wrapped continuation lines for bullets and numbered lists."""
    match = re.match(r"^\s*", line)
    leading = match.group(0) if match else ""
    stripped = line.strip()
    if re.match(r"^([-*•]|\d+[.)])\s+", stripped):
        return leading + "  "
    return leading


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    fonts: TextFontCascade | ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> float:
    if not text:
        return 0
    if isinstance(fonts, TextFontCascade):
        return sum(
            _font_text_width(draw, run, font) for run, font in _font_runs(text, fonts)
        )
    return _font_text_width(draw, text, fonts)


def _font_text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> float:
    try:
        return draw.textlength(text, font=font)
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]


def _line_height(
    fonts: TextFontCascade | ImageFont.FreeTypeFont | ImageFont.ImageFont,
    line_spacing: int,
) -> int:
    candidate_fonts = fonts.fonts if isinstance(fonts, TextFontCascade) else (fonts,)
    base = 0
    for font in candidate_fonts:
        base = max(base, _font_line_height(font))
    return max(24, int(base + line_spacing))


def _font_line_height(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    try:
        bbox = font.getbbox("国Ag")
        return int(bbox[3] - bbox[1])
    except Exception:
        return 32


def _draw_text_with_fallback(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    fill: str,
    fonts: TextFontCascade,
) -> None:
    x, y = xy
    for run, font in _font_runs(text, fonts):
        try:
            draw.text((x, y), run, fill=fill, font=font, embedded_color=True)
        except TypeError:
            draw.text((x, y), run, fill=fill, font=font)
        except Exception:
            draw.text((x, y), run, fill=fill, font=font)
        x += _font_text_width(draw, run, font)


def _font_runs(
    text: str,
    fonts: TextFontCascade,
) -> list[tuple[str, ImageFont.FreeTypeFont]]:
    runs: list[tuple[str, ImageFont.FreeTypeFont]] = []
    current_font: ImageFont.FreeTypeFont | None = None
    current_text = ""

    for cluster in _text_clusters(text):
        run_text, font = _resolve_cluster(cluster, fonts)
        if not run_text:
            continue
        if current_font is font:
            current_text += run_text
            continue
        if current_text and current_font is not None:
            runs.append((current_text, current_font))
        current_text = run_text
        current_font = font

    if current_text and current_font is not None:
        runs.append((current_text, current_font))
    return runs


def _resolve_cluster(
    cluster: str,
    fonts: TextFontCascade,
) -> tuple[str, ImageFont.FreeTypeFont]:
    font = _font_for_cluster(cluster, fonts)
    if font is not None:
        return cluster, font

    replacement = _emoji_text_fallback(cluster)
    if replacement:
        fallback_font = _font_for_cluster(replacement, fonts)
        return replacement, fallback_font or fonts.primary

    return cluster, fonts.primary


def _font_for_cluster(
    cluster: str,
    fonts: TextFontCascade,
) -> ImageFont.FreeTypeFont | None:
    candidates = (
        (*fonts.fallbacks, fonts.primary)
        if _prefers_emoji_font(cluster)
        else fonts.fonts
    )
    for font in candidates:
        if _font_supports_cluster(font, cluster):
            return font
    return None


def _font_supports_cluster(font: ImageFont.FreeTypeFont, cluster: str) -> bool:
    codepoints = _font_check_codepoints(cluster)
    if not codepoints:
        return True
    path = getattr(font, "path", None)
    if not path:
        return True
    return _font_file_supports_codepoints_cached(Path(path), codepoints)


def _text_clusters(text: str) -> list[str]:
    clusters: list[str] = []
    i = 0
    while i < len(text):
        start = i
        i += 1

        if (
            _is_regional_indicator(ord(text[start]))
            and i < len(text)
            and _is_regional_indicator(ord(text[i]))
        ):
            i += 1

        while i < len(text):
            codepoint = ord(text[i])
            if (
                _is_variation_selector(codepoint)
                or _is_emoji_modifier(codepoint)
                or unicodedata.combining(text[i])
            ):
                i += 1
                continue
            if codepoint == 0x200D and i + 1 < len(text):
                i += 2
                continue
            break
        clusters.append(text[start:i])
    return clusters


def _font_check_codepoints(text: str) -> tuple[int, ...]:
    return tuple(
        ord(char)
        for char in text
        if not _is_variation_selector(ord(char)) and ord(char) != 0x200D
    )


def _prefers_emoji_font(text: str) -> bool:
    return any(_is_emoji_like(ord(char)) for char in text)


def _emoji_text_fallback(text: str) -> str:
    key = "".join(
        char
        for char in text
        if not _is_variation_selector(ord(char)) and ord(char) != 0x200D
    )
    fallback = _EMOJI_TEXT_FALLBACKS.get(key, "")
    if fallback:
        return fallback
    if _prefers_emoji_font(key):
        return "[emoji]"
    return ""


def _is_emoji_like(codepoint: int) -> bool:
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0x2300 <= codepoint <= 0x23FF
        or 0x2B00 <= codepoint <= 0x2BFF
    )


def _is_regional_indicator(codepoint: int) -> bool:
    return 0x1F1E6 <= codepoint <= 0x1F1FF


def _is_variation_selector(codepoint: int) -> bool:
    return 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF


def _is_emoji_modifier(codepoint: int) -> bool:
    return 0x1F3FB <= codepoint <= 0x1F3FF


def _load_font_cascade(
    size: int,
    configured_path: str | None = None,
    text: str = "",
) -> TextFontCascade:
    primary = _load_font(size, configured_path)
    fallback_fonts = (
        _load_fallback_fonts(size, primary) if _prefers_emoji_font(text) else []
    )
    return TextFontCascade(primary=primary, fallbacks=tuple(fallback_fonts))


def _load_fallback_fonts(
    size: int,
    primary: ImageFont.FreeTypeFont,
) -> list[ImageFont.FreeTypeFont]:
    primary_path = str(Path(getattr(primary, "path", "")).expanduser())
    checked_paths: list[str] = []
    fonts: list[ImageFont.FreeTypeFont] = []

    for candidate in _fallback_font_candidates():
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        path_str = str(path)
        if path_str in checked_paths or path_str == primary_path:
            continue
        checked_paths.append(path_str)
        if not path.is_file():
            continue
        if not _font_file_supports_any_emoji(path):
            logger.debug(
                f"[GPTImage2] text image fallback font skipped no emoji cmap path={path}"
            )
            continue
        try:
            font = ImageFont.truetype(str(path), size=size)
        except OSError:
            logger.debug(
                f"[GPTImage2] text image fallback font load failed path={path}"
            )
            continue
        fonts.append(font)
        logger.info(f"[GPTImage2] text image fallback font selected path={path}")
        if len(fonts) >= 4:
            break

    if not fonts:
        logger.debug("[GPTImage2] text image emoji fallback font not found")
    return fonts


def _load_font(
    size: int,
    configured_path: str | None = None,
) -> ImageFont.FreeTypeFont:
    checked_paths: list[str] = []
    for candidate in _font_candidates(configured_path):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        path_str = str(path)
        if path_str in checked_paths:
            continue
        checked_paths.append(path_str)
        if path.is_file():
            if not _font_file_supports_chinese(path):
                logger.debug(
                    f"[GPTImage2] text image font skipped no CJK cmap path={path}"
                )
                continue
            try:
                font = ImageFont.truetype(str(path), size=size)
            except OSError:
                logger.debug(f"[GPTImage2] text image font load failed path={path}")
                continue
            logger.info(f"[GPTImage2] text image font selected path={path}")
            return font
    raise TextImageFontError(
        "未找到可用于渲染中文的字体。请在插件配置 text_image_font_path 中填写中文字体文件路径。"
    )


def _font_file_supports_codepoints_cached(
    path: Path,
    codepoints: tuple[int, ...],
) -> bool:
    key = (str(path), tuple(sorted(set(codepoints))))
    if key not in _FONT_SUPPORT_CACHE:
        _FONT_SUPPORT_CACHE[key] = _font_file_supports_codepoints(path, list(key[1]))
    return _FONT_SUPPORT_CACHE[key]


def _font_file_supports_codepoints(path: Path, codepoints: list[int]) -> bool:
    if not codepoints:
        return True
    try:
        data = path.read_bytes()
        for offset in _font_offsets(data):
            if _font_at_offset_supports_codepoints(data, offset, codepoints):
                return True
    except Exception as exc:
        logger.debug(
            "[GPTImage2] text image font cmap check failed "
            f"path={path} error={type(exc).__name__}: {exc}"
        )
    return False


def _font_file_supports_any_emoji(path: Path) -> bool:
    return any(
        _font_file_supports_codepoints_cached(path, (ord(char),))
        for char in _EMOJI_SAMPLE
        if not _is_variation_selector(ord(char))
    )


def _font_file_supports_chinese(path: Path) -> bool:
    """Check cmap tables so Latin fallback fonts do not pass as CJK fonts."""
    return _font_file_supports_codepoints_cached(
        path,
        tuple(ord(char) for char in _CJK_SAMPLE),
    )


def _font_offsets(data: bytes) -> list[int]:
    if data[:4] != b"ttcf":
        return [0]
    if len(data) < 12:
        return []
    count = struct.unpack_from(">I", data, 8)[0]
    offsets: list[int] = []
    for index in range(count):
        pos = 12 + index * 4
        if pos + 4 > len(data):
            break
        offsets.append(struct.unpack_from(">I", data, pos)[0])
    return offsets


def _font_at_offset_supports_codepoints(
    data: bytes,
    font_offset: int,
    codepoints: list[int],
) -> bool:
    if font_offset < 0 or font_offset + 12 > len(data):
        return False
    table_count = struct.unpack_from(">H", data, font_offset + 4)[0]
    record_offset = font_offset + 12

    for index in range(table_count):
        pos = record_offset + index * 16
        if pos + 16 > len(data):
            return False
        tag = data[pos : pos + 4]
        if tag != b"cmap":
            continue
        cmap_offset = struct.unpack_from(">I", data, pos + 8)[0]
        cmap_length = struct.unpack_from(">I", data, pos + 12)[0]
        return _cmap_supports_codepoints(data, cmap_offset, cmap_length, codepoints)
    return False


def _cmap_supports_codepoints(
    data: bytes,
    cmap_offset: int,
    cmap_length: int,
    codepoints: list[int],
) -> bool:
    if cmap_offset < 0 or cmap_offset + 4 > len(data):
        return False
    cmap_end = min(len(data), cmap_offset + cmap_length)
    subtable_count = struct.unpack_from(">H", data, cmap_offset + 2)[0]

    for index in range(subtable_count):
        record = cmap_offset + 4 + index * 8
        if record + 8 > cmap_end:
            break
        subtable_offset = cmap_offset + struct.unpack_from(">I", data, record + 4)[0]
        if subtable_offset + 2 > cmap_end:
            continue
        fmt = struct.unpack_from(">H", data, subtable_offset)[0]
        if fmt == 4 and _cmap_format4_supports(
            data, subtable_offset, cmap_end, codepoints
        ):
            return True
        if fmt in {12, 13} and _cmap_format12_or_13_supports(
            data,
            subtable_offset,
            cmap_end,
            codepoints,
        ):
            return True
    return False


def _cmap_format4_supports(
    data: bytes,
    offset: int,
    cmap_end: int,
    codepoints: list[int],
) -> bool:
    if offset + 16 > cmap_end:
        return False
    length = struct.unpack_from(">H", data, offset + 2)[0]
    end = min(cmap_end, offset + length)
    seg_count = struct.unpack_from(">H", data, offset + 6)[0] // 2
    end_codes = offset + 14
    start_codes = end_codes + seg_count * 2 + 2
    id_deltas = start_codes + seg_count * 2
    id_range_offsets = id_deltas + seg_count * 2
    if id_range_offsets + seg_count * 2 > end:
        return False

    for codepoint in codepoints:
        if codepoint > 0xFFFF:
            return False
        found = False
        for index in range(seg_count):
            end_code = struct.unpack_from(">H", data, end_codes + index * 2)[0]
            start_code = struct.unpack_from(">H", data, start_codes + index * 2)[0]
            if start_code <= codepoint <= end_code and end_code != 0xFFFF:
                found = _cmap_format4_has_glyph(
                    data,
                    end,
                    codepoint,
                    start_code,
                    id_deltas + index * 2,
                    id_range_offsets + index * 2,
                )
                break
        if not found:
            return False
    return True


def _cmap_format4_has_glyph(
    data: bytes,
    end: int,
    codepoint: int,
    start_code: int,
    id_delta_offset: int,
    id_range_offset_offset: int,
) -> bool:
    id_delta = struct.unpack_from(">h", data, id_delta_offset)[0]
    id_range_offset = struct.unpack_from(">H", data, id_range_offset_offset)[0]
    if id_range_offset == 0:
        return ((codepoint + id_delta) & 0xFFFF) != 0

    glyph_index_offset = (
        id_range_offset_offset + id_range_offset + (codepoint - start_code) * 2
    )
    if glyph_index_offset + 2 > end:
        return False
    glyph = struct.unpack_from(">H", data, glyph_index_offset)[0]
    if glyph == 0:
        return False
    return ((glyph + id_delta) & 0xFFFF) != 0


def _cmap_format12_or_13_supports(
    data: bytes,
    offset: int,
    cmap_end: int,
    codepoints: list[int],
) -> bool:
    if offset + 16 > cmap_end:
        return False
    length = struct.unpack_from(">I", data, offset + 4)[0]
    end = min(cmap_end, offset + length)
    group_count = struct.unpack_from(">I", data, offset + 12)[0]
    groups = offset + 16
    if groups + group_count * 12 > end:
        return False

    for codepoint in codepoints:
        found = False
        for index in range(group_count):
            pos = groups + index * 12
            start = struct.unpack_from(">I", data, pos)[0]
            stop = struct.unpack_from(">I", data, pos + 4)[0]
            glyph = struct.unpack_from(">I", data, pos + 8)[0]
            if start <= codepoint <= stop and glyph != 0:
                found = True
                break
        if not found:
            return False
    return True


def _font_candidates(configured_path: str | None = None) -> list[str]:
    env_path = os.environ.get("GPT_IMAGE2_TEXT_FONT")
    windows_dir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    windows_fonts = []
    if windows_dir:
        fonts_dir = Path(windows_dir) / "Fonts"
        windows_fonts = [
            str(fonts_dir / "msyh.ttc"),
            str(fonts_dir / "msyh.ttf"),
            str(fonts_dir / "simhei.ttf"),
            str(fonts_dir / "simsun.ttc"),
            str(fonts_dir / "NotoSansCJK-Regular.ttc"),
        ]

    return [
        configured_path or "",
        env_path or "",
        *_fc_match_candidates(),
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        # Linux common CJK fonts
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/usr/share/fonts/adobe-source-han-sans/SourceHanSansSC-Regular.otf",
        "/usr/share/fonts/source-han-sans/SourceHanSansSC-Regular.otf",
        # Windows
        *windows_fonts,
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]


def _fallback_font_candidates() -> list[str]:
    env_path = os.environ.get("GPT_IMAGE2_EMOJI_FONT")
    windows_dir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    windows_fonts = []
    if windows_dir:
        fonts_dir = Path(windows_dir) / "Fonts"
        windows_fonts = [
            str(fonts_dir / "seguiemj.ttf"),
            str(fonts_dir / "seguisym.ttf"),
            str(fonts_dir / "SegoeUIEmoji.ttf"),
        ]

    return [
        env_path or "",
        *_fc_match_fallback_candidates(),
        # macOS
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "/System/Library/Fonts/Apple Symbols.ttf",
        "/System/Library/Fonts/Supplemental/Apple Symbols.ttf",
        # Linux common emoji/symbol fonts
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
        "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
        "/usr/share/fonts/truetype/symbola/Symbola.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        # Windows
        *windows_fonts,
        "C:/Windows/Fonts/seguiemj.ttf",
        "C:/Windows/Fonts/seguisym.ttf",
    ]


def _fc_match_fallback_candidates() -> list[str]:
    if not shutil.which("fc-match"):
        return []

    queries = [
        "Noto Color Emoji",
        "Noto Emoji",
        "Segoe UI Emoji",
        "Apple Color Emoji",
        "Symbola",
        "emoji",
        "sans",
    ]
    paths: list[str] = []
    for query in queries:
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", query],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        path = result.stdout.strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _fc_match_candidates() -> list[str]:
    """Use fontconfig on Linux when available to find installed CJK fonts."""
    if not shutil.which("fc-match"):
        return []

    queries = [
        "Noto Sans CJK SC",
        "Noto Sans CJK",
        "Source Han Sans SC",
        "WenQuanYi Micro Hei",
        "WenQuanYi Zen Hei",
        "Microsoft YaHei",
        "SimHei",
        "sans:lang=zh-cn",
    ]
    paths: list[str] = []
    for query in queries:
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", query],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        path = result.stdout.strip()
        if path and path not in paths:
            paths.append(path)
    return paths
