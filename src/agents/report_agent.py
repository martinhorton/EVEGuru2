"""
Report Agent — daily AI-powered market opportunity emails.

Runs once per day at the configured UTC hour.  For each hub acting as
a supply source it:
  1. Queries current arbitrage opportunities (Jita uses pre-computed
     opportunities table; other hubs query live market_orders on-the-fly)
  2. Calls Claude AI to summarise, rank, and flag suspicious entries
  3. Formats a rich HTML email and sends it via SMTP

One email is sent per supply hub:
  • Jita  → Amarr / Dodixie / Rens / Hek
  • Amarr → Jita  / Dodixie / Rens / Hek
  • Rens  → Jita  / Amarr  / Dodixie / Hek
  • Hek   → Jita  / Amarr  / Dodixie / Rens
"""

import json
import logging
import math
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib
from openai import AsyncOpenAI

from .. import config, database
from ..config import Hub

log = logging.getLogger(__name__)

_MAX_FOR_AI    = 40   # opportunities sent to AI per hub


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _isk(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    if v >= 1_000_000_000:
        return f"{v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v/1_000:.1f}K"
    return f"{v:.0f}"


def _hub_short(name: str) -> str:
    return name.split()[0]


# ── Opportunity queries ────────────────────────────────────────────────────────

async def _jita_opportunities() -> list[dict]:
    """Pull active Jita-supply opportunities from the pre-computed table."""
    rows = await database.pool().fetch("""
        SELECT
            o.type_id,
            o.type_name,
            o.target_hub_name,
            o.avg_daily_volume::float,
            o.current_supply_units,
            o.shortage_ratio::float,
            o.jita_sell_price::float             AS supply_price,
            o.target_sell_price::float,
            o.margin_pct::float,
            ((o.expected_net_revenue - o.total_cost)
                * o.avg_daily_volume)::float       AS est_daily_profit,
            COALESCE(it.category_name, '')         AS category_name,
            COALESCE(it.group_name,    '')         AS group_name
        FROM opportunities o
        LEFT JOIN item_types it ON it.type_id = o.type_id
        WHERE o.active      = TRUE
          AND o.detected_at >= NOW() - INTERVAL '2 hours'
        ORDER BY o.margin_pct DESC
        LIMIT 500
    """)
    return [dict(r) for r in rows]


async def _hub_opportunities(supply_hub: Hub, target_hubs: list[Hub]) -> list[dict]:
    """
    On-the-fly arbitrage from any hub.
    Find items cheapest at supply_hub with higher prices at target hubs.
    """
    target_station_ids = [h.station_id for h in target_hubs]
    target_region_ids  = [h.region_id  for h in target_hubs]
    station_to_hub     = {h.station_id: h for h in target_hubs}

    rows = await database.pool().fetch("""
        WITH supply AS (
            SELECT type_id, MIN(price)::float AS supply_price
            FROM market_orders
            WHERE location_id  = $1
              AND is_buy_order = FALSE
              AND captured_at >= NOW() - INTERVAL '15 minutes'
            GROUP BY type_id
        ),
        targets AS (
            SELECT type_id, location_id, MIN(price)::float AS target_price
            FROM market_orders
            WHERE location_id  = ANY($2::bigint[])
              AND is_buy_order = FALSE
              AND captured_at >= NOW() - INTERVAL '15 minutes'
            GROUP BY type_id, location_id
        ),
        vol AS (
            SELECT type_id, region_id, AVG(volume)::float AS avg_vol
            FROM market_history
            WHERE region_id = ANY($3::integer[])
              AND date      >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY type_id, region_id
        )
        SELECT
            s.type_id,
            COALESCE(it.name, 'Unknown')          AS type_name,
            COALESCE(it.category_name, '')         AS category_name,
            COALESCE(it.group_name, '')             AS group_name,
            COALESCE(it.packaged_volume, 1.0)::float AS packaged_volume,
            s.supply_price,
            t.location_id                           AS target_station_id,
            t.target_price,
            COALESCE(v.avg_vol, 0)::float           AS avg_daily_volume
        FROM supply     s
        JOIN targets    t  ON t.type_id   = s.type_id
        JOIN item_types it ON it.type_id  = s.type_id
        LEFT JOIN vol   v  ON v.type_id   = s.type_id
          AND v.region_id = (
              SELECT h.region_id FROM hubs h
              WHERE h.station_id = t.location_id LIMIT 1
          )
        WHERE t.target_price > s.supply_price
          AND COALESCE(v.avg_vol, 0) >= $4
        ORDER BY (t.target_price - s.supply_price) / NULLIF(s.supply_price, 0) DESC
        LIMIT 300
    """, supply_hub.station_id, target_station_ids,
         target_region_ids, config.MIN_DAILY_VOLUME)

    opps = []
    for r in rows:
        r = dict(r)
        shipping    = r["packaged_volume"] * config.SHIPPING_ISK_PER_M3
        total_cost  = r["supply_price"] + shipping
        net_revenue = r["target_price"] * (1.0 - config.SELL_OVERHEAD_PCT)
        profit      = net_revenue - total_cost
        if total_cost <= 0:
            continue
        margin_pct = (profit / total_cost) * 100.0
        if margin_pct < config.MIN_MARGIN_PCT:
            continue
        hub = station_to_hub.get(r["target_station_id"])
        opps.append({
            "type_id":            r["type_id"],
            "type_name":          r["type_name"],
            "category_name":      r["category_name"],
            "group_name":         r["group_name"],
            "target_hub_name":    hub.name if hub else str(r["target_station_id"]),
            "avg_daily_volume":   r["avg_daily_volume"],
            "current_supply_units": 0,
            "shortage_ratio":     0.0,
            "supply_price":       r["supply_price"],
            "target_sell_price":  r["target_price"],
            "margin_pct":         min(margin_pct, 9999.0),
            "est_daily_profit":   profit * max(r["avg_daily_volume"], 1),
        })
    opps.sort(key=lambda x: x["margin_pct"], reverse=True)
    return opps


# ── AI analysis ────────────────────────────────────────────────────────────────

async def _ai_analyse(opps: list[dict], supply_hub_short: str) -> dict:
    """Ask Claude to summarise and rank opportunities. Returns dict with
    keys: summary, top_picks, red_flags."""
    fallback = {"summary": "", "top_picks": [], "red_flags": []}
    if not config.ANTHROPIC_API_KEY:
        log.debug("[report] ANTHROPIC_API_KEY not set — skipping AI analysis")
        return fallback
    if not opps:
        return fallback

    # Sort by a combined score before sending to AI
    def _score(o):
        vol   = max(o["avg_daily_volume"], 1)
        marg  = min(o["margin_pct"], 200)       # cap at 200 to avoid stale data dominating
        short = min(o.get("shortage_ratio") or 1, 20)
        return marg * math.log1p(vol) * math.log1p(short)

    scored = sorted(opps, key=_score, reverse=True)[:_MAX_FOR_AI]

    lines = ["Item | Category | Target | Vol/day | Shortage | Margin% | Est.Daily"]
    lines.append("─" * 70)
    for o in scored:
        lines.append(
            f"{o['type_name'][:28]:<28} | {o['category_name'][:12]:<12} | "
            f"{_hub_short(o['target_hub_name']):<8} | "
            f"{o['avg_daily_volume']:>8.0f}/day | "
            f"{(o.get('shortage_ratio') or 0):>6.1f}× | "
            f"{o['margin_pct']:>6.1f}% | "
            f"{_isk(o['est_daily_profit'])} ISK/day"
        )

    prompt = f"""You are an expert EVE Online market analyst. Analyse these inter-hub trading opportunities.

Supply hub: {supply_hub_short}

{chr(10).join(lines)}

Respond with ONLY valid JSON — no markdown fences, no extra text:
{{
  "summary": "2-3 sentences on overall market conditions from {supply_hub_short} today",
  "top_picks": [
    {{"item": "exact item name", "target": "hub name", "reason": "under 20 words", "concern": null}}
  ],
  "red_flags": [
    {{"item": "exact item name", "issue": "brief issue"}}
  ]
}}

Guidelines:
- top_picks: 5–8 best trades. Favour high volume + solid margin + high shortage. Avoid items needing billions of capital.
- red_flags: flag items where margin >500% (usually stale/misleading data), or where volume is suspiciously low for the margin shown.
- Keep all text brief and actionable."""

    try:
        client = AsyncOpenAI(
            api_key=config.AI_API_KEY,
            base_url=config.AI_BASE_URL,
        )
        resp = await client.chat.completions.create(
            model=config.AI_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.choices[0].message.content.strip()
        # Strip accidental markdown code fences
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("[report] AI returned invalid JSON for %s: %s", supply_hub_short, exc)
        return fallback
    except Exception as exc:
        log.warning("[report] AI analysis failed for %s: %s", supply_hub_short, exc)
        return fallback


# ── Email builder ──────────────────────────────────────────────────────────────

def _build_html(supply_hub: Hub, opps: list[dict], ai: dict, now: datetime) -> str:
    hub_short  = _hub_short(supply_hub.name)
    date_str   = now.strftime("%d %b %Y %H:%M UTC")
    best_m     = max((o["margin_pct"] for o in opps), default=0.0)
    total_p    = sum(o.get("est_daily_profit", 0) for o in opps)

    # Group by target hub
    by_hub: dict[str, list] = {}
    for o in opps:
        by_hub.setdefault(o["target_hub_name"], []).append(o)
    for lst in by_hub.values():
        lst.sort(key=lambda x: x["margin_pct"], reverse=True)

    # AI top picks block
    picks_html = ""
    if ai.get("top_picks"):
        rows = ""
        for i, p in enumerate(ai["top_picks"], 1):
            concern = (f'<br><span style="color:#fb923c;font-size:11px">⚠ {p["concern"]}</span>'
                       if p.get("concern") else "")
            rows += (
                f'<tr style="border-bottom:1px solid #182030">'
                f'<td style="padding:8px 10px;color:#e8b84b;font-weight:700">{i}</td>'
                f'<td style="padding:8px 10px;color:#48cae4">{p["item"]}</td>'
                f'<td style="padding:8px 10px;color:#7a8ba8">{p.get("target","")}</td>'
                f'<td style="padding:8px 10px;color:#cdd6f4">{p["reason"]}{concern}</td>'
                f'</tr>'
            )
        picks_html = (
            '<h2 style="color:#e8b84b;font-size:13px;text-transform:uppercase;'
            'letter-spacing:1px;margin:24px 0 10px">🤖 AI Top Picks</h2>'
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;background:#0f1520;border:1px solid #2a3a55;'
            'border-radius:6px;font-size:13px">'
            '<tr style="background:#182030">'
            '<th style="padding:7px 10px;text-align:left;color:#4a5568;font-size:11px">#</th>'
            '<th style="padding:7px 10px;text-align:left;color:#4a5568;font-size:11px">ITEM</th>'
            '<th style="padding:7px 10px;text-align:left;color:#4a5568;font-size:11px">TARGET</th>'
            '<th style="padding:7px 10px;text-align:left;color:#4a5568;font-size:11px">REASONING</th>'
            f'</tr>{rows}</table>'
        )

    # Red flags line
    flags_html = ""
    if ai.get("red_flags"):
        flags_html = (
            '<p style="color:#fb923c;font-size:12px;margin:8px 0 0">⚠ Flagged: '
            + " &nbsp;·&nbsp; ".join(
                f'{f["item"]} — {f["issue"]}' for f in ai["red_flags"]
            )
            + "</p>"
        )

    # Per-hub opportunity tables
    hub_tables = ""
    for hub_name, hub_opps in by_hub.items():
        rows = ""
        for o in hub_opps[:30]:
            mc  = ("#4ade80" if o["margin_pct"] >= 30 else
                   "#e8b84b" if o["margin_pct"] >= 15 else "#fb923c")
            sc  = "#f87171" if (o.get("shortage_ratio") or 0) >= 5 else "#cdd6f4"
            sp  = o.get("supply_price") or o.get("jita_sell_price") or 0
            rows += (
                f'<tr style="border-bottom:1px solid #111827">'
                f'<td style="padding:5px 9px;color:#48cae4">{o["type_name"]}</td>'
                f'<td style="padding:5px 9px;color:#4a5568;font-size:11px">{o["category_name"]}</td>'
                f'<td style="padding:5px 9px;text-align:right">{o["avg_daily_volume"]:.0f}</td>'
                f'<td style="padding:5px 9px;text-align:right;color:{sc}">'
                f'{(o.get("shortage_ratio") or 0):.1f}×</td>'
                f'<td style="padding:5px 9px;text-align:right;color:#7a8ba8">{_isk(sp)} ISK</td>'
                f'<td style="padding:5px 9px;text-align:right">{_isk(o["target_sell_price"])} ISK</td>'
                f'<td style="padding:5px 9px;text-align:right;color:{mc};font-weight:600">'
                f'{o["margin_pct"]:.1f}%</td>'
                f'<td style="padding:5px 9px;text-align:right;color:#e8b84b">'
                f'{_isk(o.get("est_daily_profit", 0))} ISK</td>'
                f'</tr>'
            )
        hub_tables += (
            f'<h3 style="color:#cdd6f4;font-size:13px;font-weight:600;margin:20px 0 8px">'
            f'→ {_hub_short(hub_name)}'
            f'<span style="color:#4a5568;font-size:11px;font-weight:normal">'
            f' ({len(hub_opps)} opportunities)</span></h3>'
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;background:#0f1520;border:1px solid #2a3a55;'
            'border-radius:6px;font-size:12px">'
            '<tr style="background:#182030">'
            '<th style="padding:6px 9px;text-align:left;color:#4a5568;font-size:10px">ITEM</th>'
            '<th style="padding:6px 9px;text-align:left;color:#4a5568;font-size:10px">CAT</th>'
            '<th style="padding:6px 9px;text-align:right;color:#4a5568;font-size:10px">VOL/DAY</th>'
            '<th style="padding:6px 9px;text-align:right;color:#4a5568;font-size:10px">SHORTAGE</th>'
            '<th style="padding:6px 9px;text-align:right;color:#4a5568;font-size:10px">BUY</th>'
            '<th style="padding:6px 9px;text-align:right;color:#4a5568;font-size:10px">SELL</th>'
            '<th style="padding:6px 9px;text-align:right;color:#4a5568;font-size:10px">MARGIN</th>'
            '<th style="padding:6px 9px;text-align:right;color:#4a5568;font-size:10px">EST.DAILY</th>'
            f'</tr>{rows}</table>'
        )

    summary_html = ""
    if ai.get("summary"):
        summary_html = (
            f'<p style="color:#cdd6f4;line-height:1.7;font-size:14px;'
            f'margin:0 0 16px;padding:14px 16px;background:#0f1520;'
            f'border-left:3px solid #e8b84b;border-radius:0 4px 4px 0">'
            f'{ai["summary"]}</p>'
        )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#07090f;font-family:'Segoe UI',Arial,sans-serif;color:#cdd6f4">
<div style="max-width:920px;margin:0 auto;padding:20px 16px">

  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#0f1520;border:1px solid #2a3a55;border-radius:8px;
                margin-bottom:20px">
    <tr>
      <td style="padding:18px 22px">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td><span style="font-size:20px;font-weight:700;color:#e8b84b;letter-spacing:1px">EVE GURU 2</span></td>
          <td align="right"><span style="color:#4a5568;font-size:12px">{date_str}</span></td>
        </tr></table>
        <div style="margin-top:6px;font-size:15px;font-weight:600;color:#cdd6f4">
          {hub_short} Supply — Daily Market Opportunity Report
        </div>
      </td>
    </tr>
  </table>

  <table width="100%" cellpadding="0" cellspacing="8" style="margin-bottom:20px"><tr>
    <td width="33%" style="padding-right:6px">
      <div style="background:#0f1520;border:1px solid #2a3a55;border-radius:6px;padding:12px 16px">
        <div style="font-size:10px;color:#4a5568;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px">Opportunities</div>
        <div style="font-size:22px;font-weight:700;color:#48cae4">{len(opps)}</div>
      </div>
    </td>
    <td width="33%" style="padding:0 3px">
      <div style="background:#0f1520;border:1px solid #2a3a55;border-radius:6px;padding:12px 16px">
        <div style="font-size:10px;color:#4a5568;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px">Best Margin</div>
        <div style="font-size:22px;font-weight:700;color:#4ade80">{best_m:.1f}%</div>
      </div>
    </td>
    <td width="33%" style="padding-left:6px">
      <div style="background:#0f1520;border:1px solid #2a3a55;border-radius:6px;padding:12px 16px">
        <div style="font-size:10px;color:#4a5568;text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px">Est. Daily Profit</div>
        <div style="font-size:22px;font-weight:700;color:#e8b84b">{_isk(total_p)} ISK</div>
      </div>
    </td>
  </tr></table>

  {summary_html}
  {picks_html}
  {flags_html}

  <h2 style="color:#e8b84b;font-size:13px;text-transform:uppercase;
             letter-spacing:1px;margin:28px 0 4px">All Opportunities</h2>
  <p style="color:#4a5568;font-size:11px;margin:0 0 12px">
    Showing top 30 per target hub · Min margin {config.MIN_MARGIN_PCT:.0f}% ·
    Prices from live orders (15-min window)
  </p>
  {hub_tables}

  <div style="margin-top:24px;padding-top:14px;border-top:1px solid #2a3a55;
              color:#4a5568;font-size:11px;line-height:1.6">
    Generated by EVEGuru2 &nbsp;·&nbsp;
    Live ESI market data &nbsp;·&nbsp;
    AI analysis by Claude &nbsp;·&nbsp;
    Not financial advice
  </div>
</div>
</body></html>"""


# ── Email sending ──────────────────────────────────────────────────────────────

async def _send(subject: str, html: str) -> None:
    if not config.REPORT_TO:
        log.warning("[report] REPORT_TO not configured — email not sent")
        return
    if not config.SMTP_HOST:
        log.warning("[report] SMTP_HOST not configured — email not sent")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.SMTP_FROM
    msg["To"]      = config.REPORT_TO
    msg.attach(MIMEText(html, "html", "utf-8"))

    use_tls   = config.SMTP_PORT == 465
    start_tls = config.SMTP_PORT == 587

    try:
        await aiosmtplib.send(
            msg,
            hostname=config.SMTP_HOST,
            port=config.SMTP_PORT,
            username=config.SMTP_USER or None,
            password=config.SMTP_PASSWORD or None,
            use_tls=use_tls,
            start_tls=start_tls,
        )
        log.info("[report] Sent: %s → %s", subject, config.REPORT_TO)
    except Exception as exc:
        log.error("[report] Failed to send '%s': %s", subject, exc)


# ── Orchestration ──────────────────────────────────────────────────────────────

async def run_once() -> None:
    """Generate and email one report per supply hub."""
    log.info("[report] Daily report run starting")
    now = datetime.now(timezone.utc)

    for supply_hub in config.HUBS:
        target_hubs = [h for h in config.HUBS if h.station_id != supply_hub.station_id]
        hub_short   = _hub_short(supply_hub.name)

        try:
            if supply_hub.is_supply:
                opps = await _jita_opportunities()
            else:
                opps = await _hub_opportunities(supply_hub, target_hubs)

            if not opps:
                log.info("[report] %s: no qualifying opportunities — skipping", hub_short)
                continue

            log.info("[report] %s: %d opportunities → AI analysis …", hub_short, len(opps))
            ai_result = await _ai_analyse(opps, hub_short)

            html    = _build_html(supply_hub, opps, ai_result, now)
            subject = (f"EVEGuru2 — {hub_short} Supply Report — "
                       f"{now.strftime('%d %b %Y')}")
            await _send(subject, html)

        except Exception:
            log.exception("[report] Error generating report for %s", hub_short)

    log.info("[report] Daily reports complete")
