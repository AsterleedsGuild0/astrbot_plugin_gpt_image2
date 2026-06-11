"""Provider 绑定计费配置与展示测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

# 测试环境没有 AstrBot 运行时，导入 provider/billing 前先 mock astrbot。
astrbot_api = MagicMock()
astrbot_api.logger = MagicMock()
sys.modules["astrbot"] = MagicMock()
sys.modules["astrbot.api"] = astrbot_api

from image2_core.billing.config import parse_billing_config  # noqa: E402
from image2_core.billing.messages import (  # noqa: E402
    build_balance_markdown,
    build_costs_summary_markdown,
    format_observation_balance_notice,
)
from image2_core.billing.records import BillingRecords  # noqa: E402
from image2_core.billing.tracker import BillingObservation, BillingTracker  # noqa: E402
from image2_core.providers.manager import (  # noqa: E402
    ProviderManager,
    migrate_fallback_api_providers_json_text,
)
from image2_core.providers.messages import build_providers_status_markdown  # noqa: E402


def fake_openai_key(suffix: str) -> str:
    """构造测试用 OpenAI-like key，避免源码中出现完整 key 字面量。"""
    return "sk" + "-" + suffix


class TestBillingConfig(unittest.TestCase):
    """验证 billing 配置解析和 Provider 绑定。"""

    def test_primary_billing_json_is_bound_to_primary_provider(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("test"),
                "base_url": "https://primary.example/v1",
                "primary_billing_json": (
                    '{"balance_url":"https://primary.example/balance",'
                    '"balance_json_path":"data.balance",'
                    '"currency":"CNY","scale":0.01,"balance_multiplier":7.2,'
                    '"success_cost":0.03,"failure_cost":0.001}'
                ),
            },
            "test-plugin",
        )

        provider = manager.get_image_api_provider_configs()[0]

        self.assertEqual(provider.role, "primary")
        self.assertIsNotNone(provider.billing)
        self.assertEqual(provider.billing.type, "balance")
        self.assertEqual(provider.billing.balance_json_path, "data.balance")
        self.assertEqual(provider.billing.scale, 0.01)
        self.assertEqual(provider.billing.balance_multiplier, 7.2)
        self.assertTrue(provider.billing.has_fixed_fallback)
        self.assertEqual(provider.billing.success_cost, 0.03)

    def test_primary_force_single_image_requests_is_bound_to_primary_provider(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("test"),
                "base_url": "https://primary.example/v1",
                "primary_force_single_image_requests": True,
            },
            "test-plugin",
        )

        provider = manager.get_image_api_provider_configs()[0]

        self.assertEqual(provider.role, "primary")
        self.assertTrue(provider.force_single_image_requests)

    def test_fallback_json_billing_is_bound_to_fallback_provider(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("main"),
                "base_url": "https://primary.example/v1",
                "fallback_api_providers": [
                    {
                        "name": "backup",
                        "base_url": "https://backup.example/v1",
                        "api_key": fake_openai_key("backup"),
                        "billing": {
                            "success_cost": 0.03,
                            "failure_cost": 0.001,
                            "currency": "USD",
                        },
                    }
                ],
            },
            "test-plugin",
        )

        providers = manager.get_image_api_provider_configs()
        fallback = next(p for p in providers if p.name == "backup")

        self.assertIsNotNone(fallback.billing)
        self.assertEqual(fallback.billing.type, "fixed")
        self.assertEqual(fallback.billing.success_cost, 0.03)
        self.assertEqual(fallback.billing.failure_cost, 0.001)

    def test_fallback_json_force_single_image_requests_is_bound_to_provider(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("main"),
                "base_url": "https://primary.example/v1",
                "fallback_api_providers": (
                    '[{"name":"backup","base_url":"https://backup.example/v1",'
                    f'"api_key":"{fake_openai_key("backup")}",'
                    '"force_single_image_requests":true}]'
                ),
            },
            "test-plugin",
        )

        providers = manager.get_image_api_provider_configs()
        fallback = next(p for p in providers if p.name == "backup")

        self.assertTrue(fallback.force_single_image_requests)

    def test_authoritative_fallback_force_single_image_requests_is_bound(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("main"),
                "base_url": "https://primary.example/v1",
                "authoritative_fallback_enabled": True,
                "authoritative_fallback_name": "auth",
                "authoritative_fallback_images_model": "gpt-image-2",
                "authoritative_fallback_force_single_image_requests": True,
            },
            "test-plugin",
        )

        providers = manager.get_image_api_provider_configs()
        auth = next(p for p in providers if p.role == "authoritative_fallback")

        self.assertTrue(auth.force_single_image_requests)

    def test_fallback_provider_json_text_is_supported(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("main"),
                "base_url": "https://primary.example/v1",
                "fallback_api_providers": (
                    '[{"name":"backup","base_url":"https://backup.example/v1",'
                    f'"api_key":"{fake_openai_key("backup")}",'
                    '"capabilities":"images"}]'
                ),
            },
            "test-plugin",
        )

        providers = manager.get_image_api_provider_configs()
        fallback = next(p for p in providers if p.name == "backup")

        self.assertEqual(fallback.base_url, "https://backup.example/v1")
        self.assertEqual(fallback.api_key, fake_openai_key("backup"))
        self.assertTrue(fallback.images_supported)
        self.assertFalse(fallback.responses_supported)

    def test_fallback_provider_string_items_are_ignored(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("main"),
                "base_url": "https://primary.example/v1",
                "fallback_api_providers": '["https://backup.example/v1"]',
            },
            "test-plugin",
        )

        providers = manager.get_image_api_provider_configs()

        self.assertEqual([p.role for p in providers], ["primary"])

    def test_migrates_legacy_fallback_list_to_json_text(self):
        config = {
            "fallback_api_providers": [
                "name=backup-1, base_url=https://backup.example/v1, "
                f"api_key={fake_openai_key('old')}, capabilities=images",
                {
                    "name": "backup-2",
                    "base_url": "https://backup2.example/v1",
                },
            ]
        }

        changed = migrate_fallback_api_providers_json_text(config)

        self.assertTrue(changed)
        value = config["fallback_api_providers"]
        self.assertIsInstance(value, str)
        parsed = json.loads(value)
        self.assertEqual(parsed[0]["name"], "backup-1")
        self.assertEqual(parsed[0]["base_url"], "https://backup.example/v1")
        self.assertEqual(parsed[0]["api_key"], fake_openai_key("old"))
        self.assertEqual(parsed[1]["name"], "backup-2")

    def test_migrates_legacy_fallback_dict_to_json_text(self):
        config = {
            "fallback_api_providers": {
                "name": "backup",
                "base_url": "https://backup.example/v1",
            }
        }

        changed = migrate_fallback_api_providers_json_text(config)

        self.assertTrue(changed)
        parsed = json.loads(config["fallback_api_providers"])
        self.assertEqual(
            parsed, [{"name": "backup", "base_url": "https://backup.example/v1"}]
        )

    def test_keeps_valid_fallback_json_text_unchanged(self):
        original = '[{"name":"backup","base_url":"https://backup.example/v1"}]'
        config = {"fallback_api_providers": original}

        changed = migrate_fallback_api_providers_json_text(config)

        self.assertFalse(changed)
        self.assertEqual(config["fallback_api_providers"], original)

    def test_parse_billing_config_ignores_invalid_type(self):
        self.assertIsNone(parse_billing_config({"type": "unknown"}))

    def test_parse_billing_config_infers_fixed_without_type(self):
        billing = parse_billing_config(
            {"success_cost": 0.03, "failure_cost": 0, "currency": "USD"}
        )

        self.assertIsNotNone(billing)
        self.assertEqual(billing.type, "fixed")
        self.assertTrue(billing.has_fixed_fallback)

    def test_parse_billing_config_defaults_to_cny(self):
        billing = parse_billing_config({"success_cost": 0.2})

        self.assertIsNotNone(billing)
        self.assertEqual(billing.currency, "CNY")
        self.assertEqual(billing.balance_multiplier, 1.0)

    def test_balance_config_supports_nested_fixed_fallback(self):
        billing = parse_billing_config(
            {
                "balance_url": "https://example.com/balance",
                "fixed_fallback": {"success_cost": 0.02, "failure_cost": 0.001},
                "currency": "USD",
            }
        )

        self.assertIsNotNone(billing)
        self.assertEqual(billing.type, "balance")
        self.assertTrue(billing.has_fixed_fallback)
        self.assertEqual(billing.success_cost, 0.02)
        self.assertEqual(billing.failure_cost, 0.001)

    def test_total_usage_config_uses_total_url(self):
        billing = parse_billing_config(
            {
                "total_url": "https://example.com/dashboard/billing/subscription",
                "total_json_path": "soft_limit_usd",
                "usage_url": "https://example.com/dashboard/billing/usage",
                "usage_json_path": "total_usage",
                "usage_scale": 0.01,
                "currency": "CNY",
            }
        )

        self.assertIsNotNone(billing)
        self.assertEqual(billing.type, "total_usage")
        self.assertEqual(
            billing.total_url, "https://example.com/dashboard/billing/subscription"
        )
        self.assertEqual(billing.total_json_path, "soft_limit_usd")
        self.assertEqual(
            billing.usage_url, "https://example.com/dashboard/billing/usage"
        )
        self.assertEqual(billing.usage_json_path, "total_usage")
        self.assertEqual(billing.usage_scale, 0.01)


class TestBillingTracker(unittest.TestCase):
    """验证余额差和路径提取。"""

    def test_extract_number_supports_dict_and_list_paths(self):
        self.assertEqual(
            BillingTracker._extract_number(
                {"data": {"items": [{"balance": "12.5"}]}}, "data.items.0.balance"
            ),
            12.5,
        )
        self.assertIsNone(BillingTracker._extract_number({"data": {}}, "data.missing"))

    def test_balance_observation_uses_fixed_fallback_when_delta_unknown(self):
        billing = parse_billing_config(
            {
                "type": "balance",
                "success_cost": 0.03,
                "failure_cost": 0.001,
                "currency": "USD",
            }
        )
        self.assertIsNotNone(billing)
        provider = MagicMock()
        provider.provider_id = "pid"
        provider.name = "backup"
        tracker = BillingTracker(MagicMock())

        obs = tracker._balance_observation(
            provider,
            billing,
            success=True,
            raw_before=None,
            raw_after=None,
            cost_units=3,
        )

        self.assertEqual(obs.cost, 0.09)
        self.assertEqual(obs.cost_units, 3)
        self.assertEqual(obs.cost_source, "fixed_fallback")

    def test_fixed_success_cost_is_per_returned_image(self):
        billing = parse_billing_config(
            {"success_cost": 0.03, "failure_cost": 0.001, "currency": "USD"}
        )
        self.assertIsNotNone(billing)
        provider = MagicMock()
        provider.provider_id = "pid"
        provider.name = "backup"
        tracker = BillingTracker(MagicMock())

        obs = tracker._fixed_observation(
            provider,
            billing,
            success=True,
            cost_units=4,
        )

        self.assertEqual(obs.cost, 0.12)
        self.assertEqual(obs.cost_units, 4)

    def test_fixed_failure_cost_is_per_attempt(self):
        billing = parse_billing_config(
            {"success_cost": 0.03, "failure_cost": 0.001, "currency": "USD"}
        )
        self.assertIsNotNone(billing)
        provider = MagicMock()
        provider.provider_id = "pid"
        provider.name = "backup"
        tracker = BillingTracker(MagicMock())

        obs = tracker._fixed_observation(
            provider,
            billing,
            success=False,
            cost_units=4,
        )

        self.assertEqual(obs.cost, 0.001)
        self.assertEqual(obs.cost_units, 1)

    def test_observe_call_prefers_returned_image_count_for_fixed_success(self):
        billing = parse_billing_config(
            {"success_cost": 0.03, "failure_cost": 0.001, "currency": "USD"}
        )
        self.assertIsNotNone(billing)
        provider = MagicMock()
        provider.provider_id = "pid"
        provider.name = "backup"
        tracker = BillingTracker(MagicMock())

        async def fake_call():
            return ["img1", "img2"]

        _result, obs = asyncio.run(
            tracker.observe_call(
                provider,
                billing,
                action="draw",
                api_mode="images",
                cost_units=4,
                result_cost_units=len,
                call=fake_call,
            )
        )

        self.assertIsNotNone(obs)
        self.assertEqual(obs.cost, 0.06)
        self.assertEqual(obs.cost_units, 2)

    def test_fetch_balance_subtracts_usage_url_value_from_total_url(self):
        billing = parse_billing_config(
            {
                "total_url": "https://example.com/dashboard/billing/subscription",
                "total_json_path": "soft_limit_usd",
                "usage_url": "https://example.com/dashboard/billing/usage",
                "usage_json_path": "total_usage",
                "usage_scale": 0.01,
                "scale": 1,
            }
        )
        self.assertIsNotNone(billing)
        provider = MagicMock()
        tracker = BillingTracker(MagicMock())

        async def fake_fetch_number(*_args, **kwargs):
            return 150 if kwargs["label"] == "total" else 10000

        tracker._fetch_number = fake_fetch_number  # type: ignore[method-assign]

        raw_balance = asyncio.run(tracker._fetch_balance(provider, billing))

        self.assertEqual(raw_balance, 50)

    def test_balance_url_takes_precedence_over_total_url(self):
        billing = parse_billing_config(
            {
                "balance_url": "https://example.com/api/balance",
                "balance_json_path": "data.balance",
                "total_url": "https://example.com/dashboard/billing/subscription",
                "total_json_path": "soft_limit_usd",
                "usage_url": "https://example.com/dashboard/billing/usage",
            }
        )

        self.assertIsNotNone(billing)
        self.assertEqual(billing.type, "balance")
        self.assertEqual(billing.balance_url, "https://example.com/api/balance")


class TestBillingRecordsManualAnchor(unittest.TestCase):
    """验证手动余额锚点估算。"""

    def test_record_event_returns_item_with_balance_source(self):
        """record_event() now returns the updated provider item dict containing
        manual anchor estimate fields."""
        with tempfile.TemporaryDirectory() as tmp:
            records = BillingRecords("test-plugin")
            stats_path = Path(tmp) / "billing_stats.json"
            records.billing_stats_path = lambda: stats_path  # type: ignore[method-assign]
            records.billing_events_jsonl_path = lambda: (  # type: ignore[method-assign]
                Path(tmp) / "billing_events.jsonl"
            )

            records.set_balance_anchor(
                provider_id="pid2",
                provider_name="FixedProvider",
                base_url="https://fixed.example/v1",
                role="normal",
                amount=100.0,
                currency="CNY",
                balance_multiplier=1,
            )
            item = records.record_event(
                {
                    "provider_id": "pid2",
                    "provider_name": "FixedProvider",
                    "base_url": "https://fixed.example/v1",
                    "role": "normal",
                    "billing_type": "fixed",
                    "success": True,
                    "cost": 1.5,
                    "cost_units": 1,
                    "currency": "CNY",
                    "cost_source": "fixed",
                }
            )

            self.assertIsInstance(item, dict)
            self.assertEqual(item.get("balance_source"), "manual_anchor_estimate")
            self.assertAlmostEqual(item.get("last_balance_after", 0), 98.5)
            self.assertAlmostEqual(item.get("last_converted_balance", 0), 98.5)

    def test_manual_anchor_updates_estimated_balance_after_fixed_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = BillingRecords("test-plugin")
            stats_path = Path(tmp) / "billing_stats.json"
            records.billing_stats_path = lambda: stats_path  # type: ignore[method-assign]
            records.billing_events_jsonl_path = lambda: (
                Path(  # type: ignore[method-assign]
                    tmp
                )
                / "billing_events.jsonl"
            )

            records.set_balance_anchor(
                provider_id="pid",
                provider_name="LTCraftAI",
                base_url="https://ai.ltcraft.cn/v1",
                role="normal",
                amount=78.09,
                currency="CNY",
                balance_multiplier=1,
            )
            records.record_event(
                {
                    "provider_id": "pid",
                    "provider_name": "LTCraftAI",
                    "base_url": "https://ai.ltcraft.cn/v1",
                    "role": "normal",
                    "billing_type": "fixed",
                    "success": True,
                    "cost": 0.73,
                    "cost_units": 1,
                    "currency": "CNY",
                    "cost_source": "fixed",
                }
            )

            item = records.load_billing_stats()["providers"]["pid"]
            self.assertTrue(item["manual_balance_anchor"])
            self.assertEqual(item["balance_source"], "manual_anchor_estimate")
            self.assertAlmostEqual(item["last_balance_after"], 77.36)
            self.assertAlmostEqual(item["last_converted_balance"], 77.36)


class TestBillingMessages(unittest.TestCase):
    """验证费用与余额展示。"""

    def test_costs_summary_uses_currency_totals_and_cached_balance(self):
        markdown = build_costs_summary_markdown(
            {
                "summary": {
                    "totals_by_currency": {"USD": 0.06},
                    "cost_count": 2,
                    "unknown_count": 1,
                },
                "providers": {
                    "pid": {
                        "provider_name": "backup",
                        "billing_type": "balance",
                        "currency": "USD",
                        "total_cost": 0.06,
                        "event_count": 3,
                        "last_balance_after": 9.94,
                        "last_converted_balance": 0.994,
                    }
                },
            }
        )

        self.assertIn("0.06 USD", markdown)
        self.assertIn("约 0.994 USD", markdown)

    def test_balance_markdown_shows_station_and_converted_balance(self):
        markdown = build_balance_markdown(
            [
                BillingObservation(
                    provider_id="pid",
                    provider_name="backup",
                    billing_type="balance",
                    success=True,
                    currency="CNY",
                    raw_balance_after=1200,
                    balance_after=12,
                    converted_balance_after=86.4,
                )
            ]
        )

        self.assertIn("约 86.4 CNY", markdown)
        self.assertIn("余额数值 12", markdown)
        self.assertIn("原始值 1200 raw", markdown)

    def test_balance_markdown_marks_manual_anchor_estimate(self):
        markdown = build_balance_markdown(
            [
                BillingObservation(
                    provider_id="pid",
                    provider_name="LTCraftAI",
                    billing_type="manual_anchor",
                    success=True,
                    currency="CNY",
                    balance_after=78.09,
                    converted_balance_after=78.09,
                    cost_source="manual_anchor_estimate",
                )
            ]
        )

        self.assertIn("78.09 CNY", markdown)
        self.assertIn("手动锚点估算", markdown)

    def test_providers_status_includes_lightweight_billing(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("test"),
                "primary_billing_json": '{"type":"fixed","success_cost":0.03,"currency":"USD"}',
            },
            "test-plugin",
        )
        provider = manager.get_image_api_provider_configs()[0]

        markdown = build_providers_status_markdown(
            [provider],
            {},
            global_mode="images",
            now=0,
            billing_stats={},
        )

        self.assertIn("计费：fixed", markdown)
        self.assertIn("成功单张 0.03 USD", markdown)

    def test_providers_status_shows_balance_fixed_fallback(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("test"),
                "primary_billing_json": (
                    '{"balance_url":"https://example.com/balance",'
                    '"success_cost":0.03,"failure_cost":0.001,"currency":"USD"}'
                ),
            },
            "test-plugin",
        )
        provider = manager.get_image_api_provider_configs()[0]

        markdown = build_providers_status_markdown(
            [provider],
            {},
            global_mode="images",
            now=0,
            billing_stats={},
        )

        self.assertIn("计费：balance", markdown)
        self.assertIn("固定参考 成功单张 0.03 USD / 失败单次 0.001 USD", markdown)

    def test_balance_notice_format_auto_balance(self):
        notice = format_observation_balance_notice(
            BillingObservation(
                provider_id="pid",
                provider_name="test",
                billing_type="balance",
                success=True,
                currency="CNY",
                balance_after=12.1,
                converted_balance_after=12.1,
            )
        )
        self.assertEqual(notice, "，余额 12.1 CNY")

    def test_balance_notice_format_manual_anchor(self):
        notice = format_observation_balance_notice(
            BillingObservation(
                provider_id="pid",
                provider_name="test",
                billing_type="fixed",
                success=True,
                currency="CNY",
                balance_after=77.06,
                converted_balance_after=77.06,
                balance_source="manual_anchor_estimate",
            )
        )
        self.assertEqual(notice, "，余额约 77.06 CNY（手动估算）")

    def test_balance_notice_format_no_balance(self):
        notice = format_observation_balance_notice(
            BillingObservation(
                provider_id="pid",
                provider_name="test",
                billing_type="fixed",
                success=True,
            )
        )
        self.assertEqual(notice, "")

    def test_balance_notice_format_none(self):
        self.assertEqual(format_observation_balance_notice(None), "")

    def test_providers_status_marks_manual_anchor_estimate(self):
        manager = ProviderManager(
            {
                "api_key": fake_openai_key("test"),
                "primary_billing_json": '{"success_cost":0.03,"currency":"USD"}',
            },
            "test-plugin",
        )
        provider = manager.get_image_api_provider_configs()[0]

        markdown = build_providers_status_markdown(
            [provider],
            {},
            global_mode="images",
            now=0,
            billing_stats={
                "providers": {
                    provider.provider_id: {
                        "balance_source": "manual_anchor_estimate",
                        "last_balance_after": 78.09,
                        "last_converted_balance": 78.09,
                        "currency": "CNY",
                    }
                }
            },
        )

        self.assertIn("78.09 CNY", markdown)
        self.assertIn("手动锚点估算", markdown)


class TestProviderNativeNFallbackMemory(unittest.TestCase):
    """``provider_images_native_n_unsupported`` / ``mark_provider_images_native_n_unsupported`` 持久化与读取。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._stats_path = Path(self._tmpdir) / "provider_stats.json"

        self._manager = ProviderManager(
            {
                "api_key": fake_openai_key("test"),
                "base_url": "https://primary.example/v1",
                "model": "gpt-image-2",
                "responses_model": "gpt-5.5",
            },
            "test-plugin-native-n",
        )
        # 将 stats 路径指向临时目录
        self._manager.provider_stats_path = lambda: self._stats_path  # type: ignore[method-assign]

        self._provider = self._manager.get_image_api_provider_configs()[0]

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_fresh_provider_returns_false(self):
        """新 Provider 的 images_native_n_unsupported 应为 False。"""
        self.assertFalse(
            self._manager.provider_images_native_n_unsupported(self._provider)
        )

    def test_mark_then_check_returns_true(self):
        """mark 后 provider_images_native_n_unsupported 返回 True。"""
        self._manager.mark_provider_images_native_n_unsupported(
            self._provider, "HTTP 400 Unknown parameter: 'n'."
        )
        self.assertTrue(
            self._manager.provider_images_native_n_unsupported(self._provider)
        )

    def test_mark_without_reason_still_reads_true(self):
        """不带 reason 调用 mark，unsupported 仍为 True。"""
        self._manager.mark_provider_images_native_n_unsupported(self._provider)
        self.assertTrue(
            self._manager.provider_images_native_n_unsupported(self._provider)
        )

    def test_mark_persists_to_disk(self):
        """mark 写入磁盘后，重新加载 reader 仍为 True。"""
        self._manager.mark_provider_images_native_n_unsupported(
            self._provider, "HTTP 400 Unknown parameter: 'n'."
        )
        # 清理缓存，重新加载
        self._manager._provider_stats_cache = None
        self.assertTrue(
            self._manager.provider_images_native_n_unsupported(self._provider)
        )

    def test_mark_stores_reason_field(self):
        """mark 时写入 reason 字段。"""
        reason = "HTTP 400 Unknown parameter: 'tools[0].n'."
        self._manager.mark_provider_images_native_n_unsupported(self._provider, reason)
        stats = self._manager.load_provider_stats()
        item = stats["providers"].get(self._provider.provider_id, {})
        self.assertTrue(item.get("images_native_n_unsupported"))
        self.assertIn("tools[0]", item.get("images_native_n_unsupported_reason", ""))
        self.assertIn("images_native_n_unsupported_at", item)

    def test_mark_stores_provider_metadata(self):
        """mark 也会更新 provider 基础元信息字段。"""
        self._manager.mark_provider_images_native_n_unsupported(
            self._provider, "some error"
        )
        stats = self._manager.load_provider_stats()
        item = stats["providers"].get(self._provider.provider_id, {})
        self.assertEqual(item.get("name"), self._provider.name)
        self.assertEqual(item.get("base_url"), self._provider.base_url)
        self.assertEqual(item.get("model"), self._provider.model)
        self.assertEqual(item.get("role"), self._provider.role)
        self.assertIn("updated_at", item)

    def test_independent_of_adaptive_priority_off(self):
        """adaptive priority 关闭时仍能读取和标记。"""
        # 不启用 adaptive
        self._manager.mark_provider_images_native_n_unsupported(
            self._provider, "n not supported"
        )
        # 验证已写入
        self.assertTrue(
            self._manager.provider_images_native_n_unsupported(self._provider)
        )
        # 再次确认磁盘持久化
        self._manager._provider_stats_cache = None
        self.assertTrue(
            self._manager.provider_images_native_n_unsupported(self._provider)
        )

    def test_survives_cache_clear_not_file_content(self):
        """缓存清除后不改变已持久化的值。"""
        self._manager.mark_provider_images_native_n_unsupported(self._provider, "error")
        # 直接修改文件中的值
        stats = self._manager.load_provider_stats()
        stats["providers"][self._provider.provider_id][
            "images_native_n_unsupported"
        ] = False
        self._manager.save_provider_stats()

        self._manager._provider_stats_cache = None
        self.assertFalse(
            self._manager.provider_images_native_n_unsupported(self._provider)
        )

    def test_different_provider_does_not_interfere(self):
        """另一个 Provider 的标记不影响当前 Provider。"""
        self._manager.mark_provider_images_native_n_unsupported(self._provider, "error")
        # 创建一个不同的 provider
        other_manager = ProviderManager(
            {
                "api_key": fake_openai_key("other"),
                "base_url": "https://other.example/v1",
            },
            "test-plugin-native-n",
        )
        other_manager.provider_stats_path = lambda: self._stats_path  # type: ignore[method-assign]
        other_providers = other_manager.get_image_api_provider_configs()
        other_provider = other_providers[0]

        self.assertTrue(
            self._manager.provider_images_native_n_unsupported(self._provider)
        )
        self.assertFalse(
            self._manager.provider_images_native_n_unsupported(other_provider)
        )


if __name__ == "__main__":
    unittest.main()
