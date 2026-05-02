-- EVEGuru2 schema

CREATE TABLE IF NOT EXISTS hubs (
    hub_id      SERIAL PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    region_id   INTEGER      NOT NULL,
    station_id  BIGINT       NOT NULL UNIQUE,
    is_supply   BOOLEAN      NOT NULL DEFAULT FALSE,
    active      BOOLEAN      NOT NULL DEFAULT TRUE
);

INSERT INTO hubs (name, region_id, station_id, is_supply, active) VALUES
    ('Jita IV - Moon 4 - Caldari Navy Assembly Plant',     10000002, 60003760, TRUE,  TRUE),
    ('Amarr VIII (Oris) - Emperor Family Academy',         10000043, 60008494, FALSE, TRUE),
    ('Dodixie IX - Moon 20 - Federation Navy Assembly Plant', 10000032, 60011866, FALSE, TRUE),
    ('Rens VI - Moon 8 - Brutor Tribe Treasury',           10000030, 60004588, FALSE, TRUE),
    ('Hek VIII - Moon 12 - Boundless Creation Factory',   10000042, 60005686, FALSE, TRUE)
ON CONFLICT (station_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS item_types (
    type_id         INTEGER PRIMARY KEY,
    name            VARCHAR(300),
    packaged_volume NUMERIC(18, 4),
    group_id        INTEGER,
    group_name      VARCHAR(200),
    category_id     INTEGER,
    category_name   VARCHAR(200),
    market_group_id INTEGER,
    last_updated    TIMESTAMPTZ
);

-- Track SDE import history
CREATE TABLE IF NOT EXISTS sde_meta (
    id          SERIAL PRIMARY KEY,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    type_count  INTEGER,
    bp_count    INTEGER,
    sde_url     TEXT
);

-- Manufacturing blueprints (from SDE industryActivityProducts, activityID=1)
CREATE TABLE IF NOT EXISTS blueprints (
    blueprint_type_id  INTEGER PRIMARY KEY,
    product_type_id    INTEGER NOT NULL,
    product_qty        INTEGER NOT NULL DEFAULT 1,
    base_time_seconds  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bp_product ON blueprints (product_type_id);

-- Materials required per blueprint run (activityID=1, base quantities)
CREATE TABLE IF NOT EXISTS blueprint_materials (
    blueprint_type_id  INTEGER NOT NULL,
    material_type_id   INTEGER NOT NULL,
    quantity           BIGINT  NOT NULL,
    PRIMARY KEY (blueprint_type_id, material_type_id)
);
CREATE INDEX IF NOT EXISTS idx_bpm_bp ON blueprint_materials (blueprint_type_id);

-- Daily OHLCV history per region
CREATE TABLE IF NOT EXISTS market_history (
    region_id   INTEGER      NOT NULL,
    type_id     INTEGER      NOT NULL,
    date        DATE         NOT NULL,
    average     NUMERIC(20, 2),
    highest     NUMERIC(20, 2),
    lowest      NUMERIC(20, 2),
    order_count INTEGER,
    volume      BIGINT,
    PRIMARY KEY (region_id, type_id, date)
);

CREATE INDEX IF NOT EXISTS idx_history_region_type ON market_history (region_id, type_id);
CREATE INDEX IF NOT EXISTS idx_history_date ON market_history (date);

-- Live order snapshots — one row per order_id, refreshed on every scan.
-- Using order_id as the sole PK keeps the table at ~620K rows regardless of
-- how many scan cycles have run (vs the old partitioned design which stored
-- one row per order per scan and grew to 8M+ rows at steady state).
CREATE TABLE IF NOT EXISTS market_orders (
    order_id        BIGINT        NOT NULL PRIMARY KEY,
    region_id       INTEGER       NOT NULL,
    type_id         INTEGER       NOT NULL,
    location_id     BIGINT        NOT NULL,
    is_buy_order    BOOLEAN       NOT NULL,
    price           NUMERIC(20,2) NOT NULL,
    volume_remain   INTEGER       NOT NULL,
    volume_total    INTEGER       NOT NULL,
    min_volume      INTEGER       NOT NULL DEFAULT 1,
    range           VARCHAR(20),
    issued          TIMESTAMPTZ   NOT NULL,
    duration        INTEGER       NOT NULL,
    captured_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- region_id + is_buy_order is the hot path for the arbitrage supply CTE
CREATE INDEX IF NOT EXISTS idx_orders_region_buy    ON market_orders (region_id, is_buy_order, price);
-- location_id lookup for hub supply / cheapest-sell queries
CREATE INDEX IF NOT EXISTS idx_orders_location_type ON market_orders (location_id, type_id, is_buy_order);
-- captured_at for the prune DELETE and freshness filters
CREATE INDEX IF NOT EXISTS idx_orders_captured      ON market_orders (captured_at);

-- Identified arbitrage opportunities
CREATE TABLE IF NOT EXISTS opportunities (
    id                      BIGSERIAL PRIMARY KEY,
    type_id                 INTEGER       NOT NULL,
    type_name               VARCHAR(300),
    target_station_id       BIGINT        NOT NULL,
    target_hub_name         VARCHAR(100),
    supply_station_id       BIGINT        NOT NULL DEFAULT 60003760,
    avg_daily_volume        NUMERIC(15, 4),
    current_supply_units    BIGINT,
    shortage_ratio          NUMERIC(15, 4),
    jita_sell_price         NUMERIC(20, 2),
    target_sell_price       NUMERIC(20, 2),
    hist_avg_price          NUMERIC(20, 2),
    shipping_cost           NUMERIC(20, 2),
    total_cost              NUMERIC(20, 2),
    expected_net_revenue    NUMERIC(20, 2),
    margin_pct              NUMERIC(12, 4),
    detected_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    active                  BOOLEAN       NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_opps_active_margin ON opportunities (active, margin_pct DESC, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_opps_type ON opportunities (type_id, detected_at DESC);

-- Prevents duplicate rows per item+hub — upsert refreshes the existing row
CREATE UNIQUE INDEX IF NOT EXISTS idx_opps_active_unique
    ON opportunities (type_id, target_station_id)
    WHERE active = TRUE;

-- Prune orders not seen in recent scans.
-- With one-row-per-order_id design the table is always small, so a plain
-- DELETE is fast — no batching needed.
CREATE OR REPLACE FUNCTION prune_old_orders() RETURNS void LANGUAGE sql AS $$
    DELETE FROM market_orders WHERE captured_at < NOW() - INTERVAL '70 minutes';
$$;
