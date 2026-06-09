"""Billing configuration parsing.

Billing is bound to an image API provider.  A provider may either expose a
balance endpoint that can be queried before/after an image request, or a fixed
fallback cost that is recorded when balance observation is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class BillingConfig:
    """Normalized provider billing configuration."""

    type: str
    balance_url: str = ""
    total_url: str = ""
    usage_url: str = ""
    method: str = "GET"
    total_method: str = "GET"
    usage_method: str = "GET"
    auth: str = "bearer"
    api_key: str = ""
    balance_json_path: str = "balance"
    total_json_path: str = "total"
    usage_json_path: str = "total_usage"
    balance_unit: str = "USD"
    currency: str = "USD"
    scale: float = 1.0
    usage_scale: float = 1.0
    cost_multiplier: float = 1.0
    timeout: float = 8.0
    success_cost: float = 0.0
    failure_cost: float = 0.0
    fixed_fallback_enabled: bool = False

    @property
    def enabled(self) -> bool:
        return self.type in {"balance", "total_usage", "fixed"}

    @property
    def uses_balance(self) -> bool:
        return self.type in {"balance", "total_usage"}

    @property
    def uses_total_usage(self) -> bool:
        return self.type == "total_usage"

    @property
    def uses_fixed(self) -> bool:
        return self.type == "fixed"

    @property
    def has_fixed_fallback(self) -> bool:
        return self.uses_fixed or self.fixed_fallback_enabled


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def parse_billing_config(value: Any) -> BillingConfig | None:
    """Parse a user supplied billing object/string.

    Invalid or disabled configuration returns ``None``. ``balance_url`` means a
    direct remaining-balance endpoint. ``total_url`` plus ``usage_url`` means a
    total-credit-minus-usage endpoint pair. ``success_cost`` and
    ``failure_cost`` are fixed reference/fallback costs.
    """

    data = _as_dict(value)
    if not data:
        return None

    method = str(data.get("method") or "GET").strip().upper() or "GET"
    if method not in {"GET", "POST"}:
        method = "GET"
    total_method = str(data.get("total_method") or method).strip().upper() or method
    if total_method not in {"GET", "POST"}:
        total_method = method
    usage_method = str(data.get("usage_method") or method).strip().upper() or method
    if usage_method not in {"GET", "POST"}:
        usage_method = method

    balance_unit = str(data.get("balance_unit") or data.get("unit") or "USD").strip()
    currency = str(data.get("currency") or balance_unit or "USD").strip()
    balance_url = str(data.get("balance_url") or "").strip()
    total_url = str(data.get("total_url") or "").strip()
    usage_url = str(data.get("usage_url") or "").strip()

    fixed_fallback = data.get("fixed_fallback")
    fixed_fallback_data = fixed_fallback if isinstance(fixed_fallback, dict) else {}
    fixed_present = any(
        key in data for key in {"success_cost", "failure_cost", "fixed_fallback"}
    )
    if balance_url:
        billing_type = "balance"
    elif total_url and usage_url:
        billing_type = "total_usage"
    elif fixed_present:
        billing_type = "fixed"
    else:
        return None
    success_cost_value = (
        fixed_fallback_data.get("success_cost")
        if "success_cost" in fixed_fallback_data
        else data.get("success_cost")
    )
    failure_cost_value = (
        fixed_fallback_data.get("failure_cost")
        if "failure_cost" in fixed_fallback_data
        else data.get("failure_cost")
    )
    fixed_fallback_enabled = billing_type == "fixed" or fixed_present

    return BillingConfig(
        type=billing_type,
        balance_url=balance_url,
        total_url=total_url,
        usage_url=usage_url,
        method=method,
        total_method=total_method,
        usage_method=usage_method,
        auth=str(data.get("auth") or "bearer").strip().lower() or "bearer",
        api_key=str(data.get("api_key") or "").strip(),
        balance_json_path=str(
            data.get("balance_json_path")
            or data.get("json_path")
            or data.get("path")
            or "balance"
        ).strip(),
        total_json_path=str(
            data.get("total_json_path") or data.get("total_path") or "total"
        ).strip(),
        usage_json_path=str(
            data.get("usage_json_path") or data.get("usage_path") or "total_usage"
        ).strip(),
        balance_unit=balance_unit or "USD",
        currency=currency or "USD",
        scale=_as_float(data.get("scale"), 1.0),
        usage_scale=_as_float(data.get("usage_scale"), 1.0),
        cost_multiplier=_as_float(data.get("cost_multiplier"), 1.0),
        timeout=max(1.0, _as_float(data.get("timeout"), 8.0)),
        success_cost=max(0.0, _as_float(success_cost_value, 0.0)),
        failure_cost=max(0.0, _as_float(failure_cost_value, 0.0)),
        fixed_fallback_enabled=fixed_fallback_enabled,
    )
