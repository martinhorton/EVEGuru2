"""
EVE SDE Loader — downloads the Fuzzwork SQLite SDE and imports static data
into the eveguru PostgreSQL database.

Runs once at startup; re-imports if the cached SDE file is older than
REFRESH_DAYS days (default 30).  The SQLite file is persisted in the
sde_cache Docker volume so it only needs to be downloaded once a month.

Tables imported:
  item_types        — from invTypes + invGroups + invCategories
  blueprints        — from industryActivityProducts (activityID=1)
  blueprint_materials — from industryActivityMaterials (activityID=1)
"""

import bz2
import logging
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s sde_loader — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────

SDE_URL      = "https://www.fuzzwork.co.uk/dump/sqlite-latest.sqlite.bz2"
CACHE_DIR    = Path(os.environ.get("SDE_CACHE_DIR", "/sde_cache"))
SQLITE_PATH  = CACHE_DIR / "sde.sqlite"
REFRESH_DAYS = int(os.environ.get("SDE_REFRESH_DAYS", "30"))
DATABASE_URL = os.environ["DATABASE_URL"]

# ─── Download helpers ──────────────────────────────────────────────────────────

def _needs_refresh() -> bool:
    if not SQLITE_PATH.exists():
        return True
    age_days = (time.time() - SQLITE_PATH.stat().st_mtime) / 86400
    if age_days > REFRESH_DAYS:
        log.info("Cached SDE is %.0f days old — refreshing", age_days)
        return True
    log.info("Using cached SDE (%.1f days old)", age_days)
    return False


def _download_sde() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    bz2_path = CACHE_DIR / "sde.sqlite.bz2"

    log.info("Downloading SDE from %s …", SDE_URL)

    def _progress(count, block_size, total):
        if total > 0 and count % 100 == 0:
            pct = min(100, count * block_size * 100 // total)
            print(f"\r  {pct}% …", end="", flush=True)

    urllib.request.urlretrieve(SDE_URL, bz2_path, _progress)
    print()
    log.info("Download complete — decompressing …")

    with bz2.open(bz2_path, "rb") as src, open(SQLITE_PATH, "wb") as dst:
        shutil.copyfileobj(src, dst)

    bz2_path.unlink(missing_ok=True)
    log.info("SDE ready at %s (%.0f MB)", SQLITE_PATH, SQLITE_PATH.stat().st_size / 1e6)


# ─── SQL ───────────────────────────────────────────────────────────────────────

_ENSURE_ITEM_TYPE_COLS = """
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS group_id        INTEGER;
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS group_name      VARCHAR(200);
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS category_id     INTEGER;
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS category_name   VARCHAR(200);
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS market_group_id INTEGER;
"""

_ENSURE_BLUEPRINT_TABLES = """
CREATE TABLE IF NOT EXISTS blueprints (
    blueprint_type_id  INTEGER PRIMARY KEY,
    product_type_id    INTEGER NOT NULL,
    product_qty        INTEGER NOT NULL DEFAULT 1,
    base_time_seconds  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bp_product ON blueprints (product_type_id);

CREATE TABLE IF NOT EXISTS blueprint_materials (
    blueprint_type_id  INTEGER NOT NULL,
    material_type_id   INTEGER NOT NULL,
    quantity           BIGINT  NOT NULL,
    PRIMARY KEY (blueprint_type_id, material_type_id)
);
CREATE INDEX IF NOT EXISTS idx_bpm_bp ON blueprint_materials (blueprint_type_id);

CREATE TABLE IF NOT EXISTS sde_meta (
    id          SERIAL PRIMARY KEY,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    type_count  INTEGER,
    bp_count    INTEGER,
    sde_url     TEXT
);
"""

_UPSERT_TYPES = """
INSERT INTO item_types
    (type_id, name, packaged_volume, group_id, group_name,
     category_id, category_name, market_group_id, last_updated)
VALUES %s
ON CONFLICT (type_id) DO UPDATE SET
    name             = EXCLUDED.name,
    packaged_volume  = EXCLUDED.packaged_volume,
    group_id         = EXCLUDED.group_id,
    group_name       = EXCLUDED.group_name,
    category_id      = EXCLUDED.category_id,
    category_name    = EXCLUDED.category_name,
    market_group_id  = EXCLUDED.market_group_id,
    last_updated     = EXCLUDED.last_updated
"""

_UPSERT_BLUEPRINTS = """
INSERT INTO blueprints (blueprint_type_id, product_type_id, product_qty, base_time_seconds)
VALUES %s
ON CONFLICT (blueprint_type_id) DO UPDATE SET
    product_type_id   = EXCLUDED.product_type_id,
    product_qty       = EXCLUDED.product_qty,
    base_time_seconds = EXCLUDED.base_time_seconds
"""

_UPSERT_MATERIALS = """
INSERT INTO blueprint_materials (blueprint_type_id, material_type_id, quantity)
VALUES %s
ON CONFLICT (blueprint_type_id, material_type_id) DO UPDATE SET
    quantity = EXCLUDED.quantity
"""

# ─── SQLite queries ────────────────────────────────────────────────────────────

_SDE_TYPES_QUERY = """
SELECT
    t.typeID        AS type_id,
    t.typeName      AS name,
    COALESCE(t.volume, 1.0) AS packaged_volume,
    t.groupID       AS group_id,
    g.groupName     AS group_name,
    g.categoryID    AS category_id,
    c.categoryName  AS category_name,
    t.marketGroupID AS market_group_id
FROM invTypes t
JOIN invGroups     g ON t.groupID    = g.groupID
JOIN invCategories c ON g.categoryID = c.categoryID
WHERE t.published = 1
"""

# Manufacturing activity = 1
_SDE_BLUEPRINTS_QUERY = """
SELECT
    iap.typeID       AS blueprint_type_id,
    iap.productTypeID AS product_type_id,
    iap.quantity      AS product_qty,
    COALESCE(ia.time, 0) AS base_time_seconds
FROM industryActivityProducts iap
JOIN industryActivity ia
     ON ia.typeID = iap.typeID AND ia.activityID = 1
WHERE iap.activityID = 1
"""

_SDE_MATERIALS_QUERY = """
SELECT
    iam.typeID         AS blueprint_type_id,
    iam.materialTypeID AS material_type_id,
    iam.quantity
FROM industryActivityMaterials iam
WHERE iam.activityID = 1
"""


# ─── Main ──────────────────────────────────────────────────────────────────────

def run_import() -> None:
    if _needs_refresh():
        _download_sde()

    log.info("Reading SDE from SQLite …")
    con = sqlite3.connect(str(SQLITE_PATH))
    con.row_factory = sqlite3.Row

    cur = con.cursor()

    cur.execute(_SDE_TYPES_QUERY)
    type_rows = cur.fetchall()
    log.info("  %d published types", len(type_rows))

    cur.execute(_SDE_BLUEPRINTS_QUERY)
    bp_rows = cur.fetchall()
    log.info("  %d manufacturing blueprints", len(bp_rows))

    cur.execute(_SDE_MATERIALS_QUERY)
    mat_rows = cur.fetchall()
    log.info("  %d material requirements", len(mat_rows))

    con.close()

    # ── Write to PostgreSQL ─────────────────────────────────────────────────────
    log.info("Connecting to PostgreSQL …")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    pg = conn.cursor()

    log.info("Ensuring schema …")
    for stmt in _ENSURE_ITEM_TYPE_COLS.strip().splitlines():
        stmt = stmt.strip()
        if stmt:
            pg.execute(stmt)

    pg.execute(_ENSURE_BLUEPRINT_TABLES)

    now = datetime.now(timezone.utc)

    # ── item_types ──────────────────────────────────────────────────────────────
    log.info("Upserting item_types …")
    BATCH = 2000
    type_records = [
        (r["type_id"], r["name"], float(r["packaged_volume"]),
         r["group_id"], r["group_name"],
         r["category_id"], r["category_name"],
         r["market_group_id"], now)
        for r in type_rows
    ]
    for i in range(0, len(type_records), BATCH):
        psycopg2.extras.execute_values(pg, _UPSERT_TYPES, type_records[i:i+BATCH], page_size=BATCH)
        if i and i % 20000 == 0:
            log.info("  … %d / %d types", i, len(type_records))

    # ── blueprints ──────────────────────────────────────────────────────────────
    log.info("Upserting blueprints …")
    bp_records = [
        (r["blueprint_type_id"], r["product_type_id"],
         r["product_qty"], r["base_time_seconds"])
        for r in bp_rows
    ]
    for i in range(0, len(bp_records), BATCH):
        psycopg2.extras.execute_values(pg, _UPSERT_BLUEPRINTS, bp_records[i:i+BATCH], page_size=BATCH)

    # ── blueprint_materials ─────────────────────────────────────────────────────
    log.info("Upserting blueprint_materials …")
    mat_records = [
        (r["blueprint_type_id"], r["material_type_id"], r["quantity"])
        for r in mat_rows
    ]
    for i in range(0, len(mat_records), BATCH):
        psycopg2.extras.execute_values(pg, _UPSERT_MATERIALS, mat_records[i:i+BATCH], page_size=BATCH)
        if i and i % 40000 == 0:
            log.info("  … %d / %d materials", i, len(mat_records))

    pg.execute(
        "INSERT INTO sde_meta (type_count, bp_count, sde_url) VALUES (%s, %s, %s)",
        (len(type_records), len(bp_records), SDE_URL),
    )
    conn.commit()
    pg.close()
    conn.close()

    log.info("SDE import complete — %d types, %d blueprints, %d material rows",
             len(type_records), len(bp_records), len(mat_records))


if __name__ == "__main__":
    try:
        run_import()
    except Exception:
        log.exception("SDE loader failed")
        sys.exit(1)
