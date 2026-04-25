"""
Arbitrage Agent — runs after each order scan cycle.

For each target hub:
  1. Find items with meaningful 7-day demand but thin current supply.
  2. Check if Jita has cheaper sell orders that cover the gap.
  3. Calculate net margin after shipping + broker fee + sales tax.
  4. Write qualifying opportunities to the DB and log a summary.
"""

import logging

from ..config import (
    Hub, SUPPLY_HUB, TARGET_HUBS,
    SHORTAGE_RATIO, MIN_DAILY_VOLUME, MIN_MARGIN_PCT,
    SHIPPING_ISK_PER_M3, SELL_OVERHEAD_PCT, DEMAND_WINDOW_DAYS,
)
from .. import database

log = logging.getLogger(__name__)


def _calc_opportunity(
    *,
    type_id: int,
    type_name: str,
    packaged_volume: float,
    avg_daily_volume: float,
    current_supply: int,
    jita_price: float,
    target_sell_price: float,
    target_hub: Hub,
) -> dict | None:
    """
    Returns an opportunity dict if the trade meets margin thresholds, else None.

    Cost side:  jita_price + shipping_cost (per unit)
    Revenue:    target_sell_price * (1 - SELL_OVERHEAD_PCT)
    """
    shipping_cost = packaged_volume * SHIPPING_ISK_PER_M3
    total_cost = jita_price + shipping_cost
    net_revenue = target_sell_price * (1.0 - SELL_OVERHEAD_PCT)
    profit = net_revenue - total_cost

    if total_cost <= 0:
        return None

    margin_pct = (profit / total_cost) * 100.0

    if margin_pct < MIN_MARGIN_PCT:
        return None

    shortage_ratio = avg_daily_volume / max(current_supply, 1)

    return {
        "type_id":              type_id,
        "type_name":            type_name,
        "target_station_id":    target_hub.station_id,
        "target_hub_name":      target_hub.name,
        "supply_station_id":    SUPPLY_HUB.station_id,
        "avg_daily_volume":     avg_daily_volume,
        "current_supply_units": current_supply,
        "shortage_ratio":       shortage_ratio,
        "jita_sell_price":      jita_price,
        "target_sell_price":    target_sell_price,
        "shipping_cost":        shipping_cost,
        "total_cost":           total_cost,
        "expected_net_revenue": net_revenue,
        "margin_pct":           margin_pct,
    }


async def run_once() -> None:
    log.info("[arbitrage] Starting analysis pass")

    await database.deactivate_old_opportunities()

    total_found = 0

    for hub in TARGET_HUBS:
        # Types with real demand in this hub's region over the last 7 days
        candidate_types = await database.get_active_types_for_region(
            hub.region_id, days=DEMAND_WINDOW_DAYS, min_volume=MIN_DAILY_VOLUME
        )

        if not candidate_types:
            log.debug("[arbitrage] No candidate types for %s", hub.name)
            continue

        log.debug("[arbitrage] %s: evaluating %d candidate types", hub.name, len(candidate_types))

        hub_opps = 0
        for type_id in candidate_types:
            avg_vol = await database.get_avg_daily_volume(
                hub.region_id, type_id, days=DEMAND_WINDOW_DAYS
            )
            if not avg_vol or avg_vol < MIN_DAILY_VOLUME:
                continue

            current_supply = await database.get_sell_supply_at_station(
                hub.station_id, type_id
            )

            # Skip if supply is adequate (ratio below threshold)
            if current_supply > 0:
                ratio = avg_vol / current_supply
                if ratio < SHORTAGE_RATIO:
                    continue

            # Shortage confirmed — check Jita
            jita_price = await database.get_cheapest_sell_at_station(
                SUPPLY_HUB.station_id, type_id
            )
            if jita_price is None:
                continue  # Not available in Jita

            target_price = await database.get_cheapest_sell_at_station(
                hub.station_id, type_id
            )
            if target_price is None:
                # Nothing for sale here at all — use recent average as proxy
                row = await database.pool().fetchrow(
                    """
                    SELECT AVG(average)::float AS avg_price
                    FROM market_history
                    WHERE region_id = $1 AND type_id = $2
                      AND date >= CURRENT_DATE - '7 days'::interval
                    """,
                    hub.region_id, type_id,
                )
                target_price = row["avg_price"] if row and row["avg_price"] else None
                if not target_price:
                    continue

            packaged_volume = await database.get_type_volume(type_id) or 1.0
            type_name = await database.get_type_name(type_id) or f"Type {type_id}"

            opp = _calc_opportunity(
                type_id=type_id,
                type_name=type_name,
                packaged_volume=packaged_volume,
                avg_daily_volume=avg_vol,
                current_supply=current_supply,
                jita_price=jita_price,
                target_sell_price=target_price,
                target_hub=hub,
            )

            if opp:
                await database.insert_opportunity(opp)
                hub_opps += 1
                log.info(
                    "[arbitrage] OPPORTUNITY | %s @ %s | avg vol %.0f/day | "
                    "supply %d units | Jita %.2f → %s %.2f | margin %.1f%%",
                    type_name, hub.name, avg_vol, current_supply,
                    jita_price, hub.name, target_price, opp["margin_pct"],
                )

        log.info("[arbitrage] %s: %d opportunities found", hub.name, hub_opps)
        total_found += hub_opps

    log.info("[arbitrage] Pass complete — %d total opportunities", total_found)
