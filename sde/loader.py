"""
EVE SDE Loader — downloads the Fuzzwork SQLite SDE and imports item
type / group / category data into the eveguru PostgreSQL database.

Runs once at startup; re-imports if the cached SDE file is older than
REFRESH_DAYS days (default 30).  The SQLite file is persisted in the
sde_cache Docker volume so it only needs to be downloaded once a month.

Key SDE tables used:
  invTypes        typeID, typeName, volume, groupID, marketGroupID, published
  invGroups       groupID, groupName, categoryID
  invCategories   categoryID, categoryName
"""

import bz2
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
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

SDE_URL       = "https://www.fuzzwork.co.uk/dump/sqlite-latest.sqlite.bz2"
CACHE_DIR     = Path(os.environ.get("SDE_CACHE_DIR", "/sde_cache"))
SQLITE_PATH   = CACHE_DIR / "sde.sqlite"
REFRESH_DAYS  = int(os.environ.get("SDE_REFRESH_DAYS", "30"))
DATABASE_URL  = os.environ["DATABASE_URL"]

# ─── Helpers ───────────────────────────────────────────────────────────────────

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
    print()  # newline after progress
    log.info("Download complete — decompressing …")

    with bz2.open(bz2_path, "rb") as src, open(SQLITE_PATH, "wb") as dst:
        shutil.copyfileobj(src, dst)

    bz2_path.unlink(missing_ok=True)
    log.info("SDE ready at %s (%.0f MB)", SQLITE_PATH, SQLITE_PATH.stat().st_size / 1e6)


# ─── Main import ───────────────────────────────────────────────────────────────

_ENSURE_COLUMNS_SQL = """
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS group_id       INTEGER;
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS group_name     VARCHAR(200);
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS category_id    INTEGER;
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS category_name  VARCHAR(200);
ALTER TABLE item_types ADD COLUMN IF NOT EXISTS market_group_id INTEGER;
"""

_ENSURE_META_SQL = """
CREATE TABLE IF NOT EXISTS sde_meta (
    id          SERIAL PRIMARY KEY,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    type_count  INTEGER,
    sde_url     TEXT
);
"""

_UPSERT_SQL = """
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

_SDE_QUERY = """
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
JOIN invGroups g      ON t.groupID    = g.groupID
JOIN invCategories c  ON g.categoryID = c.categoryID
WHERE t.published = 1
"""


def run_import() -> None:
    if _needs_refresh():
        _download_sde()

    log.info("Reading SDE from SQLite …")
    con_sq = sqlite3.connect(str(SQLITE_PATH))
    con_sq.row_factory = sqlite3.Row
    cur_sq = con_sq.cursor()
    cur_sq.execute(_SDE_QUERY)
    rows = cur_sq.fetchall()
    con_sq.close()
    log.info("  %d published types found in SDE", len(rows))

    now = datetime.now(timezone.utc)
    records = [
        (
            r["type_id"],
            r["name"],
            float(r["packaged_volume"]),
            r["group_id"],
            r["group_name"],
            r["category_id"],
            r["category_name"],
            r["market_group_id"],
            now,
        )
        for r in rows
    ]

    log.info("Connecting to PostgreSQL …")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    log.info("Ensuring schema columns exist …")
    for stmt in _ENSURE_COLUMNS_SQL.strip().split("\n"):
        stmt = stmt.strip()
        if stmt:
            cur.execute(stmt)

    cur.execute(_ENSURE_META_SQL)

    log.info("Upserting %d rows into item_types …", len(records))
    BATCH = 2000
    for i in range(0, len(records), BATCH):
        chunk = records[i : i + BATCH]
        psycopg2.extras.execute_values(cur, _UPSERT_SQL, chunk, page_size=BATCH)
        if i % 20000 == 0 and i > 0:
            log.info("  … %d / %d", i, len(records))

    cur.execute(
        "INSERT INTO sde_meta (type_count, sde_url) VALUES (%s, %s)",
        (len(records), SDE_URL),
    )
    conn.commit()
    cur.close()
    conn.close()

    log.info("SDE import complete — %d types loaded", len(records))


if __name__ == "__main__":
    try:
        run_import()
    except Exception:
        log.exception("SDE loader failed")
        sys.exit(1)
