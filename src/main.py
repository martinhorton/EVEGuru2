"""
EVEGuru2 — main entry point.

Starts three concurrent loops:
  • history_loop  — scans all regions once every 23 hours
  • order_loop    — scans all hub regions every 5 minutes
  • arbitrage_loop — analyses after each order cycle
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone

from . import config, database
from .esi_client import ESIClient
from .agents import history_agent, order_agent, arbitrage_agent, report_agent

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/app/logs/eveguru.log"),
    ],
)
log = logging.getLogger(__name__)

_shutdown = asyncio.Event()


def _handle_signal(*_: object) -> None:
    log.info("Shutdown signal received")
    _shutdown.set()


async def history_loop(esi: ESIClient) -> None:
    """Run history + type-resolution for all regions once, then repeat daily."""
    while not _shutdown.is_set():
        for region_id in config.ALL_REGION_IDS:
            if _shutdown.is_set():
                break
            try:
                # Fetch type_ids once — avoids a second ESI call that would
                # hit the ETag cache and return 304 (zero types)
                type_ids = await esi.get_region_types(region_id)
                log.info("[history] Region %s has %d active type_ids",
                         region_id, len(type_ids))
                if not type_ids:
                    continue
                await history_agent.resolve_unknown_types(esi, region_id, type_ids)
                await history_agent.run_once(esi, region_id, type_ids)
            except Exception:
                log.exception("[history] Error scanning region %s", region_id)

        log.info("[history] All regions done. Next run in %dh",
                 config.HISTORY_SCAN_INTERVAL_S // 3600)
        try:
            await asyncio.wait_for(
                _shutdown.wait(), timeout=config.HISTORY_SCAN_INTERVAL_S
            )
        except asyncio.TimeoutError:
            pass


async def order_loop(esi: ESIClient) -> None:
    """Scan sell orders for every hub region every ~5 minutes."""
    while not _shutdown.is_set():
        for hub in config.HUBS:
            if _shutdown.is_set():
                break
            try:
                await order_agent.run_once(esi, hub)
            except Exception:
                log.exception("[orders] Error scanning %s", hub.name)

        try:
            await asyncio.wait_for(
                _shutdown.wait(), timeout=config.ORDER_SCAN_INTERVAL_S
            )
        except asyncio.TimeoutError:
            pass


async def arbitrage_loop() -> None:
    """Run arbitrage analysis after every order cycle, then prune old data nightly."""
    cycle = 0
    while not _shutdown.is_set():
        # Wait for the order loop to have had a chance to populate data
        await asyncio.sleep(config.ORDER_SCAN_INTERVAL_S)
        if _shutdown.is_set():
            break
        try:
            await arbitrage_agent.run_once()
        except Exception:
            log.exception("[arbitrage] Error during analysis pass")

        cycle += 1
        if cycle % 288 == 0:  # roughly once per day (288 × 5min = 24h)
            try:
                await database.prune_old_orders()
            except Exception:
                log.exception("[db] Error pruning old orders")


async def report_loop() -> None:
    """Send daily market-opportunity emails at config.REPORT_HOUR_UTC."""
    while not _shutdown.is_set():
        now      = datetime.now(timezone.utc)
        next_run = now.replace(
            hour=config.REPORT_HOUR_UTC, minute=0, second=0, microsecond=0
        )
        if next_run <= now:
            next_run += timedelta(days=1)

        wait_secs = (next_run - now).total_seconds()
        log.info("[report] Next run at %s UTC (in %.0f min)",
                 next_run.strftime("%H:%M"), wait_secs / 60)

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=wait_secs)
            return  # clean shutdown
        except asyncio.TimeoutError:
            pass

        if _shutdown.is_set():
            break

        try:
            await report_agent.run_once()
        except Exception:
            log.exception("[report] Error during daily report")


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    log.info("EVEGuru2 starting up")
    await database.init_pool()

    async with ESIClient() as esi:
        log.info("ESI client ready — launching agents")
        await asyncio.gather(
            history_loop(esi),
            order_loop(esi),
            arbitrage_loop(),
            report_loop(),
        )

    await database.close_pool()
    log.info("EVEGuru2 shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
