"""
Order Agent — one instance per hub region, polls every 5 minutes.

Fetches all sell orders for a region (paginated) and stores them.
ETags mean ESI returns 304 Not Modified when nothing has changed,
keeping bandwidth near zero between updates.

Pages are fetched and inserted in batches (PAGE_BATCH × ~1 000 orders) to
cap peak Python memory usage.  Accumulating all pages before inserting would
hold ~285 000 order dicts in memory for Jita, which can push the container
over its 256 MB Docker limit.
"""

import asyncio
import logging
from datetime import datetime, timezone

from ..config import Hub
from .. import config, database
from ..esi_client import ESIClient

log = logging.getLogger(__name__)

# Number of ESI pages to fetch+insert per batch.
# Each page ≈ 1 000 orders × ~400 B ≈ 400 KB; 20 pages ≈ 8 MB in flight.
_PAGE_BATCH = config.ESI_CONCURRENCY   # 20


def _to_row(o: dict, region_id: int) -> dict:
    return {
        "order_id":      o["order_id"],
        "region_id":     region_id,
        "type_id":       o["type_id"],
        "location_id":   o["location_id"],
        "is_buy_order":  o.get("is_buy_order", False),
        "price":         o["price"],
        "volume_remain": o["volume_remain"],
        "volume_total":  o["volume_total"],
        "min_volume":    o.get("min_volume", 1),
        "range":         o.get("range"),
        "issued":        datetime.fromisoformat(o["issued"].replace("Z", "+00:00")),
        "duration":      o["duration"],
    }


async def run_once(esi: ESIClient, hub: Hub) -> int:
    """
    Fetch and store all sell orders for hub.region_id.
    Returns the number of rows stored.

    Fetches _PAGE_BATCH pages concurrently, converts and inserts them, then
    moves to the next batch — so peak memory is O(batch × 1 000 orders) rather
    than O(total_pages × 1 000 orders).
    """
    log.info("[orders] Scanning %s (region %s)", hub.name, hub.region_id)

    # Page 1 also reveals total_pages via the X-Pages header.
    first_page, total_pages = await esi.get_market_orders(
        hub.region_id, order_type="sell", page=1
    )

    if not first_page and total_pages <= 1:
        # Likely a 304 Not Modified (ETag cache hit) — nothing to store.
        log.debug("[orders] No data returned (likely 304 Not Modified) for %s", hub.name)
        return 0

    total_stored = 0

    # Insert the first page immediately so we don't have to hold it across
    # the full multi-batch fetch.
    if first_page:
        rows = [_to_row(o, hub.region_id) for o in first_page]
        total_stored += await database.upsert_orders_batch(rows)

    # Fetch and insert remaining pages in batches of _PAGE_BATCH.
    remaining = list(range(2, total_pages + 1))
    for i in range(0, len(remaining), _PAGE_BATCH):
        batch_pages = remaining[i : i + _PAGE_BATCH]
        tasks = [
            esi.get_market_orders(hub.region_id, order_type="sell", page=p)
            for p in batch_pages
        ]
        results = await asyncio.gather(*tasks)

        batch_rows: list[dict] = []
        for page_orders, _ in results:
            if page_orders:
                batch_rows.extend(_to_row(o, hub.region_id) for o in page_orders)

        if batch_rows:
            total_stored += await database.upsert_orders_batch(batch_rows)

    log.info("[orders] %s — stored %d sell orders", hub.name, total_stored)
    return total_stored
