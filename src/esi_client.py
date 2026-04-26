"""
Async ESI client with ETag caching, rate-limit awareness, and pagination.
All market endpoints used here are public — no authentication required.
"""

import asyncio
import logging
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

        for attempt in range(retries):
            async with self._semaphore:
                try:
                    assert self._session is not None
                    async with self._session.get(
                        url, params=merged, headers=req_headers
                    ) as resp:
                        # Warn if we're burning through the error budget
                        remain = resp.headers.get("X-Esi-Error-Limit-Remain")
                        if remain and int(remain) < 20:
                            log.warning("ESI error budget low: %s remaining", remain)

                        if resp.status == 304:
                            return None, dict(resp.headers)

                        if resp.status == 200:
                            etag = resp.headers.get("ETag")
                            if etag:
                                self._etags[cache_key] = etag
                            data = await resp.json(content_type=None)
                            return data, dict(resp.headers)

                        if resp.status in (502, 503, 504):
                            wait = 2 ** attempt
                            log.warning("ESI %s for %s, retry in %ss", resp.status, path, wait)
                            await asyncio.sleep(wait)
                            continue

                        if resp.status == 420:
                            reset = int(resp.headers.get("X-Esi-Error-Limit-Reset", 60))
                            log.error("ESI error limit hit, sleeping %ss", reset)
                            await asyncio.sleep(reset)
                            continue

                        log.error("ESI %s: %s", resp.status, path)
                        return None, {}

                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    log.warning("ESI request failed (%s): %s", path, exc)
                    if attempt < retries - 1:
                        await asyncio.sleep(2 ** attempt)

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
