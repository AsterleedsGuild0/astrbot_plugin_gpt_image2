"""Tests for config_redact — standalone, no AstrBot runtime required."""

from __future__ import annotations

import json
import unittest

from image2_core.diagnostics.redact import redact_config_value


class TestRedactConfigValue(unittest.TestCase):
    """Recursive config redaction."""

    # ── Dict-level redaction ──────────────────────────────────

    def test_top_level_sensitive_key(self):
        """Top-level api_key value is redacted."""
        config = {"api_key": "sk-abc123", "base_url": "https://api.example.com/v1"}
        result = redact_config_value(config)
        assert isinstance(result, dict)
        assert result["api_key"] == "***REDACTED***"
        assert result["base_url"] == "https://api.example.com/v1"

    def test_top_level_plan_api_key(self):
        """plan_api_key is redacted."""
        config = {
            "plan_api_key": "sk-plan-test",
            "plan_base_url": "https://plan.example.com",
        }
        result = redact_config_value(config)
        assert isinstance(result, dict)
        assert result["plan_api_key"] == "***REDACTED***"

    def test_top_level_authoritative_fallback_api_key(self):
        """authoritative_fallback_api_key is redacted."""
        config = {"authoritative_fallback_api_key": "sk-auth-test"}
        result = redact_config_value(config)
        assert isinstance(result, dict)
        assert result["authoritative_fallback_api_key"] == "***REDACTED***"

    def test_token_and_secret_redacted(self):
        """Keys containing 'token' or 'secret' are redacted."""
        config = {"access_token": "t-abc", "secret_key": "s-xyz"}
        result = redact_config_value(config)
        assert result["access_token"] == "***REDACTED***"
        assert result["secret_key"] == "***REDACTED***"

    def test_password_redacted(self):
        """Keys containing 'password' are redacted."""
        config = {"password": "hunter2"}
        result = redact_config_value(config)
        assert result["password"] == "***REDACTED***"

    def test_harmless_fields_preserved(self):
        """model, base_url, name, size, quality are preserved."""
        config = {
            "model": "gpt-image-2",
            "base_url": "https://api.openai.com/v1",
            "name": "my-provider",
            "size": "1024x1024",
            "quality": "auto",
        }
        result = redact_config_value(config)
        assert result["model"] == "gpt-image-2"
        assert result["base_url"] == "https://api.openai.com/v1"
        assert result["name"] == "my-provider"
        assert result["size"] == "1024x1024"
        assert result["quality"] == "auto"

    # ── Nested dict ───────────────────────────────────────────

    def test_nested_dict_sensitive_key(self):
        """Sensitive keys in nested dicts are redacted."""
        config = {
            "fallback_api_providers": {
                "name": "backup-1",
                "api_key": "sk-nested",
                "base_url": "https://backup.example.com/v1",
            }
        }
        result = redact_config_value(config)
        nested = result["fallback_api_providers"]
        assert isinstance(nested, dict)
        assert nested["api_key"] == "***REDACTED***"
        assert nested["name"] == "backup-1"
        assert nested["base_url"] == "https://backup.example.com/v1"

    # ── List of dicts ─────────────────────────────────────────

    def test_list_of_dicts_redacted(self):
        """Each dict in a list has its sensitive keys redacted."""
        config = {
            "fallback_api_providers": [
                {"name": "a", "api_key": "sk-a", "base_url": "https://a.com"},
                {"name": "b", "api_key": "sk-b", "base_url": "https://b.com"},
            ]
        }
        result = redact_config_value(config)
        items = result["fallback_api_providers"]
        assert isinstance(items, list)
        assert len(items) == 2
        for item in items:
            assert isinstance(item, dict)
            assert item["api_key"] == "***REDACTED***"
            assert item["name"] in ("a", "b")

    # ── Fallback provider strings ─────────────────────────────

    def test_fallback_string_redacted(self):
        """api_key= in comma-separated fallback string is redacted."""
        value = "name=backup-1, base_url=https://example.com/v1, api_key=sk-abc, model=gpt-image-2"
        result = redact_config_value(value)
        assert isinstance(result, str)
        assert "***REDACTED***" in result
        assert "sk-abc" not in result
        assert "name=backup-1" in result or "name = backup-1" in result
        assert "model=gpt-image-2" in result or "model = gpt-image-2" in result
        assert (
            "base_url=https://example.com/v1" in result
            or "base_url = https://example.com/v1" in result
        )

    def test_fallback_string_key_alias_redacted(self):
        """Bare 'key' alias in fallback string is redacted."""
        value = "name=x, key=sk-xyz, model=m1"
        result = redact_config_value(value)
        assert isinstance(result, str)
        assert "***REDACTED***" in result
        assert "sk-xyz" not in result

    def test_fallback_string_semicolon_redacted(self):
        """Semicolon-delimited fallback string is redacted."""
        value = "name=x; api_key=sk-abc; model=m1"
        result = redact_config_value(value)
        assert isinstance(result, str)
        assert "***REDACTED***" in result
        assert "sk-abc" not in result

    def test_simple_url_not_redacted(self):
        """A bare URL not in key=value format should not be redacted."""
        value = "https://api.example.com/v1"
        result = redact_config_value(value)
        assert result == "https://api.example.com/v1"

    # ── JSON-encoded strings ──────────────────────────────────

    def test_json_string_redacted(self):
        """JSON-string dictionary is parsed, redacted, and re-serialized."""
        value = json.dumps({"api_key": "sk-json", "model": "gpt-image-2"})
        result = redact_config_value(value)
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed["api_key"] == "***REDACTED***"
        assert parsed["model"] == "gpt-image-2"

    def test_json_list_redacted(self):
        """JSON-string list of dicts is redacted."""
        value = json.dumps(
            [
                {"api_key": "sk-a", "name": "a"},
                {"api_key": "sk-b", "name": "b"},
            ]
        )
        result = redact_config_value(value)
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert len(parsed) == 2
        for item in parsed:
            assert item["api_key"] == "***REDACTED***"

    # ── URL credentials ───────────────────────────────────────

    def test_url_with_credentials_redacted(self):
        """Userinfo in URL is stripped."""
        config = {"webhook": "https://user:pass@hook.example.com/path"}
        result = redact_config_value(config)
        assert "user:pass" not in str(result)
        assert "hook.example.com/path" in str(result)

    def test_url_with_query_redacted(self):
        """URL query parameters are stripped."""
        config = {"callback": "https://api.example.com/v1?secret_token=abc"}
        result = redact_config_value(config)
        assert "secret_token=abc" not in str(result)

    # ── Edge cases ────────────────────────────────────────────

    def test_empty_dict(self):
        assert redact_config_value({}) == {}

    def test_empty_list(self):
        assert redact_config_value([]) == []

    def test_none(self):
        assert redact_config_value(None) is None

    def test_int(self):
        assert redact_config_value(42) == 42

    def test_float(self):
        assert redact_config_value(3.14) == 3.14

    def test_bool(self):
        assert redact_config_value(True) is True
        assert redact_config_value(False) is False

    def test_max_depth(self):
        """Excessive nesting returns max-depth marker."""
        deep: dict = {}
        current = deep
        for _ in range(25):
            current["x"] = {}
            current = current["x"]
        result = redact_config_value(deep)
        assert isinstance(result, dict)
        # At max depth, the innermost value will be "(max-depth)" nested inside dict
        # Just ensure no crash and result is a dict
        assert "x" in result

    def test_bare_api_key_redacted(self):
        """Bare sk-xxx string longer than 20 chars is redacted (heuristic)."""
        value = "sk-" + "a" * 30
        result = redact_config_value(value)
        assert result == "***REDACTED***"

    def test_short_string_not_redacted(self):
        """Short sk- string is not redacted (under 20 chars)."""
        value = "sk-abc"  # only 7 chars
        result = redact_config_value(value)
        assert result == value  # unchanged — under heuristic threshold

    def test_fallback_string_token_redacted(self):
        """token= in fallback string is redacted."""
        value = "name=x, token=sk-token-val, model=m1"
        result = redact_config_value(value)
        assert "***REDACTED***" in result
        assert "sk-token-val" not in result

    def test_fallback_string_password_redacted(self):
        """password= in fallback string is redacted."""
        value = "name=x, password=secret123"
        result = redact_config_value(value)
        assert "***REDACTED***" in result
        assert "secret123" not in result

    def test_fallback_string_url_credentials_redacted(self):
        """Credentials embedded in fallback base_url are stripped."""
        value = "name=x, base_url=https://user:pass@example.com/v1, model=m1"
        result = redact_config_value(value)
        assert isinstance(result, str)
        assert "user:pass" not in result
        assert "base_url=https://example.com/v1" in result

    def test_fallback_string_url_query_redacted(self):
        """Query params embedded in fallback base_url are stripped."""
        value = (
            "name=x, base_url=https://example.com/v1?api_key=sk-query-secret, model=m1"
        )
        result = redact_config_value(value)
        assert isinstance(result, str)
        assert "sk-query-secret" not in result
        assert "?api_key=" not in result
        assert "base_url=https://example.com/v1" in result

    # ── Complex realistic config ──────────────────────────────

    def test_realistic_config(self):
        """Realistic plugin config with fallback providers list."""
        config = {
            "api_key": "sk-main",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-image-2",
            "responses_model": "gpt-5.5",
            "fallback_api_providers": [
                {
                    "name": "backup-1",
                    "api_key": "sk-backup-1",
                    "base_url": "https://backup1.example.com/v1",
                    "model": "gpt-image-2",
                    "capabilities": "all",
                },
                "name=backup-2, base_url=https://backup2.example.com/v1, api_key=sk-backup-2, capabilities=images",
            ],
            "authoritative_fallback_enabled": True,
            "authoritative_fallback_api_key": "sk-auth-fallback",
            "authoritative_fallback_base_url": "https://auth.example.com/v1",
            "plan_api_key": "sk-plan",
            "plan_base_url": "https://plan.example.com/v1",
            "size": "1024x1024",
            "quality": "auto",
            "n": 1,
            "timeout": 120,
        }
        result = redact_config_value(config)
        assert isinstance(result, dict)

        # Top-level sensitive keys redacted
        assert result["api_key"] == "***REDACTED***"
        assert result["plan_api_key"] == "***REDACTED***"
        assert result["authoritative_fallback_api_key"] == "***REDACTED***"

        # Harmless fields preserved
        assert result["base_url"] == "https://api.openai.com/v1"
        assert result["model"] == "gpt-image-2"
        assert result["size"] == "1024x1024"
        assert result["n"] == 1

        # Fallback list: dict item
        dict_item = result["fallback_api_providers"][0]
        assert isinstance(dict_item, dict)
        assert dict_item["api_key"] == "***REDACTED***"
        assert dict_item["name"] == "backup-1"
        assert dict_item["base_url"] == "https://backup1.example.com/v1"

        # Fallback list: string item
        str_item = result["fallback_api_providers"][1]
        assert isinstance(str_item, str)
        assert "***REDACTED***" in str_item
        assert "sk-backup-2" not in str_item


if __name__ == "__main__":
    unittest.main()
