import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Hub:
    name: str
    region_id: int
    station_id: int
    is_supply: bool = False


HUBS: list[Hub] = [
    Hub("Jita IV - Moon 4",          10000002, 60003760, is_supply=True),
    Hub("Amarr Emperor Family Academy", 10000043, 60008494),
    Hub("Dodixie Fed Navy Assembly",  10000032, 60011866),
    Hub("Rens Brutor Tribe Treasury", 10000030, 60004588),
    Hub("Hek Boundless Creation",     10000042, 60005686),
]

SUPPLY_HUB   = next(h for h in HUBS if h.is_supply)
TARGET_HUBS  = [h for h in HUBS if not h.is_supply]
ALL_REGION_IDS = list({h.region_id for h in HUBS})

ESI_BASE_URL    = "https://esi.evetech.net/latest"
ESI_DATASOURCE  = "tranquility"
ESI_USER_AGENT  = "EVEGuru2/1.0 (market arbitrage scanner; contact martin.horton@ashandlacy.com)"

DATABASE_URL        = os.environ["DATABASE_URL"]
# Maximum days of supply already at the target hub before we ignore the item.
# Days of supply = current_supply / avg_daily_demand.
# Commercial apps typically show everything (no cap); 30 is a sensible default
# that excludes deeply over-stocked markets while catching price-spread trades.
MAX_DAYS_SUPPLY     = float(os.getenv("MAX_DAYS_SUPPLY",      "60"))
MIN_DAILY_VOLUME    = float(os.getenv("MIN_DAILY_VOLUME",     "1.0"))
MIN_MARGIN_PCT           = float(os.getenv("MIN_MARGIN_PCT",           "10.0"))
# Absolute ISK profit floor per unit (alternative to margin %).
# An opportunity passes if EITHER margin_pct >= MIN_MARGIN_PCT OR
# profit_per_unit >= MIN_PROFIT_ISK.  This catches high-value items
# (e.g. large ships) where big absolute profit produces a low % margin
# because shipping cost is large relative to unit price.
MIN_PROFIT_ISK           = float(os.getenv("MIN_PROFIT_ISK",           "500000"))
# If the current sell price at the target hub exceeds this multiple of the
# 7-day historical average, treat it as a scam/stale order and substitute
# the historical average as the expected sell price instead.
PRICE_SANITY_MULTIPLIER  = float(os.getenv("PRICE_SANITY_MULTIPLIER",  "5.0"))
SHIPPING_ISK_PER_M3 = float(os.getenv("SHIPPING_COST_PER_M3", "1000"))
SALES_TAX_PCT       = float(os.getenv("SALES_TAX_PCT",        "3.6"))
BROKER_FEE_PCT      = float(os.getenv("BROKER_FEE_PCT",       "3.0"))
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO").upper()

# Effective overhead on the sell side (tax + broker fee)
SELL_OVERHEAD_PCT = (SALES_TAX_PCT + BROKER_FEE_PCT) / 100.0

# History: days to look back for demand calculation
DEMAND_WINDOW_DAYS = 7

# Order scan interval (ESI caches orders for 5 min)
ORDER_SCAN_INTERVAL_S = 305

# History scan interval (ESI caches history for ~23h)
HISTORY_SCAN_INTERVAL_S = 23 * 3600

# Max concurrent ESI requests
ESI_CONCURRENCY = 20

# ── Daily email reports ────────────────────────────────────────────────────────
# AI provider — defaults to DeepSeek (OpenAI-compatible).
# Swap AI_BASE_URL + AI_MODEL to use any OpenAI-compatible provider.
AI_API_KEY  = os.getenv("AI_API_KEY",  "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com")
AI_MODEL    = os.getenv("AI_MODEL",    "deepseek-chat")

SMTP_HOST     = os.getenv("SMTP_HOST",     "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER",     "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM",     "eveguru2@localhost")

REPORT_TO       = os.getenv("REPORT_TO",    "")
REPORT_HOUR_UTC = int(os.getenv("REPORT_HOUR", "7"))   # UTC hour to send (0–23)
