"""Runtime billing observation for provider calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import time
from typing import Any, TypeVar

import httpx
from astrbot.api import logger

from .config import BillingConfig
from .records import BillingRecords, provider_url_parts


T = TypeVar("T")


@dataclass
class BillingObservation:
    """One observed provider attempt."""

    provider_id: str
    provider_name: str
    billing_type: str
    success: bool
    cost: float | None = None
    cost_units: int = 1
    currency: str = "USD"
    balance_unit: str = "USD"
    raw_balance_before: float | None = None
    raw_balance_after: float | None = None
    balance_before: float | None = None
    balance_after: float | None = None
    converted_balance_before: float | None = None
    converted_balance_after: float | None = None
    cost_source: str = ""
    error: str = ""

    @property
    def has_known_cost(self) -> bool:
        return isinstance(self.cost, (int, float))


class BillingTracker:
    """Observe balance/fixed billing around image API attempts."""

    def __init__(self, records: BillingRecords) -> None:
        self.records = records
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, provider_id: str) -> asyncio.Lock:
        lock = self._locks.get(provider_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[provider_id] = lock
        return lock

    async def observe_call(
        self,
        provider: Any,
        billing: BillingConfig | None,
        *,
        action: str,
        api_mode: str,
        cost_units: int = 1,
        result_cost_units: Callable[[T], int] | None = None,
        call: Callable[[], Awaitable[T]],
    ) -> tuple[T, BillingObservation | None]:
        """Run one provider API call and record a billing event when configured."""

        if billing is None or not billing.enabled:
            return await call(), None
        if billing.uses_balance:
            async with self._lock_for(str(provider.provider_id)):
                return await self._observe_balance(
                    provider,
                    billing,
                    action,
                    api_mode,
                    cost_units,
                    result_cost_units,
                    call,
                )
        return await self._observe_fixed(
            provider, billing, action, api_mode, cost_units, result_cost_units, call
        )

    async def query_provider_balance(
        self,
        provider: Any,
        billing: BillingConfig,
    ) -> BillingObservation:
        """Realtime balance query for ``/image2 balance``."""

        async with self._lock_for(str(provider.provider_id)):
            raw = await self._fetch_balance(provider, billing)
            normalized = raw * billing.scale if raw is not None else None
            converted = (
                normalized * billing.cost_multiplier if normalized is not None else None
            )
            obs = BillingObservation(
                provider_id=str(provider.provider_id),
                provider_name=str(provider.name),
                billing_type=billing.type,
                success=raw is not None,
                cost=None,
                currency=billing.currency,
                balance_unit=billing.balance_unit,
                raw_balance_after=raw,
                balance_after=normalized,
                converted_balance_after=converted,
                error="" if raw is not None else "balance query failed",
            )
            self._record_balance_cache(provider, billing, raw, normalized, converted)
        return obs

    async def _observe_balance(
        self,
        provider: Any,
        billing: BillingConfig,
        action: str,
        api_mode: str,
        cost_units: int,
        result_cost_units: Callable[[T], int] | None,
        call: Callable[[], Awaitable[T]],
    ) -> tuple[T, BillingObservation | None]:
        raw_before = await self._fetch_balance(provider, billing)
        success = False
        error_text = ""
        try:
            result = await call()
            success = True
            success_cost_units = self._resolve_cost_units(
                fallback=cost_units,
                result=result,
                result_cost_units=result_cost_units,
            )
        except Exception as e:
            error_text = str(e)
            raw_after = await self._fetch_balance(provider, billing)
            obs = self._balance_observation(
                provider,
                billing,
                success=False,
                raw_before=raw_before,
                raw_after=raw_after,
                cost_units=cost_units,
                error=error_text,
            )
            self._record_event(provider, billing, obs, action=action, api_mode=api_mode)
            try:
                setattr(e, "_gpt_image2_billing_observation", obs)
            except Exception:
                pass
            raise
        raw_after = await self._fetch_balance(provider, billing)
        obs = self._balance_observation(
            provider,
            billing,
            success=success,
            raw_before=raw_before,
            raw_after=raw_after,
            cost_units=success_cost_units,
            error=error_text,
        )
        self._record_event(provider, billing, obs, action=action, api_mode=api_mode)
        return result, obs

    async def _observe_fixed(
        self,
        provider: Any,
        billing: BillingConfig,
        action: str,
        api_mode: str,
        cost_units: int,
        result_cost_units: Callable[[T], int] | None,
        call: Callable[[], Awaitable[T]],
    ) -> tuple[T, BillingObservation | None]:
        try:
            result = await call()
        except Exception as e:
            obs = self._fixed_observation(
                provider, billing, success=False, cost_units=cost_units, error=str(e)
            )
            self._record_event(provider, billing, obs, action=action, api_mode=api_mode)
            try:
                setattr(e, "_gpt_image2_billing_observation", obs)
            except Exception:
                pass
            raise
        success_cost_units = self._resolve_cost_units(
            fallback=cost_units,
            result=result,
            result_cost_units=result_cost_units,
        )
        obs = self._fixed_observation(
            provider, billing, success=True, cost_units=success_cost_units
        )
        self._record_event(provider, billing, obs, action=action, api_mode=api_mode)
        return result, obs

    def _balance_observation(
        self,
        provider: Any,
        billing: BillingConfig,
        *,
        success: bool,
        raw_before: float | None,
        raw_after: float | None,
        cost_units: int = 1,
        error: str = "",
    ) -> BillingObservation:
        balance_before = raw_before * billing.scale if raw_before is not None else None
        balance_after = raw_after * billing.scale if raw_after is not None else None
        converted_before = (
            balance_before * billing.cost_multiplier
            if balance_before is not None
            else None
        )
        converted_after = (
            balance_after * billing.cost_multiplier
            if balance_after is not None
            else None
        )
        cost: float | None = None
        cost_source = ""
        if balance_before is not None and balance_after is not None:
            delta = balance_before - balance_after
            if delta >= 0:
                cost = delta * billing.cost_multiplier
                cost_source = "balance_delta"
        if cost is None and billing.has_fixed_fallback:
            cost = self._fixed_cost(billing, success=success, cost_units=cost_units)
            cost_source = "fixed_fallback"
        return BillingObservation(
            provider_id=str(provider.provider_id),
            provider_name=str(provider.name),
            billing_type=billing.type,
            success=success,
            cost=cost,
            cost_units=max(1, int(cost_units or 1)) if success else 1,
            currency=billing.currency,
            balance_unit=billing.balance_unit,
            raw_balance_before=raw_before,
            raw_balance_after=raw_after,
            balance_before=balance_before,
            balance_after=balance_after,
            converted_balance_before=converted_before,
            converted_balance_after=converted_after,
            cost_source=cost_source,
            error=error,
        )

    def _fixed_observation(
        self,
        provider: Any,
        billing: BillingConfig,
        *,
        success: bool,
        cost_units: int = 1,
        error: str = "",
    ) -> BillingObservation:
        return BillingObservation(
            provider_id=str(provider.provider_id),
            provider_name=str(provider.name),
            billing_type=billing.type,
            success=success,
            cost=self._fixed_cost(billing, success=success, cost_units=cost_units),
            cost_units=max(1, int(cost_units or 1)) if success else 1,
            currency=billing.currency,
            balance_unit=billing.currency,
            cost_source="fixed",
            error=error,
        )

    @staticmethod
    def _fixed_cost(
        billing: BillingConfig, *, success: bool, cost_units: int = 1
    ) -> float:
        if not success:
            return billing.failure_cost
        units = max(1, int(cost_units or 1))
        return billing.success_cost * units

    @staticmethod
    def _resolve_cost_units(
        *,
        fallback: int,
        result: T,
        result_cost_units: Callable[[T], int] | None,
    ) -> int:
        if result_cost_units is None:
            return max(1, int(fallback or 1))
        try:
            return max(1, int(result_cost_units(result)))
        except Exception:
            return max(1, int(fallback or 1))

    async def _fetch_balance(
        self, provider: Any, billing: BillingConfig
    ) -> float | None:
        if billing.uses_total_usage:
            return await self._fetch_total_usage_balance(provider, billing)
        if not billing.balance_url:
            return None
        balance_raw = await self._fetch_number(
            provider,
            billing,
            url=billing.balance_url,
            method=billing.method,
            json_path=billing.balance_json_path,
            label="balance",
        )
        return balance_raw

    async def _fetch_total_usage_balance(
        self, provider: Any, billing: BillingConfig
    ) -> float | None:
        if not billing.total_url or not billing.usage_url:
            return None
        total_raw = await self._fetch_number(
            provider,
            billing,
            url=billing.total_url,
            method=billing.total_method,
            json_path=billing.total_json_path,
            label="total",
        )
        if total_raw is None:
            return None
        usage_raw = await self._fetch_number(
            provider,
            billing,
            url=billing.usage_url,
            method=billing.usage_method,
            json_path=billing.usage_json_path,
            label="usage",
        )
        if usage_raw is None:
            return None
        scale = billing.scale if billing.scale != 0 else 1.0
        return total_raw - (usage_raw * billing.usage_scale / scale)

    async def _fetch_number(
        self,
        provider: Any,
        billing: BillingConfig,
        *,
        url: str,
        method: str,
        json_path: str,
        label: str,
    ) -> float | None:
        headers: dict[str, str] = {}
        token = billing.api_key or str(getattr(provider, "api_key", "") or "")
        if token:
            if billing.auth in {"bearer", "header", "authorization"}:
                headers["Authorization"] = f"Bearer {token}"
            elif billing.auth in {"x-api-key", "api-key", "apikey"}:
                headers["X-API-Key"] = token
        try:
            async with httpx.AsyncClient(timeout=billing.timeout) as client:
                if method == "POST":
                    resp = await client.post(url, headers=headers)
                else:
                    resp = await client.get(url, headers=headers)
            if not resp.is_success:
                logger.warning(
                    "[GPTImage2] billing balance query failed "
                    f"provider={getattr(provider, 'name', '-')} label={label} "
                    f"status={resp.status_code}"
                )
                return None
            data = resp.json()
        except Exception as e:
            logger.warning(
                "[GPTImage2] billing balance query failed "
                f"provider={getattr(provider, 'name', '-')} label={label} "
                f"error={type(e).__name__}: {e}"
            )
            return None
        return self._extract_number(data, json_path)

    @classmethod
    def _extract_number(cls, data: Any, path: str) -> float | None:
        value = data
        for part in [
            p
            for p in str(path or "").replace("[", ".").replace("]", "").split(".")
            if p
        ]:
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list):
                try:
                    value = value[int(part)]
                except (TypeError, ValueError, IndexError):
                    return None
            else:
                return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _record_balance_cache(
        self,
        provider: Any,
        billing: BillingConfig,
        raw: float | None,
        normalized: float | None,
        converted: float | None,
    ) -> None:
        stats = self.records.load_billing_stats()
        providers = stats.setdefault("providers", {})
        item = providers.setdefault(str(provider.provider_id), {})
        item.update(
            {
                "provider_name": str(provider.name),
                "base_url": str(provider.base_url),
                "role": str(provider.role),
                "billing_type": billing.type,
                "currency": billing.currency,
                "balance_unit": billing.balance_unit,
                "last_raw_balance_after": raw,
                "last_balance_after": normalized,
                "last_converted_balance": converted,
                "last_balance_query_at": time(),
                "updated_at": time(),
            }
        )
        self.records.save_billing_stats()

    def _record_event(
        self,
        provider: Any,
        billing: BillingConfig,
        obs: BillingObservation,
        *,
        action: str,
        api_mode: str,
    ) -> None:
        host, path = provider_url_parts(str(provider.base_url))
        self.records.record_event(
            {
                "timestamp": time(),
                "provider_id": obs.provider_id,
                "provider_name": obs.provider_name,
                "base_url": str(provider.base_url),
                "base_url_host": host,
                "base_url_path": path,
                "role": str(provider.role),
                "action": action,
                "api_mode": api_mode,
                "billing_type": billing.type,
                "success": obs.success,
                "cost": obs.cost,
                "cost_units": obs.cost_units,
                "currency": obs.currency,
                "balance_unit": obs.balance_unit,
                "raw_balance_before": obs.raw_balance_before,
                "raw_balance_after": obs.raw_balance_after,
                "balance_before": obs.balance_before,
                "balance_after": obs.balance_after,
                "converted_balance_before": obs.converted_balance_before,
                "converted_balance_after": obs.converted_balance_after,
                "cost_source": obs.cost_source,
                "error_preview": obs.error[:240] if obs.error else "",
            }
        )
