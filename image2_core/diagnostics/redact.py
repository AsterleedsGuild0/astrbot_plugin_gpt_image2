"""Configuration redaction utilities for diagnostic output.

No AstrBot dependency — can be tested standalone.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

# Compile sensitive key patterns once.
_SENSITIVE_KEY_PATTERNS = re.compile(
    r"(api_key|apikey|token|secret|password)", re.IGNORECASE
)

# Additional keys that are sensitive only in fallback provider string context
# (the bare "key" alias maps to api_key).
_FALLBACK_STRING_SENSITIVE = frozenset(
    {
        "key",
        "api_key",
        "apikey",
        "plan_api_key",
        "token",
        "secret",
        "password",
    }
)


def _is_sensitive_key(key: str) -> bool:
    """Check if a config key matches sensitive patterns."""
    return bool(_SENSITIVE_KEY_PATTERNS.search(key))


def _redact_url(value: str) -> str:
    """Remove credentials/query from a URL string, preserving scheme/host/path."""
    try:
        parsed = urlparse(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            redacted = f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
            return redacted.rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    return value


def _redact_fallback_provider_string(value: str) -> str:
    """Redact api_key-like values in a comma/separator-delimited key=value string.

    e.g. ``"name=x, api_key=sk-abc, model=..."``
    -> ``"name=x, api_key=***REDACTED***, model=..."``

    Preserves all other fields without destroying formatting.
    """
    if "=" not in value:
        return value

    # Replace semicolons with commas for uniform handling
    normalized = value.replace(";", ",")
    parts = normalized.split(",")
    redacted_parts: list[str] = []

    for part in parts:
        part_stripped = part.strip()
        if "=" not in part_stripped:
            redacted_parts.append(part)
            continue

        eq_idx = part_stripped.find("=")
        key_raw = part_stripped[:eq_idx].strip()
        key_normalized = key_raw.lower().replace("-", "_")

        value_raw = part_stripped[eq_idx + 1 :].strip()

        if key_normalized in _FALLBACK_STRING_SENSITIVE or _is_sensitive_key(
            key_normalized
        ):
            # Preserve original key casing, just redact the value
            redacted_parts.append(f"{key_raw}=***REDACTED***")
        else:
            redacted_value = (
                _redact_url(value_raw)
                if value_raw.startswith(("http://", "https://"))
                else value_raw
            )
            if redacted_value != value_raw:
                redacted_parts.append(f"{key_raw}={redacted_value}")
            else:
                redacted_parts.append(part)

    return ", ".join(redacted_parts)


def _try_json_parse(value: str) -> Any | None:
    """Try to parse a string as JSON. Returns parsed value or None."""
    stripped = value.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def redact_config_value(value: object, depth: int = 0) -> object:
    """Recursively redact sensitive values from a config structure.

    Redacts values whose keys match ``api_key`` / ``token`` / ``secret`` /
    ``password`` patterns.  Handles nested dicts/lists, fallback provider
    strings, JSON-encoded strings, and URL credentials.

    * Never mutates the input.
    * Harmless fields such as ``model``, ``base_url``, ``name`` are preserved
      (except credentials/query are stripped from URLs).

    Args:
        value: The config value to redact (dict, list, str, or scalar).
        depth: Current recursion depth (internal use, starts at 0).

    Returns:
        Redacted copy of the value.
    """
    MAX_DEPTH = 20
    if depth > MAX_DEPTH:
        return "(max-depth)"

    if isinstance(value, dict):
        result: dict[str, object] = {}
        for k, v in value.items():
            k_str = str(k)
            if _is_sensitive_key(k_str):
                result[k_str] = "***REDACTED***"
            else:
                result[k_str] = redact_config_value(v, depth + 1)
        return result

    if isinstance(value, list):
        return [redact_config_value(item, depth + 1) for item in value]

    if isinstance(value, str):
        # --- JSON-encoded complex types ---
        parsed = _try_json_parse(value)
        if parsed is not None:
            redacted = redact_config_value(parsed, depth + 1)
            return json.dumps(redacted, ensure_ascii=False)

        # --- Fallback provider string (key=value pairs with separators) ---
        if "=" in value and ("," in value or ";" in value):
            chars = len(value)
            if 10 < chars < 5000:
                redacted = _redact_fallback_provider_string(value)
                if redacted != value:
                    return redacted

        # --- URL credentials ---
        if value.startswith(("http://", "https://")):
            return _redact_url(value)

        # --- Bare API key heuristic (sk-xxx / fk-xxx) ---
        stripped_val = value.strip()
        if stripped_val.startswith(("sk-", "fk-")) and 20 <= len(stripped_val) <= 300:
            return "***REDACTED***"

    return value
