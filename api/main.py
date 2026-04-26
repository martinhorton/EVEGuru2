from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

import asyncpg
import os
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

log = logging.getLogger("uvicorn")

_pool: asyncpg.Pool | None = None

# Fee config — mirrors agent .env values so the UI stays consistent
PRICE_SANITY_MULTIPLIER = float(os.getenv("PRICE_SANITY_MULTIPLIER", "5.0"))
_BROKER_FEE_PCT = float(os.getenv("BROKER_FEE_PCT", "3.0"))
_SALES_TAX_PCT  = float(os.getenv("SALES_TAX_PCT",  "3.6"))
SELL_OVERHEAD_PCT = (_BROKER_FEE_PCT + _SALES_TAX_PCT) / 100.0

# Station IDs for the five major hubs
HUB_STATIONS = (60003760, 60008494, 60011866, 60004588, 60005686)


def _row(record) -> dict:
    out = {}
    for k, v in dict(record).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=2, max_size=8, command_timeout=30
    )
    yield
    await _pool.close()


app = FastAPI(title="EVEGuru2 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config")
async def config():
    """Client-facing config so the UI uses the same fee rates as the agent."""
    return {"sell_overhead_pct": SELL_OVERHEAD_PCT}


@app.get("/api/stats")
async def stats():
    row = await _pool.fetchrow("""
        SELECT
            COUNT(*)                                                                    AS active_count,
            COALESCE(MAX(margin_pct), 0)::float                                        AS best_margin,
            COALESCE(SUM((expected_net_revenue - total_cost) * avg_daily_volume), 0)::float
                                                                                        AS total_daily_profit,
            COUNT(DISTINCT target_station_id)                                          AS hub_count,
            MAX(detected_at)                                                           AS last_scan
        FROM opportunities
        WHERE active = TRUE
          AND detected_at >= NOW() - INTERVAL '2 hours'
    """)
    return _row(row)


@app.get("/api/hubs")
async def hubs():
    rows = await _pool.fetch(
        "SELECT * FROM hubs WHERE active = TRUE ORDER BY is_supply DESC, name"
    )
    return [_row(r) for r in rows]


@app.get("/api/opportunities")
async def opportunities(
    hub: Optional[str] = Query(None),
    min_margin: float = Query(0.0),
    category_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    limit: int = Query(1000, le=5000),
    offset: int = Query(0),
):
    rows = await _pool.fetch("""
        SELECT
            o.id,
            o.type_id,
            o.type_name,
            o.target_station_id,
            o.target_hub_name,
            o.avg_daily_volume::float,
            o.current_supply_units,
            o.shortage_ratio::float,
            o.jita_sell_price::float,
            o.target_sell_price::float,
            o.shipping_cost::float,
            o.total_cost::float,
            o.expected_net_revenue::float,
            o.margin_pct::float,
            o.hist_avg_price::float,
            (o.expected_net_revenue - o.total_cost)::float                         AS profit_per_unit,
            ((o.expected_net_revenue - o.total_cost) * o.avg_daily_volume)::float  AS estimated_daily_profit,
            CASE WHEN it.packaged_volume > 0
                 THEN ((o.expected_net_revenue - o.total_cost) / it.packaged_volume)::float
                 ELSE NULL END                                                      AS profit_per_m3,
            o.detected_at,
            it.group_id,
            it.group_name,
            it.category_id,
            it.category_name,
            it.packaged_volume::float
        FROM opportunities o
        LEFT JOIN item_types it ON it.type_id = o.type_id
        WHERE o.active = TRUE
          AND ($1::text IS NULL OR o.target_hub_name ILIKE '%' || $1 || '%')
          AND o.margin_pct >= $2
          AND o.detected_at >= NOW() - INTERVAL '2 hours'
          AND ($3::integer IS NULL OR it.category_id = $3)
          AND ($4::integer IS NULL OR it.group_id    = $4)
        ORDER BY o.margin_pct DESC
        LIMIT $5 OFFSET $6
    """, hub, min_margin, category_id, group_id, limit, offset)
    return [_row(r) for r in rows]


@app.get("/api/categories")
async def categories():
    """All item categories that appear in item_types (populated by SDE loader)."""
    rows = await _pool.fetch("""
        SELECT DISTINCT category_id, category_name
        FROM item_types
        WHERE category_id IS NOT NULL
        ORDER BY category_name
    """)
    return [_row(r) for r in rows]


@app.get("/api/categories/{category_id}/groups")
async def groups_for_category(category_id: int):
    rows = await _pool.fetch("""
        SELECT DISTINCT group_id, group_name
        FROM item_types
        WHERE category_id = $1
          AND group_id IS NOT NULL
        ORDER BY group_name
    """, category_id)
    return [_row(r) for r in rows]


@app.get("/api/items/{type_id}")
async def item_info(type_id: int):
    row = await _pool.fetchrow(
        """SELECT type_id, name, packaged_volume::float,
                  group_id, group_name, category_id, category_name, market_group_id
           FROM item_types WHERE type_id = $1""",
        type_id,
    )
    if not row:
        raise HTTPException(404, "Item not found")
    return _row(row)


@app.get("/api/items/{type_id}/history")
async def item_history(type_id: int, days: int = Query(30, le=90)):
    rows = await _pool.fetch("""
        SELECT
            region_id,
            date::text  AS date,
            average::float,
            highest::float,
            lowest::float,
            volume
        FROM market_history
        WHERE type_id = $1
          AND date >= CURRENT_DATE - ($2 || ' days')::interval
        ORDER BY region_id, date ASC
    """, type_id, str(days))
    return [_row(r) for r in rows]


@app.get("/api/items/{type_id}/orders")
async def item_orders(type_id: int):
    rows = await _pool.fetch("""
        SELECT DISTINCT ON (location_id, is_buy_order, price)
            location_id,
            is_buy_order,
            price::float,
            volume_remain,
            min_volume
        FROM market_orders
        WHERE type_id     = $1
          AND location_id = ANY($2::bigint[])
          AND captured_at >= NOW() - INTERVAL '15 minutes'
        ORDER BY location_id, is_buy_order,
                 CASE WHEN is_buy_order THEN -price ELSE price END
        LIMIT 200
    """, type_id, list(HUB_STATIONS))
    return [_row(r) for r in rows]


# ── Industry endpoints ─────────────────────────────────────────────────────────

# Hub metadata: station_id, name, region_id
_HUB_META = [
    {"station_id": 60003760, "name": "Jita",    "short": "Jita",    "region_id": 10000002},
    {"station_id": 60008494, "name": "Amarr",   "short": "Amarr",   "region_id": 10000043},
    {"station_id": 60011866, "name": "Dodixie", "short": "Dodixie", "region_id": 10000032},
    {"station_id": 60004588, "name": "Rens",    "short": "Rens",    "region_id": 10000030},
    {"station_id": 60005686, "name": "Hek",     "short": "Hek",     "region_id": 10000042},
]


_SDE_NOT_LOADED = HTTPException(
    503,
    detail="Blueprint data not loaded. Run: docker compose run --rm sde",
)


@app.get("/api/industry/search")
async def industry_search(q: str = Query(..., min_length=2)):
    """Search blueprints by product name or blueprint name."""
    try:
        rows = await _pool.fetch("""
            SELECT
                b.blueprint_type_id,
                b.product_type_id,
                b.product_qty,
                b.base_time_seconds,
                bp_it.name   AS blueprint_name,
                prod_it.name AS product_name,
                prod_it.group_name,
                prod_it.category_name,
                prod_it.packaged_volume::float AS product_volume
            FROM blueprints b
            JOIN item_types bp_it   ON bp_it.type_id   = b.blueprint_type_id
            JOIN item_types prod_it ON prod_it.type_id = b.product_type_id
            WHERE prod_it.name ILIKE '%' || $1 || '%'
               OR bp_it.name  ILIKE '%' || $1 || '%'
            ORDER BY prod_it.name
            LIMIT 60
        """, q)
    except Exception as exc:
        if "does not exist" in str(exc):
            raise _SDE_NOT_LOADED
        raise
    return [_row(r) for r in rows]


@app.get("/api/industry/blueprint/{bp_type_id}")
async def blueprint_detail(bp_type_id: int):
    """Full blueprint info: product + all manufacturing materials."""
    bp = await _pool.fetchrow("""
        SELECT
            b.blueprint_type_id,
            b.product_type_id,
            b.product_qty,
            b.base_time_seconds,
            bp_it.name   AS blueprint_name,
            prod_it.name AS product_name,
            prod_it.packaged_volume::float AS product_volume,
            prod_it.group_name,
            prod_it.category_name
        FROM blueprints b
        JOIN item_types bp_it   ON bp_it.type_id   = b.blueprint_type_id
        JOIN item_types prod_it ON prod_it.type_id = b.product_type_id
        WHERE b.blueprint_type_id = $1
    """, bp_type_id)
    if not bp:
        raise HTTPException(404, "Blueprint not found")

    materials = await _pool.fetch("""
        SELECT
            bm.material_type_id,
            bm.quantity AS base_quantity,
            it.name,
            COALESCE(it.packaged_volume, 1.0)::float AS packaged_volume,
            it.group_name,
            it.category_name
        FROM blueprint_materials bm
        JOIN item_types it ON it.type_id = bm.material_type_id
        WHERE bm.blueprint_type_id = $1
        ORDER BY it.name
    """, bp_type_id)

    return {
        **_row(bp),
        "materials": [_row(m) for m in materials],
    }


@app.get("/api/industry/prices")
async def material_prices(type_ids: str = Query(...)):
    """
    Current Jita sell prices for a comma-separated list of type_ids.
    Falls back to 30-day average from market_history if no live order.
    Returns {type_id: price} map.
    """
    try:
        ids = [int(x) for x in type_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Invalid type_ids")
    if not ids:
        return {}

    JITA_STATION = 60003760
    JITA_REGION  = 10000002

    # Live Jita sell orders (last 15 min)
    order_rows = await _pool.fetch("""
        SELECT type_id, MIN(price)::float AS price
        FROM market_orders
        WHERE type_id     = ANY($1::integer[])
          AND location_id = $2
          AND is_buy_order = FALSE
          AND captured_at >= NOW() - INTERVAL '15 minutes'
        GROUP BY type_id
    """, ids, JITA_STATION)
    prices = {r["type_id"]: r["price"] for r in order_rows}

    # Fall back to 30-day history average for any missing
    missing = [i for i in ids if i not in prices]
    if missing:
        hist_rows = await _pool.fetch("""
            SELECT type_id, AVG(average)::float AS price
            FROM market_history
            WHERE type_id   = ANY($1::integer[])
              AND region_id = $2
              AND date      >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY type_id
        """, missing, JITA_REGION)
        for r in hist_rows:
            prices[r["type_id"]] = r["price"]

    return {str(k): v for k, v in prices.items()}


@app.get("/api/industry/hub-prices/{type_id}")
async def hub_sell_prices(type_id: int):
    """
    Cheapest current sell price at each hub for a given product type_id.
    Applies the same price-sanity check as the arbitrage agent: if the live
    order is more than PRICE_SANITY_MULTIPLIER × the 7-day regional average,
    the historical average is used instead (filters scam/stale listings).
    Falls back to the 7-day average when no live orders exist.
    """
    all_station_ids = [h["station_id"] for h in _HUB_META]
    all_region_ids  = [h["region_id"]  for h in _HUB_META]

    # Live cheapest sell at each hub
    order_rows = await _pool.fetch("""
        SELECT location_id, MIN(price)::float AS sell_price
        FROM market_orders
        WHERE type_id      = $1
          AND location_id  = ANY($2::bigint[])
          AND is_buy_order = FALSE
          AND captured_at >= NOW() - INTERVAL '15 minutes'
        GROUP BY location_id
    """, type_id, all_station_ids)
    live_prices = {r["location_id"]: r["sell_price"] for r in order_rows}

    # 7-day regional averages for all hubs (sanity check + fallback)
    hist_rows = await _pool.fetch("""
        SELECT region_id, AVG(average)::float AS price
        FROM market_history
        WHERE type_id   = $1
          AND region_id = ANY($2::integer[])
          AND date      >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY region_id
    """, type_id, all_region_ids)
    region_avg = {r["region_id"]: r["price"] for r in hist_rows}

    result = []
    for h in _HUB_META:
        live  = live_prices.get(h["station_id"])
        avg   = region_avg.get(h["region_id"])

        if live is None:
            sell_price = avg  # no live orders — use history
        elif avg and live > avg * PRICE_SANITY_MULTIPLIER:
            sell_price = avg  # scam/stale listing — use history
        else:
            sell_price = live

        result.append({
            "station_id": h["station_id"],
            "hub_name":   h["short"],
            "sell_price": sell_price,
        })

    return result
