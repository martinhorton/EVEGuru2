from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import asyncpg
import os
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

log = logging.getLogger("uvicorn")

_pool: asyncpg.Pool | None = None

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
    limit: int = Query(1000, le=5000),
    offset: int = Query(0),
):
    rows = await _pool.fetch("""
        SELECT
            id,
            type_id,
            type_name,
            target_station_id,
            target_hub_name,
            avg_daily_volume::float,
            current_supply_units,
            shortage_ratio::float,
            jita_sell_price::float,
            target_sell_price::float,
            shipping_cost::float,
            total_cost::float,
            expected_net_revenue::float,
            margin_pct::float,
            ((expected_net_revenue - total_cost) * avg_daily_volume)::float  AS estimated_daily_profit,
            detected_at
        FROM opportunities
        WHERE active = TRUE
          AND ($1::text IS NULL OR target_hub_name ILIKE '%' || $1 || '%')
          AND margin_pct >= $2
          AND detected_at >= NOW() - INTERVAL '2 hours'
        ORDER BY margin_pct DESC
        LIMIT $3 OFFSET $4
    """, hub, min_margin, limit, offset)
    return [_row(r) for r in rows]


@app.get("/api/items/{type_id}")
async def item_info(type_id: int):
    row = await _pool.fetchrow(
        "SELECT type_id, name, packaged_volume::float FROM item_types WHERE type_id = $1",
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
