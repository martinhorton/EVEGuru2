"""
Async ESI client with ETag caching, rate-limit awareness, and pagination.
All market endpoints used here are public — no authentication required.
"""

import asyncio
import logging
import time
import urllib.parse
from typing import Any

import aiohttp

from . import config

log = logging.getLogger(__name__)

_PARAMS = {"datasource": config.ESI_DATASOURCE}
_HEADERS = {"User-Agent": config.ESI_USER_AGENT, "Accept": "application/json"}


class ESIClient:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._etags: dict[str, str] = {}
        self._semaphore = asyncio.Semaphore(config.ESI_CONCURRENCY)
        # Error budget tracking — ESI allows 100 errors per window
        self._budget_remain: int = 100
        self._budget_reset_at: float = 0.0   # epoch seconds
        # Lock so only one coroutine handles the budget-low pause at a time;
        # the rest queue up and proceed immediately once budget is restored.
        self._budget_lock = asyncio.Lock()

    async def __aenter__(self) -> "ESIClient":
        connector = aiohttp.TCPConnector(limit=config.ESI_CONCURRENCY + 5)
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        self._session = aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=_HEADERS
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()

    def _update_budget(self, headers: dict) -> None:
        remain = headers.get("X-Esi-Error-Limit-Remain")
        reset  = headers.get("X-Esi-Error-Limit-Reset")
        if remain is not None:
            self._budget_remain = int(remain)
        if reset is not None:
            self._budget_reset_at = time.monotonic() + int(reset)

    async def _wait_if_budget_low(self) -> None:
        """Block all requests if the ESI error budget is critically low.

        Uses a lock so only ONE coroutine does the waiting — all others queue
        up and proceed immediately once the first one has restored the budget.
        This prevents the 20-simultaneous-warning storm when running at full
        concurrency.
        """
        async with self._budget_lock:
            if self._budget_remain < 10:
                sleep_for = max(0.0, self._budget_reset_at - time.monotonic()) + 2
                log.warning(
                    "ESI error budget critical (%d remaining) — pausing %.0fs for reset",
                    self._budget_remain, sleep_for,
                )
                await asyncio.sleep(sleep_for)
                self._budget_remain = 100  # will be corrected on next response
            elif self._budget_remain < 25:
                # Gentle throttle — add a small delay per request
                await asyncio.sleep(0.2)

    async def _get(
        self, path: str, params: dict | None = None, retries: int = 3
    ) -> tuple[list | dict | None, dict]:
        """
        Returns (data, response_headers). data is None on 304 (not modified).
        Handles ETag caching, error-limit headers, and retries on 5xx.
        """
        url = f"{config.ESI_BASE_URL}{path}"
        merged = {**_PARAMS, **(params or {})}
        # Cache key must include query params — different type_ids must not share an ETag
        cache_key = url + "?" + urllib.parse.urlencode(sorted(merged.items()))
        req_headers: dict[str, str] = {}
        if cache_key in self._etags:
            req_headers["If-None-Match"] = self._etags[cache_key]

        await self._wait_if_budget_low()

        for attempt in range(retries):
            backoff_secs: float = 0.0
            rate_limited = False

            async with self._semaphore:
                try:
                    assert self._session is not None
                    async with self._session.get(
                        url, params=merged, headers=req_headers
                    ) as resp:
                        self._update_budget(resp.headers)

                        if resp.status == 304:
                            return None, dict(resp.headers)

                        if resp.status == 200:
                            etag = resp.headers.get("ETag")
                            if etag:
                                self._etags[cache_key] = etag
                            data = await resp.json(content_type=None)
                            return data, dict(resp.headers)

                        if resp.status == 404:
                            # Normal for market history — type has no data in this region.
                            self._update_budget(resp.headers)
                            return None, {}

                        if resp.status in (502, 503, 504):
                            backoff_secs = float(2 ** attempt)
                            log.warning("ESI %s for %s, retry in %.0fs", resp.status, path, backoff_secs)

                        elif resp.status in (420, 429):
                            # 420 = ESI error-limit hit; 429 = Too Many Requests.
                            # Signal all coroutines to pause via the budget mechanism so
                            # they back off through _wait_if_budget_low rather than each
                            # sleeping independently while holding the semaphore.
                            reset = int(resp.headers.get("X-Esi-Error-Limit-Reset", 60))
                            self._budget_remain = 0
                            self._budget_reset_at = time.monotonic() + reset
                            backoff_secs = float(reset + 2)
                            rate_limited = True
                            log.warning(
                                "ESI %s rate-limit on %s — pausing %.0fs (semaphore released)",
                                resp.status, path, backoff_secs,
                            )

                        else:
                            log.error("ESI %s: %s", resp.status, path)
                            return None, {}

                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log.warning("ESI request failed (%s): %s", path, exc)
                    if attempt < retries - 1:
                        backoff_secs = float(2 ** attempt)

            # ── Semaphore released ─────────────────���──────────────────────────
            # All sleeps happen here so the slot is free during the wait.
            if backoff_secs:
                await asyncio.sleep(backoff_secs)
            if rate_limited:
                self._budget_remain = 100  # conservative reset; real value from next response

        return None, {}

    async def get_region_types(self, region_id: int) -> list[int]:
        """All type_ids being actively traded in a region."""
        all_types: list[int] = []
        page = 1
        while True:
            data, headers = await self._get(
                f"/markets/{region_id}/types/", params={"page": page}
            )
            if data is None:
                break
            all_types.extend(data)
            pages = int(headers.get("X-Pages", 1))
            if page >= pages:
                break
            page += 1
        return all_types

    async def get_market_history(self, region_id: int, type_id: int) -> list[dict]:
        data, _ = await self._get(
            f"/markets/{region_id}/history/",
            params={"type_id": type_id},
        )
        return data or []

    async def get_market_orders(
        self, region_id: int, order_type: str = "sell", page: int = 1
    ) -> tuple[list[dict], int]:
        """Returns (orders, total_pages). order_type: 'sell' | 'buy' | 'all'."""
        data, headers = await self._get(
            f"/markets/{region_id}/orders/",
            params={"order_type": order_type, "page": page},
        )
        pages = int(headers.get("X-Pages", 1))
        return (data or []), pages

    async def get_all_market_orders(
        self, region_id: int, order_type: str = "sell"
    ) -> list[dict]:
        """Fetch all pages of orders for a region concurrently."""
        first_page, total_pages = await self.get_market_orders(
            region_id, order_type, page=1
        )
        if total_pages <= 1:
            return first_page

        tasks = [
            self.get_market_orders(region_id, order_type, page=p)
            for p in range(2, total_pages + 1)
        ]
        results = await asyncio.gather(*tasks)
        all_orders = list(first_page)
        for orders, _ in results:
            all_orders.extend(orders)
        return all_orders

    async def get_type_info(self, type_id: int) -> dict:
        data, _ = await self._get(f"/universe/types/{type_id}/")
        return data or {}
