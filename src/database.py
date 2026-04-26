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
    """Average daily volume over the last N days."""
    row = await pool().fetchrow(
        """
        SELECT AVG(volume)::float AS avg_vol
        FROM market_history
        WHERE region_id = $1
          AND type_id   = $2
          AND date      >= CURRENT_DATE - ($3 || ' days')::interval
        """,
        region_id, type_id, str(days),
    )
    return row["avg_vol"] if row else None


async def get_active_types_for_region(
    region_id: int, days: int = 7, min_volume: float = 10
) -> list[int]:
    """Type IDs with meaningful recent volume in a region."""
    rows = await pool().fetch(
        """
        SELECT type_id
        FROM market_history
        WHERE region_id = $1
          AND date >= CURRENT_DATE - ($2 || ' days')::interval
        GROUP BY type_id
        HAVING AVG(volume) >= $3
        """,
        region_id, str(days), min_volume,
    )
    return [r["type_id"] for r in rows]


# ---------------------------------------------------------------------------
# market_orders
# ---------------------------------------------------------------------------

async def upsert_orders_batch(rows: list[dict]) -> int:
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
    await pool().executemany(
        """
        INSERT INTO market_orders
            (order_id, region_id, type_id, location_id, is_buy_order,
             price, volume_remain, volume_total, min_volume, range,
             issued, duration, captured_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        ON CONFLICT (order_id, captured_at) DO NOTHING
        """,
        records,
    )
    return len(records)


async def get_sell_supply_at_station(
    station_id: int, type_id: int, max_age_minutes: int = 10
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
    station_id: int, type_id: int, max_age_minutes: int = 10
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


async def prune_old_orders() -> None:
    await pool().execute("SELECT prune_old_orders()")
    log.info("Pruned stale order data")


# ---------------------------------------------------------------------------
# opportunities
# ---------------------------------------------------------------------------

async def deactivate_old_opportunities() -> None:
    await pool().execute(
        "UPDATE opportunities SET active = FALSE WHERE detected_at < NOW() - INTERVAL '1 hour'"
    )


async def insert_opportunity(opp: dict[str, Any]) -> None:
    await pool().execute(
        """
        INSERT INTO opportunities
            (type_id, type_name, target_station_id, target_hub_name,
             supply_station_id, avg_daily_volume, current_supply_units,
             shortage_ratio, jita_sell_price, target_sell_price,
             shipping_cost, total_cost, expected_net_revenue, margin_pct)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
        """,
        opp["type_id"], opp["type_name"], opp["target_station_id"],
        opp["target_hub_name"], opp["supply_station_id"],
        opp["avg_daily_volume"], opp["current_supply_units"],
        opp["shortage_ratio"], opp["jita_sell_price"], opp["target_sell_price"],
        opp["shipping_cost"], opp["total_cost"],
        opp["expected_net_revenue"], opp["margin_pct"],
    )


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
