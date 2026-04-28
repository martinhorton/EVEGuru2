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
    hist_avg_price: float | None,
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
        "hist_avg_price":       hist_avg_price,
        "shipping_cost":        shipping_cost,
        "total_cost":           total_cost,
        "expected_net_revenue": net_revenue,
        "margin_pct":           margin_pct,
        "packaged_volume":      packaged_volume,
    }


async def run_once() -> None:
    log.info("[arbitrage] Starting analysis pass")

    await database.deactivate_old_opportunities()

    total_found = 0

    for hub in TARGET_HUBS:
        # Single bulk query replaces the old per-type N+1 loop.
        # Demand check → supply check → Jita price → target price → item metadata
        # are all resolved in one round-trip per hub instead of ~7 per type.
        candidates = await database.get_arbitrage_candidates(
            hub_region_id=hub.region_id,
            hub_station_id=hub.station_id,
            supply_station_id=SUPPLY_HUB.station_id,
            min_daily_volume=MIN_DAILY_VOLUME,
            max_days_supply=MAX_DAYS_SUPPLY,
            days=DEMAND_WINDOW_DAYS,
        )

        if not candidates:
            log.debug("[arbitrage] No candidates for %s", hub.name)
            continue

        log.debug("[arbitrage] %s: %d candidates returned", hub.name, len(candidates))

        hub_opps = 0
        for row in candidates:
            live_price = row["live_target_price"]
            hist_avg   = row["hist_avg_price"]

            # Price sanity check: if live order is far above historical norm,
            # substitute hist avg to avoid scam/stale listings skewing margin.
            if live_price is None:
                target_price = hist_avg
            elif hist_avg and live_price > hist_avg * PRICE_SANITY_MULTIPLIER:
                log.debug(
                    "[arbitrage] %s @ %s: sell %.2f is %.1f× hist avg %.2f — using hist avg",
                    row["type_id"], hub.name, live_price, live_price / hist_avg, hist_avg,
                )
                target_price = hist_avg
            else:
                target_price = live_price

            if not target_price:
                continue

            type_name = row["type_name"] or f"Type {row['type_id']}"

            opp = _calc_opportunity(
                type_id=row["type_id"],
                type_name=type_name,
                packaged_volume=row["packaged_volume"],
                avg_daily_volume=row["avg_daily"],
                current_supply=row["current_supply"],
                jita_price=row["jita_price"],
                target_sell_price=target_price,
                hist_avg_price=hist_avg,
                target_hub=hub,
            )

            if opp:
                await database.insert_opportunity(opp)
                hub_opps += 1
                log.info(
                    "[arbitrage] OPPORTUNITY | %s @ %s | avg vol %.0f/day | "
                    "supply %d units | Jita %.2f → %s %.2f | margin %.1f%%",
                    type_name, hub.name, row["avg_daily"], row["current_supply"],
                    row["jita_price"], hub.name, target_price, opp["margin_pct"],
                )

        log.info("[arbitrage] %s: %d opportunities found", hub.name, hub_opps)
        total_found += hub_opps

    log.info("[arbitrage] Pass complete — %d total opportunities", total_found)
