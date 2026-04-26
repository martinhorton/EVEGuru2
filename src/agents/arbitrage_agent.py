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
    MAX_DAYS_SUPPLY, MIN_DAILY_VOLUME, MIN_MARGIN_PCT, MIN_PROFIT_ISK,
    PRICE_SANITY_MULTIPLIER, SHIPPING_ISK_PER_M3, SELL_OVERHEAD_PCT,
    DEMAND_WINDOW_DAYS,
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

    # Accept if EITHER the % margin is good enough OR the absolute ISK profit
    # per unit clears the floor.  The ISK floor catches high-value items (e.g.
    # large ships) where significant ISK profit is produced at a low % margin
    # because shipping cost is a large fraction of unit price.
    if margin_pct < MIN_MARGIN_PCT and profit < MIN_PROFIT_ISK:
        return None

    # Store days-of-supply (supply / demand) — matches "D.O.S." column in
    # commercial EVE trading apps.  Cap at 9999 for zero-demand edge cases.
    shortage_ratio = current_supply / max(avg_daily_volume, 0.001)
    shortage_ratio = min(shortage_ratio, 9999.0)

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

            # Skip if the hub already has more than MAX_DAYS_SUPPLY days of stock.
            # days_of_supply = supply / daily_demand.  Items with 0 supply always pass.
            if current_supply > 0 and avg_vol > 0:
                days_of_supply = current_supply / avg_vol
                if days_of_supply > MAX_DAYS_SUPPLY:
                    continue

            # Shortage confirmed — check Jita
            # Use a realistic source price: cheapest tier where cumulative supply
            # covers at least one day's demand (avoids single-unit lowball orders)
            jita_price = await database.get_realistic_buy_price_at_station(
                SUPPLY_HUB.station_id, type_id, min_quantity=avg_vol
            )
            if jita_price is None:
                continue  # Not available in Jita

            # Get both the current sell price and the 7-day historical average
            target_price = await database.get_cheapest_sell_at_station(
                hub.station_id, type_id
            )
            hist_avg = await database.get_avg_market_price(
                hub.region_id, type_id, days=DEMAND_WINDOW_DAYS
            )

            if target_price is None:
                # Nothing for sale — use historical average as proxy
                target_price = hist_avg
            elif hist_avg and target_price > hist_avg * PRICE_SANITY_MULTIPLIER:
                # Current order is far above historical norm — likely a scam/stale
                # listing.  Substitute the historical average so we calculate what
                # the trade would realistically yield.
                log.debug(
                    "[arbitrage] %s @ %s: sell %.2f is %.1f× hist avg %.2f — using hist avg",
                    type_id, hub.name, target_price, target_price / hist_avg, hist_avg,
                )
                target_price = hist_avg

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
