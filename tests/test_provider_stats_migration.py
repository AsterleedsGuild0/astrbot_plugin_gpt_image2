"""Provider 统计选择性清理迁移测试。"""

from __future__ import annotations

import copy
import sys
import unittest
from unittest.mock import MagicMock

# 测试环境没有 AstrBot 运行时，导入 provider/manager 前先 mock astrbot。
astrbot_api = MagicMock()
astrbot_api.logger = MagicMock()
sys.modules["astrbot"] = MagicMock()
sys.modules["astrbot.api"] = astrbot_api

from image2_core.providers.manager import (  # noqa: E402
    ImageAPIProviderConfig,
    PROVIDER_STATS_SCHEMA_VERSION,
    PROVIDER_STATS_SELECTIVE_CLEANUP_KEY,
    ProviderManager,
    migrate_provider_stats_selective_cleanup,
)


class TestMigrateProviderStatsSelectiveCleanup(unittest.TestCase):
    """migrate_provider_stats_selective_cleanup 的单元测试。"""

    def _assert_migrated(
        self,
        stats: dict,
        migration_result: tuple[bool, dict[str, int]],
        *,
        expected_failure_count: int | None = None,
        expected_unknown_removed: int = 0,
        expected_failure_removed: int = 0,
    ) -> None:
        """验证迁移后的通用断言。"""
        migrated, summary = migration_result
        self.assertTrue(migrated)
        self.assertIn(PROVIDER_STATS_SELECTIVE_CLEANUP_KEY, stats["migration"])
        marker = stats["migration"][PROVIDER_STATS_SELECTIVE_CLEANUP_KEY]
        self.assertGreater(marker["applied_at"], 0)
        self.assertEqual(stats["version"], PROVIDER_STATS_SCHEMA_VERSION)
        self.assertEqual(summary["unknown_removed"], expected_unknown_removed)
        self.assertEqual(summary["failure_removed"], expected_failure_removed)

        if expected_failure_count is not None:
            for item in stats.get("providers", {}).values():
                if isinstance(item, dict):
                    self.assertEqual(item["failure_count"], expected_failure_count)

    # ── Case 1: HC-like polluted old aggregate ─────────────────

    def test_hc_like_polluted_aggregate(self):
        """旧聚合含 unknown 与 HTTP 200，应移除 unknown、归入 api_schema_error。"""
        stats: dict = {
            "version": 1,
            "providers": {
                "hc-provider": {
                    "success_count": 10,
                    "failure_count": 56,
                    "failure_reasons": {
                        "unknown": 30,
                        "network_timeout": 9,
                        "http_5xx": 15,
                        "http_403": 2,
                    },
                    "failure_status_codes": {"502": 15, "403": 2, "200": 8},
                }
            },
        }
        result = migrate_provider_stats_selective_cleanup(stats)

        self._assert_migrated(
            stats,
            result,
            expected_failure_count=34,
            expected_unknown_removed=22,
            expected_failure_removed=22,
        )

        item = stats["providers"]["hc-provider"]
        reasons = item.get("failure_reasons", {})
        self.assertNotIn("unknown", reasons)
        self.assertIn("api_schema_error", reasons)
        self.assertEqual(reasons["api_schema_error"], 8)
        self.assertEqual(reasons["http_5xx"], 15)
        self.assertEqual(reasons["http_403"], 2)
        self.assertEqual(reasons["network_timeout"], 9)
        # 按计数降序排序
        reason_keys = list(reasons.keys())
        self.assertEqual(reason_keys[0], "http_5xx")  # 15
        self.assertEqual(reason_keys[2], "api_schema_error")  # 8

    # ── Case 2: Axis-like historical gap ─────────────────────

    def test_axis_like_historical_gap(self):
        """原因已细分但 failure_count 含不可量化缺口，应下调至已知和。"""
        stats: dict = {
            "version": 1,
            "providers": {
                "axis-provider": {
                    "success_count": 162,
                    "failure_count": 164,
                    "failure_reasons": {
                        "http_400": 67,
                        "http_5xx": 41,
                        "network_timeout": 17,
                        "network_connect": 4,
                    },
                    "failure_status_codes": {"400": 67, "502": 41},
                }
            },
        }
        result = migrate_provider_stats_selective_cleanup(stats)

        self._assert_migrated(
            stats,
            result,
            expected_failure_count=129,
            expected_unknown_removed=0,
            expected_failure_removed=35,
        )

        item = stats["providers"]["axis-provider"]
        reasons = item.get("failure_reasons", {})
        self.assertNotIn("unknown", reasons)
        self.assertEqual(reasons["http_400"], 67)
        self.assertEqual(reasons["http_5xx"], 41)
        self.assertEqual(reasons["network_timeout"], 17)
        self.assertEqual(reasons["network_connect"], 4)

    def test_status_code_backfill_does_not_expand_failure_count(self):
        """状态码补足不能因与既有原因重叠而扩大历史 failure_count。"""
        stats: dict = {
            "version": 2,
            "providers": {
                "provider-a": {
                    "success_count": 0,
                    "failure_count": 5,
                    "failure_reasons": {
                        "network_timeout": 5,
                    },
                    "failure_status_codes": {"400": 5},
                }
            },
        }

        result = migrate_provider_stats_selective_cleanup(stats)

        self._assert_migrated(
            stats,
            result,
            expected_failure_count=5,
            expected_unknown_removed=0,
            expected_failure_removed=0,
        )

        item = stats["providers"]["provider-a"]
        reasons = item.get("failure_reasons", {})
        self.assertEqual(sum(reasons.values()), 5)
        self.assertEqual(reasons["network_timeout"], 5)
        self.assertNotIn("http_400", reasons)

    def test_status_code_backfill_uses_historical_gap_without_expanding(self):
        """状态码可补足 failure_count 内的历史缺口，但不能超过原始总数。"""
        stats: dict = {
            "version": 2,
            "providers": {
                "provider-a": {
                    "success_count": 0,
                    "failure_count": 10,
                    "failure_reasons": {
                        "network_timeout": 5,
                    },
                    "failure_status_codes": {"400": 8},
                }
            },
        }

        result = migrate_provider_stats_selective_cleanup(stats)

        self._assert_migrated(
            stats,
            result,
            expected_failure_count=10,
            expected_unknown_removed=0,
            expected_failure_removed=0,
        )

        item = stats["providers"]["provider-a"]
        reasons = item.get("failure_reasons", {})
        self.assertEqual(sum(reasons.values()), 10)
        self.assertEqual(reasons["network_timeout"], 5)
        self.assertEqual(reasons["http_400"], 5)

    # ── Case 3: Idempotency ──────────────────────────────────

    def test_idempotency_already_migrated(self):
        """已迁移的数据再次执行迁移应返回 (False, {}) 且不修改数据。"""
        stats: dict = {
            "version": PROVIDER_STATS_SCHEMA_VERSION,
            "migration": {
                PROVIDER_STATS_SELECTIVE_CLEANUP_KEY: {
                    "applied_at": 1234567890.0,
                    "providers_changed": 1,
                    "unknown_removed": 22,
                    "failure_removed": 22,
                }
            },
            "providers": {
                "provider-a": {
                    "success_count": 10,
                    "failure_count": 34,
                    "failure_reasons": {
                        "http_5xx": 15,
                        "network_timeout": 9,
                        "api_schema_error": 8,
                        "http_403": 2,
                    },
                    "failure_status_codes": {"502": 15, "403": 2, "200": 8},
                    "consecutive_failures": 5,
                    "cooldown_until": 0,
                }
            },
        }
        before = copy.deepcopy(stats)

        migrated, summary = migrate_provider_stats_selective_cleanup(stats)

        self.assertFalse(migrated)
        self.assertEqual(summary, {})
        # 数据保持不变
        self.assertEqual(stats, before)

    def test_idempotency_empty_migration_dict_adds_marker(self):
        """migration 字段存在但为空 dict 时仍进行迁移（非重复保护）。"""
        stats: dict = {
            "version": 1,
            "migration": {},
            "providers": {
                "provider-a": {
                    "success_count": 10,
                    "failure_count": 56,
                    "failure_reasons": {"unknown": 30},
                    "failure_status_codes": {},
                }
            },
        }

        migrated, summary = migrate_provider_stats_selective_cleanup(stats)

        self.assertTrue(migrated)
        self.assertIn(PROVIDER_STATS_SELECTIVE_CLEANUP_KEY, stats["migration"])

    # ── Edge cases ───────────────────────────────────────────

    def test_non_dict_stats_returns_false(self):
        """stats 不是 dict 时返回 (False, {})."""
        migrated, summary = migrate_provider_stats_selective_cleanup("bad")  # type: ignore[arg-type]
        self.assertFalse(migrated)
        self.assertEqual(summary, {})

    def test_no_providers_key(self):
        """没有 providers 键也能正常返回迁移成功。"""
        stats: dict = {"version": 1}
        migrated, summary = migrate_provider_stats_selective_cleanup(stats)
        self.assertTrue(migrated)
        self.assertEqual(summary["providers_changed"], 0)
        self.assertIn(PROVIDER_STATS_SELECTIVE_CLEANUP_KEY, stats["migration"])

    def test_empty_providers(self):
        """providers 为空 dict。"""
        stats: dict = {"version": 1, "providers": {}}
        migrated, summary = migrate_provider_stats_selective_cleanup(stats)
        self.assertTrue(migrated)
        self.assertEqual(summary["providers_changed"], 0)
        self.assertIn(PROVIDER_STATS_SELECTIVE_CLEANUP_KEY, stats["migration"])


class TestProviderElapsedStats(unittest.TestCase):
    """验证 provider attempt 与全局任务耗时口径分离。"""

    def test_fallback_task_elapsed_is_not_provider_attempt_elapsed(self):
        """fallback 成功时，任务耗时不同于最终成功站点 attempt 耗时。"""
        manager = ProviderManager({"adaptive_provider_priority": True}, "test-plugin")
        manager._provider_stats_cache = {
            "version": PROVIDER_STATS_SCHEMA_VERSION,
            "providers": {},
        }
        manager.save_provider_stats = lambda: None  # type: ignore[method-assign]

        primary = ImageAPIProviderConfig(
            name="主站",
            api_key="",
            base_url="https://primary.example/v1",
            model="gpt-image-2",
            responses_model="gpt-5.5",
            provider_id="primary",
            configured_order=0,
        )
        fallback = ImageAPIProviderConfig(
            name="备用站",
            api_key="",
            base_url="https://fallback.example/v1",
            model="gpt-image-2",
            responses_model="gpt-5.5",
            provider_id="fallback",
            configured_order=1,
        )

        manager.record_image_provider_result(
            primary,
            success=False,
            error_msg="timeout",
            elapsed_ms=400,
        )
        manager.record_image_provider_result(fallback, success=True, elapsed_ms=100)
        manager.record_image_task_result(
            success=True,
            provider=fallback,
            elapsed_ms=900,
        )

        stats = manager.load_provider_stats()
        primary_item = stats["providers"]["primary"]
        fallback_item = stats["providers"]["fallback"]
        task_summary = stats["task_summary"]

        self.assertEqual(primary_item["failure_elapsed_ms_avg"], 400.0)
        self.assertEqual(fallback_item["success_elapsed_ms_avg"], 100.0)
        self.assertEqual(task_summary["success_elapsed_ms_avg"], 900.0)
        self.assertNotEqual(
            task_summary["success_elapsed_ms_avg"],
            fallback_item["success_elapsed_ms_avg"],
        )


if __name__ == "__main__":
    unittest.main()
