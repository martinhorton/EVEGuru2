# EVEGuru2

EVE Online market arbitrage scanner. Identifies profitable opportunities to buy items at Jita (The Forge) and sell them at regional trade hubs (Amarr, Dodixie, Rens, Hek) after accounting for shipping costs, broker fees and sales tax.

---

## Contents

1. [Architecture Overview](#architecture-overview)
2. [Services](#services)
3. [Server & Paths](#server--paths)
4. [Ports](#ports)
5. [Database Schema](#database-schema)
6. [How It Works](#how-it-works)
7. [API Reference](#api-reference)
8. [Configuration](#configuration)
9. [Deploying & Updating](#deploying--updating)
10. [Diagnostics & Verification](#diagnostics--verification)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                  │
│                                                         │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐            │
│  │  agent   │   │   api    │   │   web    │            │
│  │ (Python) │   │(FastAPI) │   │ (nginx)  │            │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘            │
│       │              │              │                   │
│       └──────────────┴──────┐  /api/* proxied           │
│                             │  to api:8000              │
│                      ┌──────┴──────┐                    │
│                      │     db      │                    │
│                      │(PostgreSQL) │                    │
│                      └─────────────┘                    │
└─────────────────────────────────────────────────────────┘
         ▲                              ▲
         │ ESI API calls                │ HTTPS :8443
    EVE Online                    User browser
```

---

## Services

### `agent` — Market Scanner
Python asyncio service running four concurrent loops:

| Loop | Interval | Purpose |
|---|---|---|
| `history_loop` | Every 23h | Fetches 30-day OHLCV history for all traded types in all hub regions |
| `order_loop` | Every 5 min | Fetches live sell orders for all 5 hub regions concurrently |
| `arbitrage_loop` | Every 5 min | Analyses orders and writes profitable opportunities to DB |
| `report_loop` | Daily (configurable hour) | Sends AI-generated email report of top opportunities |

**Source:** `src/`  
**Entry point:** `python -m src.main`

### `api` — REST API
FastAPI application served by uvicorn. Exposes all market data and diagnostic endpoints. See [API Reference](#api-reference).

**Source:** `api/main.py`  
**Internal port:** 8000

### `web` — Frontend + Reverse Proxy
nginx container serving the compiled React/Vite frontend and proxying `/api/*` requests to the `api` container.

**Source:** `frontend/`  
**Config:** `frontend/nginx.conf`

### `db` — PostgreSQL 16
Stores all market data, opportunities and configuration.

**Image:** `postgres:16-alpine`  
**Internal port:** 5432 (not exposed to host)

### `sde` — Static Data Loader (one-shot)
Imports EVE's Static Data Export (SDE) — item types, blueprint materials and volumes. Runs once and exits. Re-run when new EVE expansions are released.

**Source:** `sde/`  
**Trigger:** `docker compose run --rm sde`

---

## Server & Paths

| Item | Value |
|---|---|
| **Host** | `ALBS-AlmaLinux` — 192.168.135.168 |
| **OS** | AlmaLinux |
| **Project root** | `/home/martinhorton/Projects/EVEGuru2` |
| **Logs** | `/home/martinhorton/Projects/EVEGuru2/logs/eveguru.log` |
| **Docker volumes** | `eveguru2_postgres_data` (DB data), `eveguru2_sde_cache`, `eveguru2_ssl_certs` |
| **Environment file** | `/home/martinhorton/Projects/EVEGuru2/.env` |
| **Windows dev path** | `C:\Users\Martin.Horton\OneDrive - Ash & Lacy Buildings Solutions Ltd\Projects\EVEGuru2` |
| **GitHub** | `https://github.com/martinhorton/EVEGuru2` |

---

## Ports

| Port | Protocol | Service | Description |
|---|---|---|---|
| **8080** | HTTP | `web` (nginx) | Redirects to HTTPS 8443 |
| **8443** | HTTPS | `web` (nginx) | Main entry point — serves UI and proxies API |
| 5432 | TCP | `db` | PostgreSQL — **internal Docker network only** |
| 8000 | TCP | `api` | uvicorn — **internal Docker network only** |

> Other services on this host use ports 443 (cereberus-nginx), 8000 (hollybot-webui), 8001 (hollybot-reranker), 5432 (lightrag-postgres), 5678 (n8n), 3978 (hollybot-teamsbot).

---

## Database Schema

### `hubs`
Configured trading hubs. Jita is marked `is_supply=TRUE` and is the source for all arbitrage.

### `item_types`
EVE item catalogue — name, packaged volume, group, category. Populated by the SDE loader; unknown new items resolved via ESI.

### `market_history`
Daily OHLCV (Open/High/Low/Close/Volume) data per region per type. 30 days retained. Source of demand calculations.

### `market_orders`
Live sell order snapshots. Pruned every hour to retain only the last ~30 minutes of data (sufficient for the 65-minute freshness window with ETag caching).

Partitioned by `captured_at` with a default partition.

Key index: `(location_id, type_id, is_buy_order, captured_at DESC)`

### `opportunities`
Active arbitrage opportunities. Partial unique index on `(type_id, target_station_id) WHERE active=TRUE` — each item/hub pair has exactly one live row, refreshed each scan.

Deactivated after 1 hour without a refresh.

### `blueprints` / `blueprint_materials`
Manufacturing blueprint data from SDE. Used by the Industry tab.

### `sde_meta`
Tracks SDE import history (timestamp, record counts, source URL).

---

## How It Works

### Arbitrage Pipeline

For each target hub (Amarr, Dodixie, Rens, Hek), a single bulk SQL query per hub replaces what was previously thousands of individual DB round-trips:

```
1. DEMAND CHECK
   market_history → avg daily volume ≥ MIN_DAILY_VOLUME (default 1.0/day)
   Uses SUM(volume)/7 over last 7 calendar days (includes zero-trade days)

2. SUPPLY CHECK
   market_orders at hub station → days_of_supply = supply / avg_daily
   Pass if days_of_supply ≤ MAX_DAYS_SUPPLY (default 60)

3. JITA PRICE
   market_orders across The Forge REGION (not just NPC station 60003760)
   Includes Perimeter citadel orders — many T2/faction items only list there
   Realistic price = first price tier where cumulative supply ≥ avg_daily
   (avoids single-unit lowball orders skewing the source cost)

4. TARGET PRICE
   Cheapest live sell at hub station.
   If live price > 5× 7-day hist avg → substitute hist avg (scam/stale filter)
   If no live orders → use hist avg as proxy

5. MARGIN CALCULATION
   shipping_cost = packaged_volume_m3 × SHIPPING_ISK_PER_M3
   total_cost    = jita_price + shipping_cost
   net_revenue   = target_price × (1 - SELL_OVERHEAD_PCT)
   margin_pct    = (net_revenue - total_cost) / total_cost × 100

   PASSES if: margin_pct ≥ MIN_MARGIN_PCT (10%)
           OR profit_per_unit ≥ MIN_PROFIT_ISK (500,000 ISK)
           (ISK floor catches large ships with low % margin)
```

### ETag Caching
ESI orders are cached for 5 minutes. When nothing has changed, ESI returns HTTP 304 and no new rows are inserted — so `captured_at` is not updated. The freshness window is therefore set to 65 minutes (12 × 5-min cycles) rather than the naive 20 minutes.

### Order Scanning
All 5 hub regions are scanned **concurrently** (asyncio.gather) so Jita's larger payload doesn't delay other regions and cause stale data at the time the arbitrage agent runs.

---

## API Reference

Base URL: `https://192.168.135.168:8443`  
Interactive docs: `https://192.168.135.168:8443/docs`

### Market Opportunities

| Method | Path | Description |
|---|---|---|
| GET | `/api/opportunities` | Active opportunities. Filters: `hub`, `min_margin`, `category_id`, `group_id`, `limit`, `offset` |
| GET | `/api/opportunities/search?q=<name>` | Search by partial item name |
| GET | `/api/stats` | Summary stats — count, best margin, estimated daily profit |
| GET | `/api/hubs` | Configured hub list |

### Item Data

| Method | Path | Description |
|---|---|---|
| GET | `/api/categories` | All item categories in the database |
| GET | `/api/categories/{id}/groups` | Groups within a category |
| GET | `/api/items/{type_id}` | Item metadata |
| GET | `/api/items/{type_id}/history?days=30` | OHLCV history (up to 90 days) |
| GET | `/api/items/{type_id}/orders` | Current sell orders at all hub stations |

### Industry

| Method | Path | Description |
|---|---|---|
| GET | `/api/industry/search?q=<name>` | Blueprint search |
| GET | `/api/industry/blueprint/{bp_id}` | Blueprint detail with materials |
| GET | `/api/industry/prices?type_ids=1,2,3` | Jita sell prices for a list of type IDs |
| GET | `/api/industry/hub-prices/{type_id}` | Cheapest sell at each hub |

### Diagnostics & Verification

| Method | Path | Description |
|---|---|---|
| GET | `/api/diagnostics/item?name=Helios&hub=Rens` | Full pipeline trace for one item — shows exactly which filter stage it passes or fails |
| GET | `/api/diagnostics/batch?names=Helios,Buzzard&hub=Rens` | Same for up to 50 items in one call |
| GET | `/api/health` | Health check |
| GET | `/api/config` | Active fee configuration |

---

## Configuration

All configuration lives in `/home/martinhorton/Projects/EVEGuru2/.env` on the server. This file is **not committed to git**.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — | PostgreSQL connection string |
| `MIN_DAILY_VOLUME` | `1.0` | Minimum avg daily volume to consider an item |
| `MAX_DAYS_SUPPLY` | `60` | Skip if hub already has > N days of stock |
| `MIN_MARGIN_PCT` | `10.0` | Minimum % margin to record as opportunity |
| `MIN_PROFIT_ISK` | `500000` | Minimum ISK profit per unit (alternative to % margin) |
| `SHIPPING_COST_PER_M3` | `1000` | ISK per m³ shipping cost Jita → hub |
| `SALES_TAX_PCT` | `3.6` | Sales tax % applied to revenue |
| `BROKER_FEE_PCT` | `3.0` | Broker fee % applied to revenue |
| `PRICE_SANITY_MULTIPLIER` | `5.0` | Replace live price with hist avg if live > N× hist avg |
| `LOG_LEVEL` | `INFO` | Python log level |
| `REPORT_HOUR` | `7` | UTC hour to send daily email report |
| `AI_API_KEY` | — | API key for AI report generation (DeepSeek by default) |
| `AI_BASE_URL` | `https://api.deepseek.com` | Any OpenAI-compatible endpoint |
| `AI_MODEL` | `deepseek-chat` | Model name |
| `SMTP_HOST` | — | SMTP server for email reports |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | — | SMTP username |
| `SMTP_PASSWORD` | — | SMTP password |
| `SMTP_FROM` | — | Sender address |
| `REPORT_TO` | — | Recipient address |
| `EVE_CLIENT_ID` | — | EVE SSO application client ID |
| `EVE_CLIENT_SECRET` | — | EVE SSO application client secret |
| `EVE_CALLBACK_URL` | — | EVE SSO OAuth callback URL |
| `POSTGRES_PASSWORD` | — | PostgreSQL password |

---

## Deploying & Updating

### First-time setup
```bash
cd /home/martinhorton/Projects/EVEGuru2
cp .env.example .env          # edit with real values
docker compose up -d
docker compose run --rm sde   # load SDE data (required once)
```

### Applying code changes (from Windows)
```bash
# On Windows (Claude Code / PowerShell):
git add <files> && git commit -m "message" && git push

# On Linux server:
cd /home/martinhorton/Projects/EVEGuru2
git pull

# Agent or database.py changed:
docker compose build agent && docker compose up -d agent

# API changed:
docker compose build api && docker compose up -d api && docker compose restart web

# docker-compose.yml changed (env vars, resource limits, PostgreSQL flags):
docker compose up -d --force-recreate
```

### Checking logs
```bash
# Live agent log
docker compose logs -f agent | grep -E '\[arbitrage\]|OPPORTUNITY|ERROR'

# All services
docker compose logs -f

# Persistent log file
tail -f /home/martinhorton/Projects/EVEGuru2/logs/eveguru.log
```

### Common operations
```bash
# Check all containers
docker compose ps

# Check container resource usage
docker stats

# Manual SDE refresh (after EVE expansion)
docker compose run --rm sde

# Prune stale orders manually
docker compose exec db psql -U eveguru -d eveguru \
  -c "DELETE FROM market_orders WHERE captured_at < NOW() - INTERVAL '30 minutes';"

# Opportunity count by hub
docker compose exec db psql -U eveguru -d eveguru -c "
  SELECT target_hub_name, COUNT(*), ROUND(MAX(margin_pct),1) AS best_margin
  FROM opportunities WHERE active=TRUE GROUP BY target_hub_name;"
```

---

## Diagnostics & Verification

The `/api/diagnostics/item` endpoint traces every filter stage and explains exactly why an item is or is not in the opportunities list. Useful for cross-checking against reference datasets.

### Example: check specific items at Rens
```bash
curl -sk "https://192.168.135.168:8443/api/diagnostics/batch?hub=Rens&names=Helios,Buzzard,Sunesis"
```

### Diagnostic verdict meanings

| Verdict | Meaning |
|---|---|
| `IN LIST ✓` | Item is an active opportunity |
| `FILTERED at step 1` | Avg daily volume below minimum |
| `FILTERED at step 2` | Hub already well-stocked (days of supply too high) |
| `FILTERED at step 3` | No sell orders in Jita region within freshness window |
| `FILTERED at step 4` | No price data at target hub |
| `FILTERED at step 5` | Margin and profit both below thresholds |
| `PASSES ALL FILTERS but not yet in table` | Agent hasn't run since last order scan (wait ~5 min) |
| `MISSING: Item not found in item_types` | SDE hasn't been loaded or item is very new |

### Known methodology differences vs commercial tools
- **Demand calculation**: We use `SUM(volume) / 7` (all calendar days including zero-trade days). Some commercial tools use `AVG` over trading days only, giving higher apparent demand for sporadically-traded items.
- **Thermonuclear Trigger Unit**: Filtered at Rens because the hub is currently overstocked (>60 days supply). This is correct — it is not a buying opportunity when supply already exceeds demand by that margin.
