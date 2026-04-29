"""
Database access layer — asyncpg connection pool with typed query helpers.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any

import asyncpg

from . import config

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=60,
    )
    log.info("Database pool initialised")


async def close_pool() -> None:
    if _pool:
        await _pool.close()


def pool() -> asyncpg.Pool:
    assert _pool is not None, "DB pool not initialised"
    return _pool


# ---------------------------------------------------------------------------
# item_types
# ---------------------------------------------------------------------------

async def upsert_type(
    type_id: int,
    name: str,
    packaged_volume: float,
    group_id: int | None = None,
    group_name: str | None = None,
    category_id: int | None = None,
    category_name: str | None = None,
    market_group_id: int | None = None,
) -> None:
    await pool().execute(
        """
        INSERT INTO item_types
            (type_id, name, packaged_volume,
             group_id, group_name, category_id, category_name, market_group_id,
             last_updated)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
        ON CONFLICT (type_id) DO UPDATE
            SET name             = EXCLUDED.name,
                packaged_volume  = EXCLUDED.packaged_volume,
                group_id         = COALESCE(EXCLUDED.group_id,        item_types.group_id),
                group_name       = COALESCE(EXCLUDED.group_name,       item_types.group_name),
                category_id      = COALESCE(EXCLUDED.category_id,      item_types.category_id),
                category_name    = COALESCE(EXCLUDED.category_name,    item_types.category_name),
                market_group_id  = COALESCE(EXCLUDED.market_group_id,  item_types.market_group_id),
                last_updated     = NOW()
        """,
        type_id, name, packaged_volume,
        group_id, group_name, category_id, category_name, market_group_id,
    )


async def get_type_name(type_id: int) -> str | None:
    row = await pool().fetchrow(
        "SELECT name FROM item_types WHERE type_id = $1", type_id
    )
    return row["name"] if row else None


async def get_type_volume(type_id: int) -> float | None:
    row = await pool().fetchrow(
        "SELECT packaged_volume FROM item_types WHERE type_id = $1", type_id
    )
    return float(row["packaged_volume"]) if row and row["packaged_volume"] else None


async def get_categories() -> list[dict]:
    """All distinct categories that have at least one item_type in the DB."""
    rows = await pool().fetch(
        """
        SELECT DISTINCT category_id, category_name
        FROM item_types
        WHERE category_id IS NOT NULL
        ORDER BY category_name
        """
    )
    return [{"category_id": r["category_id"], "category_name": r["category_name"]} for r in rows]


async def get_groups_for_category(category_id: int) -> list[dict]:
    rows = await pool().fetch(
        """
        SELECT DISTINCT group_id, group_name
        FROM item_types
        WHERE category_id = $1
          AND group_id IS NOT NULL
        ORDER BY group_name
        """,
        category_id,
    )
    return [{"group_id": r["group_id"], "group_name": r["group_name"]} for r in rows]


# ---------------------------------------------------------------------------
# market_history
# ---------------------------------------------------------------------------

async def upsert_history_batch(rows: list[dict]) -> int:
    """Bulk upsert history rows. Each dict: region_id, type_id, date, average,
    highest, lowest, order_count, volume."""
    if not rows:
        return 0
    records = [
        (
            r["region_id"], r["type_id"], r["date"],
            r.get("average"), r.get("highest"), r.get("lowest"),
            r.get("order_count"), r.get("volume"),
        )
        for r in rows
    ]
    await pool().executemany(
        """
        INSERT INTO market_history
            (region_id, type_id, date, average, highest, lowest, order_count, volume)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (region_id, type_id, date) DO UPDATE
            SET average=EXCLUDED.average, highest=EXCLUDED.highest,
                lowest=EXCLUDED.lowest, order_count=EXCLUDED.order_count,
                volume=EXCLUDED.volume
        """,
        records,
    )
    return len(records)


async def get_avg_daily_volume(
    region_id: int, type_id: int, days: int = 7
) -> float | None:
    """
    Average daily volume over the last N calendar days.

    Uses SUM/days rather than AVG(volume) so that days with zero trades
    are included in the denominator — otherwise items that only trade on
    some days get an inflated average.
    """
    row = await pool().fetchrow(
        """
        SELECT (COALESCE(SUM(volume), 0)::float / $3) AS avg_vol
        FROM market_history
        WHERE region_id = $1
          AND type_id   = $2
          AND date      >= CURRENT_DATE - ($3 * INTERVAL '1 day')
        """,
        region_id, type_id, days,
    )
    v = row["avg_vol"] if row else None
    return float(v) if v else None


async def get_active_types_for_region(
    region_id: int, days: int = 7, min_volume: float = 10
) -> list[int]:
    """Type IDs with meaningful recent volume in a region.

    Uses the same SUM(volume)/days calendar-day average as get_avg_daily_volume
    so the pre-filter and per-type check are consistent.
    """
    rows = await pool().fetch(
        """
        SELECT type_id
        FROM market_history
        WHERE region_id = $1
          AND date >= CURRENT_DATE - ($2 * INTERVAL '1 day')
        GROUP BY type_id
        HAVING (SUM(volume)::float / $2) >= $3
        """,
        region_id, days, min_volume,
    )
    return [r["type_id"] for r in rows]


# ---------------------------------------------------------------------------
# market_orders
# ---------------------------------------------------------------------------

_ORDER_INSERT_SQL = """
    INSERT INTO market_orders
        (order_id, region_id, type_id, location_id, is_buy_order,
         price, volume_remain, volume_total, min_volume, range,
         issued, duration, captured_at)
    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
    ON CONFLICT (order_id, captured_at) DO NOTHING
"""
_ORDER_BATCH_SIZE = 5_000  # rows per executemany call (~1-2s each, well within timeout)


async def upsert_orders_batch(rows: list[dict]) -> int:
    """Insert market orders in chunks to avoid command_timeout on large regions (e.g. Jita)."""
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    records = [
        (
            r["order_id"], r["region_id"], r["type_id"], r["location_id"],
            r["is_buy_order"], r["price"], r["volume_remain"], r["volume_total"],
            r.get("min_volume", 1), r.get("range"), r["issued"], r["duration"], now,
        )
        for r in rows
    ]
    for i in range(0, len(records), _ORDER_BATCH_SIZE):
        chunk = records[i : i + _ORDER_BATCH_SIZE]
        await pool().executemany(_ORDER_INSERT_SQL, chunk)
    return len(records)


async def get_sell_supply_at_station(
    station_id: int, type_id: int, max_age_minutes: int = 65
) -> int:
    """Total units for sale at a specific station (most recent scan)."""
    row = await pool().fetchrow(
        """
        SELECT COALESCE(SUM(volume_remain), 0)::bigint AS supply
        FROM market_orders
        WHERE location_id   = $1
          AND type_id       = $2
          AND is_buy_order  = FALSE
          AND captured_at  >= NOW() - ($3 || ' minutes')::interval
        """,
        station_id, type_id, str(max_age_minutes),
    )
    return int(row["supply"]) if row else 0


async def get_cheapest_sell_at_station(
    station_id: int, type_id: int, max_age_minutes: int = 65
) -> float | None:
    """Lowest sell price at a specific station."""
    row = await pool().fetchrow(
        """
        SELECT MIN(price)::float AS min_price
        FROM market_orders
        WHERE location_id   = $1
          AND type_id       = $2
          AND is_buy_order  = FALSE
          AND captured_at  >= NOW() - ($3 || ' minutes')::interval
        """,
        station_id, type_id, str(max_age_minutes),
    )
    v = row["min_price"] if row else None
    return v


async def get_realistic_buy_price_at_station(
    station_id: int, type_id: int, min_quantity: float, max_age_minutes: int = 65
) -> float | None:
    """
    Cheapest price at which at least `min_quantity` cumulative units are available.

    Works through sell orders cheapest-first and returns the price of the tier
    where cumulative supply first meets `min_quantity`.  This avoids single-unit
    lowball/scam orders at Jita distorting the source cost.

    Falls back to the absolute cheapest order if total supply < min_quantity
    (i.e. there simply isn't enough stock — use whatever is there).
    """
    row = await pool().fetchrow(
        """
        WITH ranked AS (
            SELECT price,
                   SUM(volume_remain) OVER (
                       ORDER BY price
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS cum_supply
            FROM market_orders
            WHERE location_id  = $1
              AND type_id      = $2
              AND is_buy_order = FALSE
              AND captured_at >= NOW() - ($3 || ' minutes')::interval
        )
        SELECT price FROM ranked
        WHERE cum_supply >= $4
        ORDER BY price
        LIMIT 1
        """,
        station_id, type_id, str(max_age_minutes), min_quantity,
    )
    if row:
        return float(row["price"])
    # Total supply is less than min_quantity — fall back to cheapest available
    return await get_cheapest_sell_at_station(station_id, type_id, max_age_minutes)


async def get_avg_market_price(
    region_id: int, type_id: int, days: int = 7
) -> float | None:
    """7-day average transaction price for a type in a region (from market_history)."""
    row = await pool().fetchrow(
        """
        SELECT AVG(average)::float AS avg_price
        FROM market_history
        WHERE region_id = $1
          AND type_id   = $2
          AND date     >= CURRENT_DATE - ($3 || ' days')::interval
        """,
        region_id, type_id, str(days),
    )
    v = row["avg_price"] if row else None
    return float(v) if v else None


async def prune_old_orders() -> None:
    # Keep only 30 minutes of order snapshots — enough for the 20-minute
    # freshness window plus one full scan cycle of buffer.
    # We bypass the init.sql stored procedure (which used 25 hours) and
    # delete directly so the retention can be changed without a DB migration.
    result = await pool().execute(
        "DELETE FROM market_orders WHERE captured_at < NOW() - INTERVAL '30 minutes'"
    )
    log.info("Pruned stale order data (%s)", result)


# ---------------------------------------------------------------------------
# opportunities
# ---------------------------------------------------------------------------

async def deactivate_old_opportunities() -> None:
    await pool().execute(
        "UPDATE opportunities SET active = FALSE WHERE detected_at < NOW() - INTERVAL '1 hour'"
    )


async def upsert_opportunity(opp: dict[str, Any]) -> None:
    """Insert or refresh an active opportunity.

    A partial unique index on (type_id, target_station_id) WHERE active=TRUE
    means each item/hub pair has exactly one live row.  Subsequent scans update
    the prices, supply, and margin rather than creating a duplicate.
    """
    await pool().execute(
        """
        INSERT INTO opportunities
            (type_id, type_name, target_station_id, target_hub_name,
             supply_station_id, avg_daily_volume, current_supply_units,
             shortage_ratio, jita_sell_price, target_sell_price,
             hist_avg_price, shipping_cost, total_cost,
             expected_net_revenue, margin_pct,
             detected_at, active)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15, NOW(), TRUE)
        ON CONFLICT (type_id, target_station_id)
        WHERE active = TRUE
        DO UPDATE SET
            type_name            = EXCLUDED.type_name,
            avg_daily_volume     = EXCLUDED.avg_daily_volume,
            current_supply_units = EXCLUDED.current_supply_units,
            shortage_ratio       = EXCLUDED.shortage_ratio,
            jita_sell_price      = EXCLUDED.jita_sell_price,
            target_sell_price    = EXCLUDED.target_sell_price,
            hist_avg_price       = EXCLUDED.hist_avg_price,
            shipping_cost        = EXCLUDED.shipping_cost,
            total_cost           = EXCLUDED.total_cost,
            expected_net_revenue = EXCLUDED.expected_net_revenue,
            margin_pct           = EXCLUDED.margin_pct,
            detected_at          = NOW()
        """,
        opp["type_id"], opp["type_name"], opp["target_station_id"],
        opp["target_hub_name"], opp["supply_station_id"],
        opp["avg_daily_volume"], opp["current_supply_units"],
        opp["shortage_ratio"], opp["jita_sell_price"], opp["target_sell_price"],
        opp.get("hist_avg_price"), opp["shipping_cost"], opp["total_cost"],
        opp["expected_net_revenue"], opp["margin_pct"],
    )


# Keep old name as alias so nothing else breaks
insert_opportunity = upsert_opportunity


async def get_arbitrage_candidates(
    hub_region_id: int,
    hub_station_id: int,
    supply_region_id: int,
    min_daily_volume: float = 1.0,
    max_days_supply: float = 60.0,
    days: int = 7,
    max_age_minutes: int = 65,
) -> list[dict]:
    """
    Single bulk query replacing the per-type N+1 loop in the arbitrage agent.

    Uses supply_region_id (e.g. The Forge = 10000002) rather than a single
    station ID so that items listed at Perimeter citadels and other non-NPC
    locations in the supply region are included in the source price.

    Returns one row per candidate type containing everything the agent needs
    to call _calc_opportunity — avg_daily, current_supply, jita_price,
    live_target_price, hist_avg_price, packaged_volume, type_name.

    The price-sanity check (live vs hist avg) is intentionally left to the
    caller so it stays in one place with the rest of the business logic.
    """
    rows = await pool().fetch(
        """
        WITH
        -- 1. Types with sufficient demand in this hub's region
        demand AS (
            SELECT type_id,
                   SUM(volume)::float / $5 AS avg_daily
            FROM   market_history
            WHERE  region_id = $1
              AND  date >= CURRENT_DATE - ($5 * INTERVAL '1 day')
            GROUP  BY type_id
            HAVING SUM(volume)::float / $5 >= $6
        ),
        -- 2. Current sell supply at the target hub station
        hub_supply AS (
            SELECT type_id,
                   SUM(volume_remain)::bigint AS supply
            FROM   market_orders
            WHERE  location_id = $2
              AND  is_buy_order = FALSE
              AND  captured_at >= NOW() - ($7 || ' minutes')::interval
            GROUP  BY type_id
        ),
        -- 3. Keep only types below the days-of-supply threshold
        undersupplied AS (
            SELECT d.type_id,
                   d.avg_daily,
                   COALESCE(s.supply, 0) AS supply
            FROM   demand d
            LEFT   JOIN hub_supply s ON s.type_id = d.type_id
            WHERE  COALESCE(s.supply, 0) = 0
               OR  COALESCE(s.supply, 0)::float / NULLIF(d.avg_daily, 0) <= $4
        ),
        -- 4. Cumulative supply across the whole supply REGION (cheapest-first).
        --    Using region_id rather than a single station_id means citadel orders
        --    in Perimeter etc. are included — many T2/faction items are only
        --    listed there, not at the Jita NPC station.
        supply_cum AS (
            SELECT mo.type_id,
                   mo.price,
                   SUM(mo.volume_remain) OVER (
                       PARTITION BY mo.type_id
                       ORDER BY mo.price
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS cum_supply
            FROM   market_orders mo
            WHERE  mo.region_id = $3
              AND  mo.is_buy_order = FALSE
              AND  mo.captured_at >= NOW() - ($7 || ' minutes')::interval
              AND  mo.type_id IN (SELECT type_id FROM undersupplied)
        ),
        -- 5. Realistic supply price: first tier where cumulative supply >= avg_daily
        jita_price AS (
            SELECT DISTINCT ON (jc.type_id)
                   jc.type_id,
                   jc.price::float AS jita_price
            FROM   supply_cum jc
            JOIN   undersupplied u ON u.type_id = jc.type_id
            WHERE  jc.cum_supply >= u.avg_daily
            ORDER  BY jc.type_id, jc.price ASC
        ),
        -- 6. Fallback: total region supply < avg_daily — use cheapest available
        jita_fallback AS (
            SELECT type_id,
                   MIN(price)::float AS jita_price
            FROM   market_orders
            WHERE  region_id = $3
              AND  is_buy_order = FALSE
              AND  captured_at >= NOW() - ($7 || ' minutes')::interval
              AND  type_id IN (
                       SELECT u.type_id FROM undersupplied u
                       WHERE  u.type_id NOT IN (SELECT type_id FROM jita_price)
                   )
            GROUP  BY type_id
        ),
        jita AS (
            SELECT * FROM jita_price
            UNION ALL
            SELECT * FROM jita_fallback
        ),
        -- 7. Cheapest live sell at target hub (returned separately for sanity check)
        target_sell AS (
            SELECT type_id,
                   MIN(price)::float AS live_price
            FROM   market_orders
            WHERE  location_id = $2
              AND  is_buy_order = FALSE
              AND  captured_at >= NOW() - ($7 || ' minutes')::interval
              AND  type_id IN (SELECT type_id FROM jita)
            GROUP  BY type_id
        ),
        -- 8. Historical average at target hub (sanity check + fallback)
        hist AS (
            SELECT type_id,
                   AVG(average)::float AS avg_price
            FROM   market_history
            WHERE  region_id = $1
              AND  date >= CURRENT_DATE - ($5 * INTERVAL '1 day')
              AND  type_id IN (SELECT type_id FROM jita)
            GROUP  BY type_id
        )
        SELECT
            u.type_id,
            it.name                                     AS type_name,
            COALESCE(it.packaged_volume, 1.0)::float    AS packaged_volume,
            u.avg_daily,
            u.supply                                    AS current_supply,
            j.jita_price,
            ts.live_price                               AS live_target_price,
            h.avg_price                                 AS hist_avg_price
        FROM   undersupplied u
        JOIN   jita          j  ON j.type_id  = u.type_id
        LEFT   JOIN target_sell ts ON ts.type_id = u.type_id
        LEFT   JOIN hist        h  ON h.type_id  = u.type_id
        LEFT   JOIN item_types  it ON it.type_id = u.type_id
        WHERE  ts.live_price IS NOT NULL OR h.avg_price IS NOT NULL
        """,
        hub_region_id, hub_station_id, supply_station_id,
        max_days_supply, days, min_daily_volume, str(max_age_minutes),
    )
    return [dict(r) for r in rows]


async def get_recent_opportunities(limit: int = 50) -> list[asyncpg.Record]:
    return await pool().fetch(
        """
        SELECT * FROM opportunities
        WHERE active = TRUE
        ORDER BY margin_pct DESC, detected_at DESC
        LIMIT $1
        """,
        limit,
    )
