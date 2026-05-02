"""
History Agent — runs once daily per region.

Fetches all type_ids traded in a region, pulls 30 days of OHLCV history for
each, and stores it in market_history. Also resolves item names/volumes for
any unseen type_ids.

ESI history endpoint caches for ~23 hours, so running more frequently wastes
requests without gaining new data.
"""

import asyncio
import logging
from datetime import date, timedelta

from .. import config, database
from ..esi_client import ESIClient

log = logging.getLogger(__name__)

HISTORY_DAYS_TO_KEEP = 30


async def run_once(esi: ESIClient, region_id: int, type_ids: list[int]) -> None:
    log.info("[history] Starting scan for region %s (%d types)", region_id, len(type_ids))

    cutoff = date.today() - timedelta(days=HISTORY_DAYS_TO_KEEP)

    total_rows = 0

    async def fetch_type(type_id: int) -> list[dict]:
        """Fetch history for one type and return filtered rows (no shared state)."""
        rows = await esi.get_market_history(region_id, type_id)
        result: list[dict] = []
        for r in rows:
            try:
                row_date = date.fromisoformat(r["date"])
            except (KeyError, ValueError):
                continue
            if row_date < cutoff:
                continue
            result.append({
                "region_id":   region_id,
                "type_id":     type_id,
                "date":        row_date,
                "average":     r.get("average"),
                "highest":     r.get("highest"),
                "lowest":      r.get("lowest"),
                "order_count": r.get("order_count"),
                "volume":      r.get("volume"),
            })
        return result

    # Process in small chunks with a brief pause between each to stay well inside
    # ESI's 100-errors-per-60s budget even when many types return 404.
    # Each coroutine returns its own rows list — no shared mutable state between
    # concurrent coroutines (avoids double-flush / lost-rows race condition).
    chunk_size = 20
    batch: list[dict] = []
    for i in range(0, len(type_ids), chunk_size):
        chunk = type_ids[i : i + chunk_size]
        chunk_results = await asyncio.gather(*[fetch_type(tid) for tid in chunk])
        for rows in chunk_results:
            batch.extend(rows)
        await asyncio.sleep(0.5)   # ~40 req/s max — gentle on the error budget
        if i % 1000 == 0 and i > 0:
            log.info("[history] Region %s: processed %d/%d types",
                     region_id, i, len(type_ids))
        # Flush every 2000 accumulated rows to keep memory bounded
        if len(batch) >= 2000:
            total_rows += await database.upsert_history_batch(batch)
            batch.clear()

    if batch:
        total_rows += await database.upsert_history_batch(batch)

    log.info("[history] Region %s complete — %d rows stored", region_id, total_rows)


async def resolve_unknown_types(esi: ESIClient, region_id: int, type_ids: list[int]) -> None:
    """Fetch name + packaged volume for any type not yet in item_types.

    Types already populated by the SDE loader (group_id IS NOT NULL) are
    skipped entirely — no ESI call needed.  Only truly unknown types (e.g.
    very recently released items not yet in the SDE) hit the ESI.
    """
    # Types with full SDE data already have group_id set
    rows = await database.pool().fetch(
        "SELECT type_id FROM item_types WHERE group_id IS NOT NULL"
    )
    sde_known = {r["type_id"] for r in rows}

    # Types in DB but without SDE data (legacy ESI-resolved rows)
    rows_esi = await database.pool().fetch(
        "SELECT type_id FROM item_types WHERE group_id IS NULL"
    )
    esi_known = {r["type_id"] for r in rows_esi}

    all_known = sde_known | esi_known
    unknown = [t for t in type_ids if t not in all_known]

    sde_count = sum(1 for t in type_ids if t in sde_known)
    if sde_count:
        log.debug("[history] Region %s: %d types already resolved via SDE",
                  region_id, sde_count)

    if not unknown:
        return

    log.info("[history] Resolving %d unknown types via ESI for region %s",
             len(unknown), region_id)

    async def resolve_one(type_id: int) -> None:
        info = await esi.get_type_info(type_id)
        if not info:
            return
        await database.upsert_type(
            type_id=type_id,
            name=info.get("name", f"Unknown [{type_id}]"),
            packaged_volume=info.get("packaged_volume") or info.get("volume") or 0.0,
        )

    chunk_size = 50
    for i in range(0, len(unknown), chunk_size):
        chunk = unknown[i : i + chunk_size]
        await asyncio.gather(*[resolve_one(tid) for tid in chunk])

    log.info("[history] ESI type resolution complete for region %s", region_id)
