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


app = FastAPI(
    title="EVEGuru2 API",
    description="Market arbitrage scanner for EVE Online. All endpoints are read-only.",
    version="2.0",
    lifespan=lifespan,
)

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


# ── Verification / diagnostic endpoints ───────────────────────────────────────

_HUB_LOOKUP = {
    "jita":    {"station_id": 60003760, "region_id": 10000002, "name": "Jita"},
    "amarr":   {"station_id": 60008494, "region_id": 10000043, "name": "Amarr"},
    "dodixie": {"station_id": 60011866, "region_id": 10000032, "name": "Dodixie"},
    "rens":    {"station_id": 60004588, "region_id": 10000030, "name": "Rens"},
    "hek":     {"station_id": 60005686, "region_id": 10000042, "name": "Hek"},
}
_DEMAND_DAYS    = 7
_MAX_AGE_MIN    = 20
_MIN_DAILY_VOL  = float(os.getenv("MIN_DAILY_VOLUME",    "1.0"))
_MAX_DAYS_SUPPLY = float(os.getenv("MAX_DAYS_SUPPLY",   "60.0"))
_MIN_MARGIN_PCT = float(os.getenv("MIN_MARGIN_PCT",     "10.0"))
_MIN_PROFIT_ISK = float(os.getenv("MIN_PROFIT_ISK",    "500000"))
_SHIPPING_PER_M3 = float(os.getenv("SHIPPING_COST_PER_M3", "1000"))
_SANITY_MULT    = PRICE_SANITY_MULTIPLIER
_OVERHEAD       = SELL_OVERHEAD_PCT
JITA_STATION    = 60003760


@app.get(
    "/api/opportunities/search",
    summary="Search opportunities by item name",
    tags=["Verification"],
)
async def search_opportunities(
    q:   str           = Query(..., min_length=2, description="Partial item name"),
    hub: Optional[str] = Query(None, description="Hub name filter (e.g. 'Rens')"),
):
    """Return all active opportunities whose item name matches *q* (case-insensitive)."""
    rows = await _pool.fetch(
        """
        SELECT
            o.type_id,
            o.type_name,
            o.target_hub_name,
            o.margin_pct::float,
            o.jita_sell_price::float,
            o.target_sell_price::float,
            o.avg_daily_volume::float,
            o.current_supply_units,
            o.shortage_ratio::float,
            (o.expected_net_revenue - o.total_cost)::float AS profit_per_unit,
            o.shipping_cost::float,
            o.total_cost::float,
            o.detected_at,
            o.active
        FROM opportunities o
        WHERE o.active = TRUE
          AND o.type_name ILIKE '%' || $1 || '%'
          AND ($2::text IS NULL OR o.target_hub_name ILIKE '%' || $2 || '%')
          AND o.detected_at >= NOW() - INTERVAL '2 hours'
        ORDER BY o.margin_pct DESC
        LIMIT 200
        """,
        q, hub,
    )
    return [_row(r) for r in rows]


@app.get(
    "/api/diagnostics/item",
    summary="Full pipeline trace for a named item at a hub",
    tags=["Verification"],
)
async def diagnose_item(
    name: str = Query(..., min_length=2, description="Exact or partial item name"),
    hub:  str = Query("Rens", description="Hub name (Jita/Amarr/Dodixie/Rens/Hek)"),
):
    """
    Traces every filter stage of the arbitrage pipeline for the named item
    and explains exactly why it is (or is not) in the opportunities list.

    Useful for automated verification against a reference dataset.
    """
    hub_info = next(
        (v for k, v in _HUB_LOOKUP.items() if hub.lower() in k),
        _HUB_LOOKUP["rens"],
    )
    age = str(_MAX_AGE_MIN)

    result: dict = {
        "query":  {"name": name, "hub": hub_info["name"]},
        "steps":  {},
        "verdict": None,
    }

    # ── Step 1: item_types ────────────────────────────────────────────────────
    type_row = await _pool.fetchrow(
        """SELECT type_id, name, COALESCE(packaged_volume, 1.0)::float AS packaged_volume,
                  group_name, category_name
           FROM item_types WHERE name ILIKE $1 LIMIT 1""",
        f"%{name}%",
    )
    if not type_row:
        result["verdict"] = "MISSING: Item not found in item_types table (not in SDE/ESI cache)"
        return result

    type_id  = type_row["type_id"]
    pkg_vol  = type_row["packaged_volume"]
    result["item"] = _row(type_row)

    # ── Step 2: demand (market_history) ──────────────────────────────────────
    demand = await _pool.fetchrow(
        """
        SELECT COALESCE(SUM(volume), 0)::float / $3 AS avg_daily,
               COUNT(*) AS trading_days
        FROM   market_history
        WHERE  type_id = $1 AND region_id = $2
          AND  date >= CURRENT_DATE - ($3 * INTERVAL '1 day')
        """,
        type_id, hub_info["region_id"], _DEMAND_DAYS,
    )
    avg_daily = demand["avg_daily"] if demand else 0.0
    result["steps"]["1_demand"] = {
        "avg_daily_volume": round(avg_daily, 3),
        "trading_days_in_window": demand["trading_days"] if demand else 0,
        "min_required": _MIN_DAILY_VOL,
        "passes": avg_daily >= _MIN_DAILY_VOL,
    }
    if avg_daily < _MIN_DAILY_VOL:
        result["verdict"] = (
            f"FILTERED at step 1 — avg daily volume ({avg_daily:.3f}) "
            f"< minimum ({_MIN_DAILY_VOL})"
        )
        return result

    # ── Step 3: supply at hub ─────────────────────────────────────────────────
    supply_row = await _pool.fetchrow(
        """
        SELECT COALESCE(SUM(volume_remain), 0)::bigint AS supply
        FROM   market_orders
        WHERE  location_id = $1 AND type_id = $2
          AND  is_buy_order = FALSE
          AND  captured_at >= NOW() - ($3 || ' minutes')::interval
        """,
        hub_info["station_id"], type_id, age,
    )
    supply = int(supply_row["supply"]) if supply_row else 0
    dos    = supply / avg_daily if avg_daily > 0 else 0.0
    result["steps"]["2_supply"] = {
        "current_supply_units": supply,
        "days_of_supply": round(dos, 2),
        "max_days_allowed": _MAX_DAYS_SUPPLY,
        "passes": dos <= _MAX_DAYS_SUPPLY,
    }
    if dos > _MAX_DAYS_SUPPLY:
        result["verdict"] = (
            f"FILTERED at step 2 — days of supply ({dos:.1f}d) "
            f"> maximum ({_MAX_DAYS_SUPPLY}d)"
        )
        return result

    # ── Step 4: Jita price ────────────────────────────────────────────────────
    jita_cum = await _pool.fetchrow(
        """
        WITH ranked AS (
            SELECT price,
                   SUM(volume_remain) OVER (
                       ORDER BY price
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS cum_supply
            FROM market_orders
            WHERE location_id = $1 AND type_id = $2
              AND is_buy_order = FALSE
              AND captured_at >= NOW() - ($3 || ' minutes')::interval
        )
        SELECT price FROM ranked WHERE cum_supply >= $4 ORDER BY price LIMIT 1
        """,
        JITA_STATION, type_id, age, avg_daily,
    )
    jita_meta = await _pool.fetchrow(
        """
        SELECT MIN(price)::float AS cheapest, SUM(volume_remain) AS total_supply
        FROM market_orders
        WHERE location_id = $1 AND type_id = $2
          AND is_buy_order = FALSE
          AND captured_at >= NOW() - ($3 || ' minutes')::interval
        """,
        JITA_STATION, type_id, age,
    )
    jita_price = (
        float(jita_cum["price"]) if jita_cum
        else (float(jita_meta["cheapest"]) if jita_meta and jita_meta["cheapest"] else None)
    )
    result["steps"]["3_jita_price"] = {
        "realistic_price": jita_price,
        "cheapest_available": float(jita_meta["cheapest"]) if jita_meta and jita_meta["cheapest"] else None,
        "total_jita_supply": int(jita_meta["total_supply"]) if jita_meta and jita_meta["total_supply"] else 0,
        "passes": jita_price is not None,
    }
    if jita_price is None:
        result["verdict"] = "FILTERED at step 3 — no Jita sell orders in the last 20 minutes"
        return result

    # ── Step 5: target price + sanity check ───────────────────────────────────
    live_row = await _pool.fetchrow(
        """
        SELECT MIN(price)::float AS price FROM market_orders
        WHERE location_id = $1 AND type_id = $2
          AND is_buy_order = FALSE
          AND captured_at >= NOW() - ($3 || ' minutes')::interval
        """,
        hub_info["station_id"], type_id, age,
    )
    hist_row = await _pool.fetchrow(
        """
        SELECT AVG(average)::float AS price FROM market_history
        WHERE region_id = $1 AND type_id = $2
          AND date >= CURRENT_DATE - ($3 * INTERVAL '1 day')
        """,
        hub_info["region_id"], type_id, _DEMAND_DAYS,
    )
    live_price = float(live_row["price"]) if live_row and live_row["price"] else None
    hist_price = float(hist_row["price"]) if hist_row and hist_row["price"] else None

    if live_price is None:
        effective = hist_price
        price_note = "No live orders at hub — using 7-day historical average"
    elif hist_price and live_price > hist_price * _SANITY_MULT:
        effective = hist_price
        price_note = (
            f"Sanity check triggered: live {live_price:,.0f} ISK is "
            f"{live_price/hist_price:.1f}× the 7-day avg — using hist avg instead"
        )
    else:
        effective = live_price
        price_note = "Using live cheapest sell order"

    result["steps"]["4_target_price"] = {
        "live_price": live_price,
        "hist_avg_price": hist_price,
        "effective_price": effective,
        "note": price_note,
        "passes": effective is not None,
    }
    if not effective:
        result["verdict"] = "FILTERED at step 4 — no live orders and no price history at hub"
        return result

    # ── Step 6: margin calculation ────────────────────────────────────────────
    shipping  = pkg_vol * _SHIPPING_PER_M3
    total_cost = jita_price + shipping
    net_rev    = effective * (1.0 - _OVERHEAD)
    profit     = net_rev - total_cost
    margin_pct = (profit / total_cost * 100.0) if total_cost > 0 else 0.0

    result["steps"]["5_margin"] = {
        "jita_price":           round(jita_price, 2),
        "shipping_cost":        round(shipping, 2),
        "packaged_volume_m3":   pkg_vol,
        "total_cost":           round(total_cost, 2),
        "effective_sell_price": round(effective, 2),
        "sell_overhead_pct":    round(_OVERHEAD * 100, 2),
        "net_revenue":          round(net_rev, 2),
        "profit_per_unit":      round(profit, 2),
        "margin_pct":           round(margin_pct, 2),
        "min_margin_required":  _MIN_MARGIN_PCT,
        "min_profit_required":  _MIN_PROFIT_ISK,
        "passes": margin_pct >= _MIN_MARGIN_PCT or profit >= _MIN_PROFIT_ISK,
    }

    # ── Step 7: opportunities table ───────────────────────────────────────────
    opp = await _pool.fetchrow(
        """
        SELECT id, margin_pct::float, detected_at, active
        FROM opportunities
        WHERE type_id = $1 AND target_station_id = $2 AND active = TRUE
        """,
        type_id, hub_info["station_id"],
    )
    result["in_opportunities_table"] = _row(opp) if opp else None

    if margin_pct < _MIN_MARGIN_PCT and profit < _MIN_PROFIT_ISK:
        result["verdict"] = (
            f"FILTERED at step 5 — margin {margin_pct:.1f}% < {_MIN_MARGIN_PCT}% "
            f"AND profit {profit:,.0f} ISK < {_MIN_PROFIT_ISK:,.0f} ISK"
        )
    elif opp:
        result["verdict"] = (
            f"IN LIST ✓ — margin {opp['margin_pct']:.1f}%, "
            f"detected {opp['detected_at'].strftime('%H:%M:%S UTC') if opp['detected_at'] else '?'}"
        )
    else:
        result["verdict"] = (
            f"PASSES ALL FILTERS but not yet in table — "
            f"expected margin {margin_pct:.1f}%. "
            f"Wait for the next arbitrage pass (~5 min) or check agent logs."
        )

    return result


@app.get(
    "/api/diagnostics/batch",
    summary="Check multiple items at once",
    tags=["Verification"],
)
async def diagnose_batch(
    names: str = Query(..., description="Comma-separated item names"),
    hub:   str = Query("Rens"),
):
    """
    Run pipeline diagnostics for multiple items in one call.
    Returns a summary dict keyed by item name.
    """
    name_list = [n.strip() for n in names.split(",") if n.strip()]
    if len(name_list) > 50:
        raise HTTPException(400, "Maximum 50 items per batch request")

    results = {}
    for name in name_list:
        results[name] = await diagnose_item(name=name, hub=hub)
    return results
