"""Provider config, stats, failure classification, and retry logic for GPT Image2.

Responsibility:
- ImageAPIProviderConfig dataclass
- Provider config parsing (primary/fallback/authoritative_fallback, capabilities, api_mode)
- provider_id construction
- Adaptive priority sorting and health scoring
- Provider stats JSON load/save/update/summary
- Provider failures JSONL append, trim, and recent read
- Failure reason/status code classification, retryable judgment
- Provider error summary, provider user label
- Provider retry notice config parsing, session config read/write
- ProviderManager class that holds runtime state and delegates to pure functions

This module has no dependency on AstrBot event/send, only on ``astrbot.api.logger``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from time import time

from astrbot.api import logger


# ── Utility formatting functions ─────────────────────────────────


def safe_text_preview(text: str, *, limit: int = 160) -> str:
    """Compact safe text preview for diagnostics."""
    normalized = " ".join(text.replace("\x00", "\ufffd").split())
    if len(normalized) > limit:
        normalized = normalized[:limit] + "\u2026"
    return repr(normalized)


def safe_markdown_preview(text: str, *, limit: int = 160) -> str:
    """Compact provider errors for Markdown cards without inline-code breakage."""
    normalized = " ".join(str(text).replace("\x00", "\ufffd").split())
    normalized = normalized.replace("`", "'")
    if len(normalized) > limit:
        normalized = normalized[:limit].rstrip() + "\u2026"
    return normalized or "-"


def format_duration(seconds: int) -> str:
    """Format seconds into a human-readable duration string."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if rest == 0 else f"{minutes}m{rest}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h{minutes}m"


# ── ImageAPIProviderConfig dataclass ──────────────────────────────


@dataclass(frozen=True)
class ImageAPIProviderConfig:
    """Draw/edit API provider config. Plan mode keeps its own config path."""

    name: str
    api_key: str
    base_url: str
    model: str  # Images API model; empty = unsupported
    responses_model: str  # Responses API model; empty = unsupported
    provider_id: str
    configured_order: int
    role: str = "normal"  # "primary" | "normal" | "authoritative_fallback"
    adaptive: bool = True

    @property
    def images_supported(self) -> bool:
        return bool(self.model)

    @property
    def responses_supported(self) -> bool:
        return bool(self.responses_model)

    def supports_mode(self, mode: str) -> bool:
        if mode == "images":
            return self.images_supported
        if mode == "responses":
            return self.responses_supported
        return False

    def model_for_mode(self, mode: str) -> str:
        """Return the model name for the given mode; caller must check ``supports_mode`` first."""
        if mode == "images":
            return self.model
        return self.responses_model


# ── Failure classification constants ─────────────────────────────


FAILURE_REASON_ORDER = [
    "network_timeout",
    "network_connect",
    "network_proxy",
    "network_protocol",
    "http_400",
    "http_401",
    "http_403",
    "http_404",
    "http_413",
    "http_422",
    "http_429",
    "http_5xx",
    "http_524",
    "html_error_page",
    "api_schema_error",
    "provider_compatibility",
    "unknown",
]


# ── Pure functions (no instance state) ───────────────────────────


def normalize_api_mode(value: object) -> str:
    """Normalize api_mode config value to ``'images'`` or ``'responses'``."""
    mode = str(value or "images").strip().lower()
    return mode if mode in {"images", "responses"} else "images"


def normalize_provider_role(value: object) -> str:
    """Normalize provider role string, handling common aliases."""
    role = str(value or "normal").strip().lower().replace("-", "_")
    aliases = {
        "official": "authoritative_fallback",
        "official_fallback": "authoritative_fallback",
        "authoritative": "authoritative_fallback",
        "authority": "authoritative_fallback",
        "authority_fallback": "authoritative_fallback",
        "fallback_authoritative": "authoritative_fallback",
    }
    role = aliases.get(role, role)
    return role if role in {"normal", "authoritative_fallback"} else "normal"


def normalize_bool(value: object, *, default: bool) -> bool:
    """Normalize a flexible config boolean value."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def build_provider_id(
    name: str,
    base_url: str,
    model: str,
    responses_model: str,
) -> str:
    """Build a stable provider_id from name or config fingerprint."""
    if name.strip():
        return f"name:{name.strip().lower()}"
    source = "|".join(
        [
            base_url.strip().rstrip("/"),
            model.strip(),
            responses_model.strip(),
        ]
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return f"config:{digest}"


def classify_failure_reason(error_msg: str) -> str:
    """Classify failure reason from error message for telemetry."""
    lower = error_msg.lower()

    # Network errors
    if "timeoutexception" in lower or (
        "\u7f51\u7edc\u8bf7\u6c42\u5931\u8d25" in error_msg
        and "\u8d85\u65f6" in error_msg
    ):
        return "network_timeout"
    if "proxyerror" in lower or "\u4ee3\u7406\u8fde\u63a5\u5931\u8d25" in error_msg:
        return "network_proxy"
    if "remoteprotocolerror" in lower or "\u534f\u8bae\u5f02\u5e38" in error_msg:
        return "network_protocol"
    if (
        "connecterror" in lower
        or "\u8fde\u63a5\u5931\u8d25" in error_msg
        or "connection refused" in lower
    ):
        return "network_connect"
    if "networkerror" in lower or "\u7f51\u7edc\u4f20\u8f93\u5f02\u5e38" in error_msg:
        return "network_connect"

    # HTML error page
    if "html \u9519\u8bef\u9875" in error_msg or ("html" in lower and "error" in lower):
        return "html_error_page"

    # HTTP status code classification
    if error_msg.startswith("HTTP "):
        parts = error_msg.split(maxsplit=2)
        try:
            status_code = int(parts[1])
        except (IndexError, ValueError):
            return "unknown"
        if status_code == 400:
            return "http_400"
        if status_code == 401:
            return "http_401"
        if status_code == 403:
            return "http_403"
        if status_code == 404:
            return "http_404"
        if status_code == 413:
            return "http_413"
        if status_code == 422:
            return "http_422"
        if status_code == 429:
            return "http_429"
        if status_code == 524:
            return "http_524"
        if 500 <= status_code < 600:
            return "http_5xx"

    # API schema errors
    if (
        "api \u8fd4\u56de\u9519\u8bef" in error_msg
        or "api \u8fd4\u56de\u7ed3\u6784\u5f02\u5e38" in error_msg
    ):
        return "api_schema_error"

    # Provider compatibility
    compat_phrases = {
        "does not support",
        "not supported",
        "unsupported",
        "unknown parameter",
        "invalid parameter",
        "unexpected parameter",
        "not allowed",
        "cannot",
    }
    if any(phrase in lower for phrase in compat_phrases):
        return "provider_compatibility"

    return "unknown"


def classify_http_status_code(error_msg: str) -> int | None:
    """Extract HTTP status code from error message, if parseable."""
    if error_msg.startswith("HTTP "):
        parts = error_msg.split(maxsplit=2)
        try:
            return int(parts[1])
        except (IndexError, ValueError):
            pass
    return None


def failure_reason_is_retryable(reason_key: str) -> bool:
    """Whether a failure reason should trigger fallback retry."""
    non_retryable = {"http_401", "http_403"}
    return reason_key not in non_retryable


def should_try_next_image_provider(error_msg: str) -> bool:
    """Return whether a draw/edit failure is likely provider-specific."""
    lower = error_msg.lower()
    if is_image_input_unsupported(error_msg):
        return True
    if (
        "\u7f51\u7edc\u8bf7\u6c42\u5931\u8d25" in error_msg
        or "connecterror" in lower
        or "timeout" in lower
        or "timed out" in lower
        or "connection" in lower
        or "api \u8fd4\u56de\u9519\u8bef" in error_msg
        or "api \u8fd4\u56de\u7ed3\u6784\u5f02\u5e38" in error_msg
        or "html \u9519\u8bef\u9875" in error_msg
        or "invalid endpoint for image generation models" in lower
    ):
        return True
    if not error_msg.startswith("HTTP "):
        return False
    parts = error_msg.split(maxsplit=2)
    try:
        status_code = int(parts[1])
    except (IndexError, ValueError):
        return False
    return status_code in {
        400,
        401,
        403,
        408,
        409,
        429,
        500,
        502,
        503,
        504,
        520,
        522,
        524,
    }


def is_image_input_unsupported(error_msg: str) -> bool:
    """Check if error indicates the model does not support image input."""
    lower = error_msg.lower()
    return (
        "does not support image input" in lower
        or "does not support image" in lower
        or ("cannot read" in lower and "image" in lower)
        or "image input is not supported" in lower
        or ("model does not support" in lower and "image" in lower)
    )


def provider_stat_int(item: dict, key: str) -> int:
    """Safely extract an integer stat from a provider stats dict."""
    try:
        return int(item.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def provider_stat_float(item: dict, key: str) -> float:
    """Safely extract a float stat from a provider stats dict."""
    try:
        return float(item.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def provider_health_score(item: dict, now: float) -> float:
    """Compute a health score from a provider stats item."""
    success_count = provider_stat_int(item, "success_count")
    failure_count = provider_stat_int(item, "failure_count")
    consecutive_failures = provider_stat_int(item, "consecutive_failures")
    last_success_at = provider_stat_float(item, "last_success_at")
    last_failure_at = provider_stat_float(item, "last_failure_at")
    cooldown_until = provider_stat_float(item, "cooldown_until")

    score = min(success_count, 20) * 2.0
    score -= min(failure_count, 20) * 1.5
    score -= min(consecutive_failures, 10) * 8.0

    if last_success_at > 0:
        success_age = max(0.0, now - last_success_at)
        score += max(0.0, 50.0 * (1.0 - success_age / 3600.0))
    if last_failure_at > 0:
        failure_age = max(0.0, now - last_failure_at)
        score -= max(0.0, 20.0 * (1.0 - failure_age / 900.0))
    if cooldown_until > now:
        score -= 100.0
    return score


def parse_fallback_api_provider_string(value: str) -> dict[str, str]:
    """Parse a fallback provider string (JSON, URL-only, or key=value format)."""
    text = value.strip()
    if not text:
        return {}

    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(
                f"[GPTImage2] fallback API provider JSON item parse failed error={e}"
            )
            return {}
        return parsed if isinstance(parsed, dict) else {}

    if "=" not in text:
        return {"base_url": text}

    data: dict[str, str] = {}
    for chunk in text.replace(";", ",").split(","):
        if "=" not in chunk:
            continue
        key, raw = chunk.split("=", 1)
        key = key.strip().lower().replace("-", "_")
        raw = raw.strip()
        if key in {"url", "base", "baseurl", "base_url"}:
            key = "base_url"
        elif key in {"key", "apikey", "api_key"}:
            key = "api_key"
        elif key in {"responses", "responses_model", "response_model"}:
            key = "responses_model"
        elif key in {"api", "api_mode", "apimode"}:
            key = "api_mode"
        elif key in {"role", "provider_role"}:
            key = "role"
        elif key in {"adaptive", "adapt", "adaptive_priority"}:
            key = "adaptive"
        elif key in {"cap", "capabilities", "capability"}:
            key = "capabilities"
        elif key not in {
            "name",
            "base_url",
            "api_key",
            "api_mode",
            "role",
            "adaptive",
            "model",
            "responses_model",
            "capabilities",
        }:
            continue
        data[key] = raw
    return data


def provider_error_summary(provider_errors: list[tuple[str, str]]) -> str:
    """Build a Markdown error summary for all failed providers."""
    if not provider_errors:
        return ""
    lines = ["\n\n\u5df2\u5c1d\u8bd5\u7684 API \u7ad9\u70b9\uff1a"]
    for name, error in provider_errors:
        lines.append(f"- **{name}**\uff1a{safe_markdown_preview(error, limit=160)}")
    return "\n".join(lines)


def provider_user_label(
    provider: ImageAPIProviderConfig,
    global_mode: str = "",
) -> str:
    """Build a user-facing label for a provider."""
    suffix = (
        " / \u6743\u5a01\u5157\u5e95"
        if provider.role == "authoritative_fallback"
        else ""
    )
    mode_label = f" / {global_mode}" if global_mode else ""
    return f"{provider.name}{mode_label}{suffix}"


def prompt_rewrite_guard_config_key(api_mode: str) -> str:
    """Return the config key for the given API mode's prompt rewrite guard."""
    return (
        "responses_prompt_rewrite_guard"
        if api_mode == "responses"
        else "images_prompt_rewrite_guard"
    )


def prompt_rewrite_guard_default(api_mode: str) -> bool:
    """Return the default value for a prompt rewrite guard."""
    return api_mode == "responses"


def prompt_rewrite_guard_status(enabled: bool) -> str:
    """Return a human-readable status string for a guard setting."""
    return "\u2705 \u5f00\u542f" if enabled else "\u5173\u95ed"


def parse_bool_switch(value: object) -> bool | None:
    """Parse a command switch value into a boolean, or None if invalid."""
    text = str(value or "").strip().lower()
    if text in {
        "1",
        "true",
        "yes",
        "y",
        "on",
        "enable",
        "enabled",
        "\u5f00\u542f",
        "\u5f00",
        "\u542f\u7528",
    }:
        return True
    if text in {
        "0",
        "false",
        "no",
        "n",
        "off",
        "disable",
        "disabled",
        "\u5173\u95ed",
        "\u5173",
        "\u7981\u7528",
    }:
        return False
    return None


def trim_jsonl(path: Path, max_lines: int = 5000) -> None:
    """Trim JSONL file to keep only the last ``max_lines`` entries."""
    try:
        if not path.exists() or path.stat().st_size < 1024 * 100:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= max_lines:
            return
        with path.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines[-max_lines:]) + "\n")
    except Exception as e:
        logger.debug(f"[GPTImage2] jsonl trim skipped error={type(e).__name__}: {e}")


def read_recent_failure_records(
    count: int,
    *,
    path: Path | None = None,
) -> list[dict]:
    """Read the last ``count`` records from ``provider_failures.jsonl``."""
    if path is None:
        return []
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        selected = lines[-count:]
        records: list[dict] = []
        for line in selected:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
    except Exception:
        return []


def redact_provider_stats(stats_data: dict) -> dict:
    """Return a copy of stats dict with API-key-like data removed from URLs."""
    import copy as _copy
    from urllib.parse import urlparse as _urlparse_inner

    result = _copy.deepcopy(stats_data)
    providers = result.get("providers", {})
    if not isinstance(providers, dict):
        return result
    for item in providers.values():
        if not isinstance(item, dict):
            continue
        base_url = item.get("base_url", "")
        if base_url:
            try:
                parsed = _urlparse_inner(base_url)
                redacted = f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
                item["base_url"] = redacted
            except Exception:
                item["base_url"] = "<redacted>"
    return result


# ── ProviderManager: instance state container ────────────────────


class ProviderManager:
    """Holds provider runtime state and delegates to module-level helpers.

    This class encapsulates:
    - Provider config parsing (primary/fallback/authoritative_fallback)
    - Adaptive priority sorting and health scoring
    - Provider stats JSON load/save/update/summary
    - Provider failures JSONL append/trim/recent read
    - Provider retry notice config parsing

    It does **not** depend on AstrBot event/send, only on ``self.config``
    (a reference to the plugin's config dict) and the plugin data directory
    name resolver.
    """

    def __init__(self, config: dict, plugin_name: str | Callable[[], str]) -> None:
        self.config = config
        self._plugin_name = plugin_name
        self._provider_stats_cache: dict | None = None
        self._provider_retry_notice_state: dict[str, dict[str, object]] = {}

    def plugin_name(self) -> str:
        """Return the effective plugin data directory name.

        Keep this dynamic to preserve the old ``getattr(self, "name",
        self.plugin_name)`` behavior from ``main.py``.
        """
        if callable(self._plugin_name):
            return str(self._plugin_name())
        return str(self._plugin_name)

    # ── Path helpers ─────────────────────────────────────────

    def provider_stats_path(self) -> Path:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / self.plugin_name()
            / "provider_stats.json"
        )

    def provider_failures_jsonl_path(self) -> Path:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / self.plugin_name()
            / "provider_failures.jsonl"
        )

    # ── Config access helpers ────────────────────────────────

    def adaptive_provider_priority_enabled(self) -> bool:
        value = self.config.get("adaptive_provider_priority", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    def provider_failure_cooldown(self) -> int:
        try:
            return max(0, int(self.config.get("provider_failure_cooldown", 300)))
        except (TypeError, ValueError):
            return 300

    def provider_retry_notice_global_enabled(self) -> bool:
        return normalize_bool(
            self.config.get("provider_retry_notice_enabled"),
            default=True,
        )

    def provider_retry_notice_session_config(self) -> dict[str, bool]:
        value = self.config.get("provider_retry_notice_sessions", {})
        if isinstance(value, dict):
            items = value.items()
        elif isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = {}
            items = parsed.items() if isinstance(parsed, dict) else []
        else:
            items = []

        result: dict[str, bool] = {}
        for key, raw in items:
            session_key = str(key or "").strip()
            if not session_key:
                continue
            result[session_key] = normalize_bool(raw, default=True)
        return result

    def set_provider_retry_notice_session_enabled(
        self,
        session_key: str,
        enabled: bool,
    ) -> None:
        sessions = self.provider_retry_notice_session_config()
        sessions[session_key] = enabled
        self.config["provider_retry_notice_sessions"] = json.dumps(
            sessions,
            ensure_ascii=False,
            sort_keys=True,
        )

    def provider_retry_notice_interval(self) -> int:
        try:
            return max(0, int(self.config.get("provider_retry_notice_interval", 300)))
        except (TypeError, ValueError):
            return 300

    def prompt_rewrite_guard_enabled(self, api_mode: str) -> bool:
        key = prompt_rewrite_guard_config_key(api_mode)
        return normalize_bool(
            self.config.get(key),
            default=prompt_rewrite_guard_default(api_mode),
        )

    # ── Provider config building ─────────────────────────────

    def get_fallback_api_provider_items(self) -> list:
        value = self.config.get("fallback_api_providers", [])
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as e:
                logger.warning(
                    f"[GPTImage2] fallback_api_providers JSON parse failed error={e}"
                )
                return []
            return parsed if isinstance(parsed, list) else []
        logger.warning(
            "[GPTImage2] fallback_api_providers ignored invalid type "
            f"type={type(value).__name__}"
        )
        return []

    def resolve_fallback_capabilities(self, data: dict) -> str:
        """Resolve capabilities from explicit field or legacy api_mode.

        Returns ``'images'``, ``'responses'``, or ``'all'``.
        """
        raw = str(data.get("capabilities") or "").strip().lower()
        if raw in {"images", "responses"}:
            return raw
        if raw in {"all", "both"}:
            return "all"
        if not raw:
            api_mode = str(data.get("api_mode") or "").strip().lower()
            if api_mode == "images":
                logger.info(
                    "[GPTImage2] auto-inferred capabilities=images from legacy "
                    f"api_mode for provider {data.get('name', '-')}; "
                    "consider using capabilities=images"
                )
                return "images"
            if api_mode == "responses":
                logger.info(
                    "[GPTImage2] auto-inferred capabilities=responses from legacy "
                    f"api_mode for provider {data.get('name', '-')}; "
                    "consider using capabilities=responses"
                )
                return "responses"
        return "all"

    def parse_fallback_api_provider(
        self,
        item: object,
        *,
        index: int,
        default_api_key: str,
        default_base_url: str,
        default_model: str,
        default_responses_model: str,
    ) -> ImageAPIProviderConfig | None:
        """Parse one fallback provider from WebUI list string or legacy dict."""
        if isinstance(item, dict):
            data = dict(item)
        elif isinstance(item, str):
            data = parse_fallback_api_provider_string(item)
        else:
            logger.warning(
                "[GPTImage2] skip invalid fallback API provider "
                f"index={index} type={type(item).__name__}"
            )
            return None

        if not data:
            return None

        explicit_name = str(data.get("name") or "").strip()
        provider_name = explicit_name or f"fallback-{index}"
        provider_base_url = str(data.get("base_url") or default_base_url).strip()
        provider_api_key = str(data.get("api_key") or default_api_key).strip()
        provider_role = normalize_provider_role(data.get("role") or "normal")
        provider_adaptive = normalize_bool(
            data.get("adaptive"), default=provider_role != "authoritative_fallback"
        )

        # Capabilities resolution
        capabilities = self.resolve_fallback_capabilities(data)
        if capabilities == "images":
            provider_model = str(data.get("model") or default_model).strip()
            provider_responses_model = ""
        elif capabilities == "responses":
            provider_model = ""
            provider_responses_model = str(
                data.get("responses_model") or default_responses_model
            ).strip()
        else:  # "all"
            provider_model = str(data.get("model") or default_model).strip()
            provider_responses_model = str(
                data.get("responses_model") or default_responses_model
            ).strip()

        if not provider_base_url:
            logger.warning(
                "[GPTImage2] skip fallback API provider without base_url "
                f"index={index} name={provider_name or '-'}"
            )
            return None
        if not provider_api_key:
            logger.warning(
                "[GPTImage2] skip fallback API provider without api_key "
                f"index={index} name={provider_name or '-'}"
            )
            return None
        if not provider_model and not provider_responses_model:
            logger.warning(
                "[GPTImage2] skip fallback API provider with no supported "
                f"capabilities index={index} name={provider_name or '-'}"
            )
            return None

        return ImageAPIProviderConfig(
            name=provider_name or f"fallback-{index}",
            api_key=provider_api_key,
            base_url=provider_base_url or "https://api.openai.com/v1",
            model=provider_model,
            responses_model=provider_responses_model,
            provider_id=build_provider_id(
                explicit_name,
                provider_base_url,
                provider_model,
                provider_responses_model,
            ),
            configured_order=index,
            role=provider_role,
            adaptive=provider_adaptive,
        )

    def get_image_api_provider_configs(self) -> list[ImageAPIProviderConfig]:
        """Build ordered draw/edit provider list: primary + normal backups + authoritative."""
        configs: list[ImageAPIProviderConfig] = []
        base_url = str(self.config.get("base_url", "https://api.openai.com/v1") or "")
        api_key = str(self.config.get("api_key", "") or "")
        model = str(self.config.get("model", "gpt-image-2") or "gpt-image-2")
        responses_model = str(
            self.config.get("responses_model", "gpt-5.5") or "gpt-5.5"
        )
        primary_name = (
            str(self.config.get("primary_provider_name", "") or "").strip() or "primary"
        )

        if api_key.strip():
            configs.append(
                ImageAPIProviderConfig(
                    name=primary_name,
                    api_key=api_key.strip(),
                    base_url=base_url.strip() or "https://api.openai.com/v1",
                    model=model.strip() or "gpt-image-2",
                    responses_model=responses_model.strip() or "gpt-5.5",
                    provider_id="name:primary",
                    configured_order=0,
                    role="primary",
                    adaptive=False,
                )
            )

        # Build authoritative fallback from dedicated config section
        auth_enabled = normalize_bool(
            self.config.get("authoritative_fallback_enabled"), default=False
        )
        auth_provider: ImageAPIProviderConfig | None = None
        if auth_enabled:
            auth_name = (
                str(self.config.get("authoritative_fallback_name", "") or "").strip()
                or "authoritative-fallback"
            )
            auth_base_url = (
                str(
                    self.config.get("authoritative_fallback_base_url", "") or ""
                ).strip()
                or base_url.strip()
            )
            auth_api_key = (
                str(self.config.get("authoritative_fallback_api_key", "") or "").strip()
                or api_key.strip()
            )
            auth_model = str(
                self.config.get("authoritative_fallback_images_model", "") or ""
            ).strip()
            auth_responses_model = str(
                self.config.get("authoritative_fallback_responses_model", "") or ""
            ).strip()

            if auth_api_key and auth_base_url:
                if not auth_model and not auth_responses_model:
                    logger.warning(
                        "[GPTImage2] authoritative_fallback enabled but both "
                        "images model and responses model are empty; skipped"
                    )
                else:
                    auth_provider = ImageAPIProviderConfig(
                        name=auth_name,
                        api_key=auth_api_key,
                        base_url=auth_base_url,
                        model=auth_model,
                        responses_model=auth_responses_model,
                        provider_id=build_provider_id(
                            auth_name,
                            auth_base_url,
                            auth_model,
                            auth_responses_model,
                        ),
                        configured_order=9999,
                        role="authoritative_fallback",
                        adaptive=False,
                    )
            else:
                logger.warning(
                    "[GPTImage2] authoritative_fallback_enabled but missing "
                    "api_key or base_url, skipped"
                )

        # Parse fallback_api_providers
        for index, item in enumerate(self.get_fallback_api_provider_items(), start=1):
            provider = self.parse_fallback_api_provider(
                item,
                index=index,
                default_api_key=api_key,
                default_base_url=base_url,
                default_model=model,
                default_responses_model=responses_model,
            )
            if provider is not None:
                # Check for legacy authoritative_fallback in the list
                if provider.role == "authoritative_fallback":
                    if auth_provider is not None:
                        logger.warning(
                            "[GPTImage2] ignoring authoritative_fallback from "
                            f"fallback_api_providers name={provider.name} "
                            "because dedicated authoritative_fallback config is enabled"
                        )
                        continue
                    auth_provider = provider
                    continue
                configs.append(provider)

        if auth_provider is not None:
            configs.append(auth_provider)

        if not configs:
            raise ValueError(
                "\u672a\u914d\u7f6e\u4efb\u4f55\u53ef\u7528\u7684\u751f\u56fe API Key\u3002\u8bf7\u914d\u7f6e api_key\uff0c"
                "\u6216\u5728 fallback_api_providers \u4e2d\u914d\u7f6e api_key\u3002"
            )
        return self.rank_image_api_provider_configs(configs)

    # ── Adaptive sorting ────────────────────────────────────

    def adaptive_sort_normal_providers(
        self,
        normal: list[ImageAPIProviderConfig],
    ) -> list[ImageAPIProviderConfig]:
        """Sort normal backups by health score (adaptive) then configured_order."""
        if len(normal) <= 1:
            return normal

        adaptive_enabled = self.adaptive_provider_priority_enabled()
        stats = self.load_provider_stats().get("providers", {})
        now = time()

        ranked: list[tuple[bool, float, int, ImageAPIProviderConfig]] = []
        for provider in normal:
            item = stats.get(provider.provider_id, {})
            item = item if isinstance(item, dict) else {}
            cooldown_until = provider_stat_float(item, "cooldown_until")
            adaptive_for_provider = adaptive_enabled and provider.adaptive
            cooldown_active = adaptive_for_provider and cooldown_until > now
            score = provider_health_score(item, now) if adaptive_for_provider else 0.0
            ranked.append(
                (
                    cooldown_active,
                    -score,
                    provider.configured_order,
                    provider,
                )
            )

        ranked.sort(key=lambda x: (x[0], x[1], x[2]))
        return [item[3] for item in ranked]

    def rank_image_api_provider_configs(
        self,
        configs: list[ImageAPIProviderConfig],
    ) -> list[ImageAPIProviderConfig]:
        """Three-segment sort: primary first, normal sorted, authoritative last."""
        if len(configs) <= 1:
            return configs

        primary = [c for c in configs if c.role == "primary"]
        authoritative = [c for c in configs if c.role == "authoritative_fallback"]
        normal = [c for c in configs if c.role == "normal"]

        normal = self.adaptive_sort_normal_providers(normal)

        result = primary + normal + authoritative
        if [p.provider_id for p in result] != [p.provider_id for p in configs]:
            logger.info(
                "[GPTImage2] provider priority reordered "
                f"order={[p.name for p in result]}"
            )
        return result

    # ── Stats load/save ─────────────────────────────────────

    def load_provider_stats(self) -> dict:
        if self._provider_stats_cache is not None:
            return self._provider_stats_cache

        path = self.provider_stats_path()
        if not path.exists():
            self._provider_stats_cache = {"version": 1, "providers": {}}
            return self._provider_stats_cache

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(
                "[GPTImage2] provider stats load failed "
                f"path={path} error={type(e).__name__}: {e}"
            )
            data = {"version": 1, "providers": {}}

        if not isinstance(data, dict):
            data = {"version": 1, "providers": {}}
        if not isinstance(data.get("providers"), dict):
            data["providers"] = {}
        data.setdefault("version", 1)
        self._provider_stats_cache = data
        return data

    def save_provider_stats(self) -> None:
        if self._provider_stats_cache is None:
            return
        path = self.provider_stats_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._provider_stats_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(
                "[GPTImage2] provider stats save failed "
                f"path={path} error={type(e).__name__}: {e}"
            )

    def update_provider_stats_summary(self) -> None:
        """Recalculate top-level aggregate summary from per-provider stats."""
        stats = self.load_provider_stats()
        providers = stats.get("providers", {})
        if not isinstance(providers, dict):
            return

        total_success = 0
        total_failure = 0
        all_reasons: dict[str, int] = {}
        all_codes: dict[str, int] = {}

        for item in providers.values():
            if not isinstance(item, dict):
                continue
            total_success += provider_stat_int(item, "success_count")
            total_failure += provider_stat_int(item, "failure_count")

            reasons = item.get("failure_reasons", {})
            if isinstance(reasons, dict):
                for key, count in reasons.items():
                    all_reasons[key] = all_reasons.get(key, 0) + count

            codes = item.get("failure_status_codes", {})
            if isinstance(codes, dict):
                for code, count in codes.items():
                    all_codes[code] = all_codes.get(code, 0) + count

        total = total_success + total_failure
        success_rate = round(total_success / total, 4) if total > 0 else 0.0

        sorted_reasons = dict(sorted(all_reasons.items(), key=lambda x: -x[1]))
        sorted_codes = dict(sorted(all_codes.items(), key=lambda x: -x[1]))

        stats["summary"] = {
            "success_count": total_success,
            "failure_count": total_failure,
            "success_rate": success_rate,
            "failure_reasons": sorted_reasons,
            "failure_status_codes": sorted_codes,
            "updated_at": time(),
        }
        stats["version"] = 2

    # ── Record results ──────────────────────────────────────

    def record_image_provider_result(
        self,
        provider: ImageAPIProviderConfig,
        *,
        success: bool,
        error_msg: str = "",
    ) -> None:
        if not self.adaptive_provider_priority_enabled():
            return

        stats = self.load_provider_stats()
        providers = stats.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            stats["providers"] = providers

        item = providers.get(provider.provider_id)
        if not isinstance(item, dict):
            item = {}
            providers[provider.provider_id] = item

        now = time()
        item.update(
            {
                "name": provider.name,
                "base_url": provider.base_url,
                "api_mode": self.config.get("api_mode", "images"),
                "model": provider.model,
                "responses_model": provider.responses_model,
                "images_model": provider.model,
                "configured_order": provider.configured_order,
                "role": provider.role,
                "adaptive": provider.adaptive,
                "updated_at": now,
            }
        )

        if success:
            item["success_count"] = provider_stat_int(item, "success_count") + 1
            item["consecutive_failures"] = 0
            item["last_success_at"] = now
            item["cooldown_until"] = 0
            item.pop("last_error", None)
        else:
            item["failure_count"] = provider_stat_int(item, "failure_count") + 1
            item["consecutive_failures"] = (
                provider_stat_int(item, "consecutive_failures") + 1
            )
            item["last_failure_at"] = now
            item["cooldown_until"] = now + self.provider_failure_cooldown()
            item["last_error"] = safe_markdown_preview(error_msg, limit=240)

            # v2: failure reason classification
            reason_key = classify_failure_reason(error_msg)
            reasons = item.setdefault("failure_reasons", {})
            if not isinstance(reasons, dict):
                reasons = {}
                item["failure_reasons"] = reasons
            reasons[reason_key] = reasons.get(reason_key, 0) + 1

            # v2: failure status code counts
            status_code = classify_http_status_code(error_msg)
            if status_code is not None:
                code_str = str(status_code)
                codes = item.setdefault("failure_status_codes", {})
                if not isinstance(codes, dict):
                    codes = {}
                    item["failure_status_codes"] = codes
                codes[code_str] = codes.get(code_str, 0) + 1

        # Update aggregate summary
        self.update_provider_stats_summary()
        self.save_provider_stats()

    # ── Failures JSONL ──────────────────────────────────────

    def append_provider_failure_record(
        self,
        provider: ImageAPIProviderConfig,
        *,
        error_msg: str,
        action: str,
        attempt_index: int,
        attempt_total: int,
        elapsed_ms: int | None = None,
        error: "BaseException | None" = None,
    ) -> None:
        """Append a sanitized failure record to ``provider_failures.jsonl``."""
        from urllib.parse import urlparse

        reason_key = classify_failure_reason(error_msg)

        try:
            parsed = urlparse(provider.base_url)
            url_host: str = parsed.hostname or "-"
            url_path: str = parsed.path or "-"
        except Exception:
            url_host = "-"
            url_path = "-"

        record: dict[str, object] = {
            "timestamp": time(),
            "provider_id": provider.provider_id,
            "provider_name": provider.name,
            "base_url_host": url_host,
            "base_url_path": url_path,
            "role": provider.role,
            "action": action,
            "attempt_index": attempt_index,
            "attempt_total": attempt_total,
            "reason_key": reason_key,
            "message_preview": safe_text_preview(error_msg, limit=240),
            "retryable": should_try_next_image_provider(error_msg),
        }

        # Extract structured diagnostics from ImageAPIError if available
        if error is not None and type(error).__name__ == "ImageAPIError":
            # Avoid importing ImageAPIError at module top to prevent cycles
            from .client import ImageAPIError as _IAE

            if isinstance(error, _IAE) and error.diagnostics is not None:
                diag = error.diagnostics
                record["error_class"] = "ImageAPIError"
                record["status_code"] = diag.status_code
                record["response_content_type"] = diag.response_content_type
                record["request_ids"] = diag.request_ids
                record["response_preview"] = diag.response_preview
                record["response_preview_truncated"] = diag.response_preview_truncated
                record["response_bytes"] = diag.response_bytes
                record["elapsed_ms"] = diag.elapsed_ms
                if diag.response_json_summary:
                    record["response_json_summary"] = diag.response_json_summary
            else:
                record["error_class"] = "RuntimeError"
                parsed_code = classify_http_status_code(error_msg)
                if parsed_code is not None:
                    record["status_code"] = parsed_code
                if elapsed_ms is not None:
                    record["elapsed_ms"] = elapsed_ms
        else:
            record["error_class"] = "RuntimeError"
            parsed_code = classify_http_status_code(error_msg)
            if parsed_code is not None:
                record["status_code"] = parsed_code
            if elapsed_ms is not None:
                record["elapsed_ms"] = elapsed_ms

        path = self.provider_failures_jsonl_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            # Trim to last ~5000 lines to avoid unbounded growth
            trim_jsonl(path, max_lines=5000)
        except Exception as e:
            logger.warning(
                "[GPTImage2] failed to append provider failure record "
                f"error={type(e).__name__}: {e}"
            )

    def read_recent_failure_records_inst(self, count: int) -> list[dict]:
        """Instance wrapper reading from the plugin's JSONL path."""
        return read_recent_failure_records(
            count, path=self.provider_failures_jsonl_path()
        )
