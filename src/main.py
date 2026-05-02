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
    try:
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
            except BaseException as exc:
                log.error("[history] wait_for interrupted by %s — exiting loop",
                          type(exc).__name__)
                raise
    except asyncio.CancelledError:
        log.warning("[history] loop cancelled (CancelledError)")
        raise
    except BaseException as exc:
        log.error("[history] loop exiting due to unexpected %s: %s",
                  type(exc).__name__, exc)
        raise
    finally:
        log.info("[history] loop finished — _shutdown=%s", _shutdown.is_set())


async def order_loop(esi: ESIClient) -> None:
    """Scan sell orders for every hub region every ~5 minutes.

    Hubs are scanned sequentially so that only one region's orders (up to
    ~285K dicts for Jita) are held in Python memory at once.  Scanning all
    five concurrently could hold ~1M order dicts simultaneously, pushing the
    container over its 256 MB memory limit.  The latency cost is small
    compared to the 5-minute scan interval and the arbitrage window.
    """
    try:
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
            except BaseException as exc:
                log.error("[orders] wait_for interrupted by %s — exiting loop",
                          type(exc).__name__)
                raise
    except asyncio.CancelledError:
        log.warning("[orders] loop cancelled (CancelledError)")
        raise
    except BaseException as exc:
        log.error("[orders] loop exiting due to unexpected %s: %s",
                  type(exc).__name__, exc)
        raise
    finally:
        log.info("[orders] loop finished — _shutdown=%s", _shutdown.is_set())


async def arbitrage_loop() -> None:
    """Run arbitrage analysis after every order cycle, prune old orders every 2 cycles."""
    cycle = 0
    try:
        while not _shutdown.is_set():
            # Wait for the order loop to have had a chance to populate data
            try:
                await asyncio.wait_for(
                    _shutdown.wait(), timeout=config.ORDER_SCAN_INTERVAL_S
                )
                break  # shutdown was signalled
            except asyncio.TimeoutError:
                pass
            except BaseException as exc:
                log.error("[arbitrage] wait_for interrupted by %s — exiting loop",
                          type(exc).__name__)
                raise

            if _shutdown.is_set():
                break
            try:
                await arbitrage_agent.run_once()
            except Exception:
                log.exception("[arbitrage] Error during analysis pass")

            cycle += 1
            if cycle % 2 == 0:  # every ~10 minutes keeps the table small
                try:
                    await database.prune_old_orders()
                except Exception:
                    log.exception("[db] Error pruning old orders")
    except asyncio.CancelledError:
        log.warning("[arbitrage] loop cancelled (CancelledError)")
        raise
    except BaseException as exc:
        log.error("[arbitrage] loop exiting due to unexpected %s: %s",
                  type(exc).__name__, exc)
        raise
    finally:
        log.info("[arbitrage] loop finished — _shutdown=%s", _shutdown.is_set())


async def report_loop() -> None:
    """Send daily market-opportunity emails at config.REPORT_HOUR_UTC."""
    try:
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
            except BaseException as exc:
                log.error("[report] wait_for interrupted by %s — exiting loop",
                          type(exc).__name__)
                raise

            if _shutdown.is_set():
                break

            try:
                await report_agent.run_once()
            except Exception:
                log.exception("[report] Error during daily report")
    except asyncio.CancelledError:
        log.warning("[report] loop cancelled (CancelledError)")
        raise
    except BaseException as exc:
        log.error("[report] loop exiting due to unexpected %s: %s",
                  type(exc).__name__, exc)
        raise
    finally:
        log.info("[report] loop finished — _shutdown=%s", _shutdown.is_set())


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    log.info("EVEGuru2 starting up")
    await database.init_pool()

    # Prune any leftover stale orders from previous runs before agents start.
    # This prevents a large accumulated table from immediately timing out the
    # first arbitrage pass after a crash-loop restart.
    try:
        await database.prune_old_orders()
    except Exception:
        log.exception("[db] Startup prune failed — continuing anyway")

    async with ESIClient() as esi:
        log.info("ESI client ready — launching agents")
        # return_exceptions=True so a fatal error in one loop doesn't silently
        # cancel all the others — each loop logs its own exceptions internally.
        log.info("Launching all agent loops")
        results = await asyncio.gather(
            history_loop(esi),
            order_loop(esi),
            arbitrage_loop(),
            report_loop(),
            return_exceptions=True,
        )
        log.info("All agent loops have exited — checking results")
        for name, result in zip(
            ("history", "orders", "arbitrage", "report"), results
        ):
            if isinstance(result, BaseException):
                log.error("Loop '%s' exited with %s: %s",
                          name, type(result).__name__, result)
            else:
                log.info("Loop '%s' returned normally", name)

    await database.close_pool()
    log.info("EVEGuru2 shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
