"""
Order Agent — one instance per hub region, polls every 5 minutes.

Fetches all sell orders for a region (paginated) and stores them.
ETags mean ESI returns 304 Not Modified when nothing has changed,
keeping bandwidth near zero between updates.
"""

import logging

from ..config import Hub
from .. import database
from ..esi_client import ESIClient

log = logging.getLogger(__name__)


async def run_once(esi: ESIClient, hub: Hub) -> int:
    """
    Fetch and store all sell orders for hub.region_id.
    Returns the number of rows stored.
    """
    log.info("[orders] Scanning %s (region %s)", hub.name, hub.region_id)

    raw_orders = await esi.get_all_market_orders(hub.region_id, order_type="sell")

    if not raw_orders:
        log.debug("[orders] No data returned (likely 304 Not Modified) for %s", hub.name)
        return 0

    rows = [
        {
            "order_id":     o["order_id"],
            "region_id":    hub.region_id,
            "type_id":      o["type_id"],
            "location_id":  o["location_id"],
            "is_buy_order": o.get("is_buy_order", False),
            "price":        o["price"],
            "volume_remain": o["volume_remain"],
            "volume_total": o["volume_total"],
            "min_volume":   o.get("min_volume", 1),
            "range":        o.get("range"),
            "issued":       o["issued"],
            "duration":     o["duration"],
        }
        for o in raw_orders
    ]

    stored = await database.upsert_orders_batch(rows)
    log.info("[orders] %s — stored %d sell orders", hub.name, stored)
    return stored
