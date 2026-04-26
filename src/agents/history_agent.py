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
    batch: list[dict] = []

    async def fetch_and_queue(type_id: int) -> None:
        nonlocal total_rows
        rows = await esi.get_market_history(region_id, type_id)
        for r in rows:
            try:
                row_date = date.fromisoformat(r["date"])
            except (KeyError, ValueError):
                continue
            if row_date < cutoff:
                continue
            batch.append({
                "region_id":   region_id,
                "type_id":     type_id,
                "date":        row_date,
                "average":     r.get("average"),
                "highest":     r.get("highest"),
                "lowest":      r.get("lowest"),
                "order_count": r.get("order_count"),
                "volume":      r.get("volume"),
            })
            # Flush every 2000 rows to keep memory bounded
            if len(batch) >= 2000:
                inserted = await database.upsert_history_batch(batch)
                total_rows += inserted
                batch.clear()

    # Process in chunks to avoid building a massive task list
    chunk_size = 100
    for i in range(0, len(type_ids), chunk_size):
        chunk = type_ids[i : i + chunk_size]
        await asyncio.gather(*[fetch_and_queue(tid) for tid in chunk])
        log.debug("[history] Region %s: processed %d/%d types",
                  region_id, min(i + chunk_size, len(type_ids)), len(type_ids))

    if batch:
        total_rows += await database.upsert_history_batch(batch)

    log.info("[history] Region %s complete — %d rows stored", region_id, total_rows)


async def resolve_unknown_types(esi: ESIClient, region_id: int, type_ids: list[int]) -> None:
    """Fetch name + packaged volume for any type not yet in item_types."""
    rows = await database.pool().fetch("SELECT type_id FROM item_types")
    known = {r["type_id"] for r in rows}
    unknown = [t for t in type_ids if t not in known]

    if not unknown:
        return

    log.info("[history] Resolving %d unknown type names for region %s",
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

    log.info("[history] Type resolution complete for region %s", region_id)
