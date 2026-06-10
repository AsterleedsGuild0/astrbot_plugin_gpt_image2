"""Billing event and aggregate persistence."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from time import time
from urllib.parse import urlparse

from astrbot.api import logger


BILLING_STATS_SCHEMA_VERSION = 1


def trim_jsonl(path: Path, max_lines: int = 5000) -> None:
    """Trim JSONL file to keep only the last ``max_lines`` entries."""
    try:
        if not path.exists() or path.stat().st_size < 1024 * 100:
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= max_lines:
            return
        path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    except Exception as e:
        logger.debug(
            f"[GPTImage2] billing jsonl trim skipped error={type(e).__name__}: {e}"
        )


class BillingRecords:
    """Read/write billing stats and recent billing events."""

    def __init__(self, plugin_name: str | Callable[[], str]) -> None:
        self._plugin_name = plugin_name
        self._billing_stats_cache: dict | None = None

    def plugin_name(self) -> str:
        if callable(self._plugin_name):
            return str(self._plugin_name())
        return str(self._plugin_name)

    def _plugin_data_dir(self) -> Path:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path()) / "plugin_data" / self.plugin_name()

    def billing_stats_path(self) -> Path:
        return self._plugin_data_dir() / "billing_stats.json"

    def billing_events_jsonl_path(self) -> Path:
        return self._plugin_data_dir() / "billing_events.jsonl"

    def load_billing_stats(self) -> dict:
        if self._billing_stats_cache is not None:
            return self._billing_stats_cache

        path = self.billing_stats_path()
        if not path.exists():
            self._billing_stats_cache = {
                "version": BILLING_STATS_SCHEMA_VERSION,
                "providers": {},
                "summary": {},
            }
            return self._billing_stats_cache

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(
                "[GPTImage2] billing stats load failed "
                f"path={path} error={type(e).__name__}: {e}"
            )
            data = {}

        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("providers"), dict):
            data["providers"] = {}
        if not isinstance(data.get("summary"), dict):
            data["summary"] = {}
        data["version"] = BILLING_STATS_SCHEMA_VERSION
        self._billing_stats_cache = data
        return data

    def save_billing_stats(self) -> None:
        if self._billing_stats_cache is None:
            return
        path = self.billing_stats_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._billing_stats_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(
                "[GPTImage2] billing stats save failed "
                f"path={path} error={type(e).__name__}: {e}"
            )

    @staticmethod
    def _as_float(value: object, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _refresh_manual_balance_estimate(cls, item: dict) -> None:
        if item.get("manual_balance_anchor") is not True:
            return
        anchor_balance = cls._as_float(item.get("manual_anchor_balance"))
        anchor_total_cost = cls._as_float(item.get("manual_anchor_total_cost"))
        total_cost = cls._as_float(item.get("total_cost"))
        balance_multiplier = cls._as_float(
            item.get("manual_anchor_balance_multiplier"), 1.0
        )
        if balance_multiplier == 0:
            balance_multiplier = 1.0
        spent_since_anchor = (total_cost - anchor_total_cost) / balance_multiplier
        estimated_balance = anchor_balance - spent_since_anchor
        item["balance_source"] = "manual_anchor_estimate"
        item["last_balance_after"] = estimated_balance
        item["last_converted_balance"] = estimated_balance * balance_multiplier
        item["last_balance_query_at"] = time()

    def set_balance_anchor(
        self,
        *,
        provider_id: str,
        provider_name: str,
        base_url: str,
        role: str,
        amount: float,
        currency: str,
        balance_multiplier: float = 1.0,
    ) -> dict:
        """Set a manual balance anchor for providers without realtime balance APIs."""

        stats = self.load_billing_stats()
        providers = stats.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            stats["providers"] = providers
        item = providers.get(provider_id)
        if not isinstance(item, dict):
            item = {}
            providers[provider_id] = item
        now = time()
        current_total_cost = self._as_float(item.get("total_cost"))
        if balance_multiplier == 0:
            balance_multiplier = 1.0
        item.update(
            {
                "provider_name": provider_name,
                "base_url": base_url,
                "role": role,
                "manual_balance_anchor": True,
                "manual_anchor_balance": float(amount),
                "manual_anchor_currency": currency,
                "manual_anchor_balance_multiplier": float(balance_multiplier),
                "manual_anchor_total_cost": current_total_cost,
                "manual_anchor_at": now,
                "currency": currency,
                "updated_at": now,
            }
        )
        self._refresh_manual_balance_estimate(item)
        self._update_summary(stats)
        self.save_billing_stats()
        return item

    @staticmethod
    def _inc(item: dict, key: str, amount: int = 1) -> None:
        try:
            current = int(item.get(key, 0) or 0)
        except (TypeError, ValueError):
            current = 0
        item[key] = current + amount

    @staticmethod
    def _add_float(item: dict, key: str, amount: float) -> None:
        try:
            current = float(item.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            current = 0.0
        item[key] = current + float(amount)

    def record_event(self, record: dict) -> None:
        """Append one billing event and update aggregate stats."""

        record = dict(record)
        record.setdefault("timestamp", time())
        provider_id = str(record.get("provider_id") or "unknown")
        cost = record.get("cost")

        stats = self.load_billing_stats()
        providers = stats.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            stats["providers"] = providers
        item = providers.get(provider_id)
        if not isinstance(item, dict):
            item = {}
            providers[provider_id] = item

        for key in (
            "provider_name",
            "base_url",
            "role",
            "billing_type",
            "currency",
        ):
            if record.get(key) not in {None, ""}:
                item[key] = record[key]
        item["updated_at"] = record["timestamp"]
        self._inc(item, "event_count")
        if record.get("success") is True:
            self._inc(item, "success_count")
        elif record.get("success") is False:
            self._inc(item, "failure_count")

        if isinstance(cost, (int, float)):
            self._inc(item, "cost_count")
            self._add_float(item, "total_cost", float(cost))
            item["last_cost"] = float(cost)
            item["avg_cost"] = item["total_cost"] / max(1, item["cost_count"])
        else:
            self._inc(item, "unknown_count")
        cost_units = record.get("cost_units")
        try:
            cost_units_value = int(cost_units)
        except (TypeError, ValueError):
            cost_units_value = 0
        if cost_units_value > 0:
            self._add_float(item, "total_cost_units", float(cost_units_value))
            item["last_cost_units"] = cost_units_value

        for key in ("balance_before", "balance_after", "raw_balance_after"):
            if record.get(key) is not None:
                item[f"last_{key}"] = record[key]

        self._refresh_manual_balance_estimate(item)

        self._update_summary(stats)
        self.save_billing_stats()
        self._append_event(record)

    def _update_summary(self, stats: dict) -> None:
        providers = stats.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        summary = {
            "provider_count": len(providers),
            "total_cost": 0.0,
            "cost_count": 0,
            "unknown_count": 0,
            "event_count": 0,
            "updated_at": time(),
        }
        currencies: dict[str, float] = {}
        for item in providers.values():
            if not isinstance(item, dict):
                continue
            try:
                total_cost = float(item.get("total_cost", 0.0) or 0.0)
            except (TypeError, ValueError):
                total_cost = 0.0
            try:
                cost_count = int(item.get("cost_count", 0) or 0)
                unknown_count = int(item.get("unknown_count", 0) or 0)
                event_count = int(item.get("event_count", 0) or 0)
            except (TypeError, ValueError):
                cost_count = unknown_count = event_count = 0
            summary["total_cost"] += total_cost
            summary["cost_count"] += cost_count
            summary["unknown_count"] += unknown_count
            summary["event_count"] += event_count
            currency = str(item.get("currency") or "").strip() or "unknown"
            currencies[currency] = currencies.get(currency, 0.0) + total_cost
        summary["totals_by_currency"] = currencies
        stats["summary"] = summary
        stats["version"] = BILLING_STATS_SCHEMA_VERSION

    def _append_event(self, record: dict) -> None:
        path = self.billing_events_jsonl_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            trim_jsonl(path, max_lines=5000)
        except Exception as e:
            logger.warning(
                "[GPTImage2] failed to append billing event "
                f"error={type(e).__name__}: {e}"
            )

    def read_recent_events(self, count: int) -> list[dict]:
        path = self.billing_events_jsonl_path()
        try:
            if not path.exists():
                return []
            lines = path.read_text(encoding="utf-8").splitlines()[-count:]
        except Exception:
            return []
        records: list[dict] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records


def provider_url_parts(base_url: str) -> tuple[str, str]:
    try:
        parsed = urlparse(base_url)
        return parsed.hostname or "-", parsed.path or "-"
    except Exception:
        return "-", "-"
