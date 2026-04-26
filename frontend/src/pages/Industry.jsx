import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { isk, num } from '../utils/format'

// ── EVE manufacturing formula ──────────────────────────────────────────────────
// For N runs with ME level (0–10) and facility bonus (%):
//   adjusted = max(runs, ceil(baseQty × runs × (1 - me/100) × (1 - facilityMe/100)))
function calcAdjustedQty(baseQty, runs, me, facilityMePct) {
  const factor = (1 - me / 100) * (1 - facilityMePct / 100)
  return Math.max(runs, Math.ceil(baseQty * runs * factor))
}

// Facility presets: [label, me_bonus_%]
const FACILITIES = [
  ['NPC Station',                    0.0],
  ['Citadel (no rig)',               0.0],
  ['Citadel (T1 ME rig)',            2.0],
  ['Citadel (T2 ME rig)',            2.4],
  ['Engineering Complex (no rig)',   1.0],
  ['Engineering Complex (T1 rig)',   3.0],
  ['Engineering Complex (T2 rig)',   3.4],
]

const DEFAULT_SELL_OVERHEAD = 0.066  // fallback if /api/config not yet loaded

function secondsToHMS(s) {
  if (!s) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return [h && `${h}h`, m && `${m}m`, sec && `${sec}s`].filter(Boolean).join(' ') || '0s'
}

// Apply TE (time efficiency) reduction: base × (1 - te/100) × facility_time_bonus
function calcBuildTime(baseSeconds, te, facilityIdx) {
  // Engineering complexes give 15% time reduction; structures 0%
  const structureBonus = facilityIdx >= 4 ? 0.85 : 1.0
  return Math.ceil(baseSeconds * (1 - te / 100) * structureBonus)
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function SearchBox({ onSelect }) {
  const [query, setQuery]       = useState('')
  const [results, setResults]   = useState([])
  const [loading, setLoading]   = useState(false)
  const [focused, setFocused]   = useState(false)
  const [searchErr, setSearchErr] = useState(null)
  const timer                   = useRef(null)

  const search = useCallback((q) => {
    if (q.length < 2) { setResults([]); setSearchErr(null); return }
    setLoading(true)
    setSearchErr(null)
    fetch(`/api/industry/search?q=${encodeURIComponent(q)}`)
      .then(r => {
        if (!r.ok) throw new Error(`API ${r.status}: ${r.statusText}`)
        return r.json()
      })
      .then(data => { setResults(data); if (data.length === 0) setSearchErr('No blueprints found — blueprint data may not be loaded yet (run: docker compose run --rm sde)') })
      .catch(e => { setResults([]); setSearchErr(e.message) })
      .finally(() => setLoading(false))
  }, [])

  const handleChange = (e) => {
    const q = e.target.value
    setQuery(q)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => search(q), 300)
  }

  const handlePick = (bp) => {
    setQuery(bp.product_name)
    setResults([])
    onSelect(bp)
  }

  return (
    <div className="ind-search-wrap">
      <input
        className="ind-search-input"
        placeholder="Search by item name (e.g. Scorch S, Rifter, Tritanium)…"
        value={query}
        onChange={handleChange}
        onFocus={() => setFocused(true)}
        onBlur={() => setTimeout(() => setFocused(false), 200)}
        autoComplete="off"
      />
      {loading && <span className="ind-search-spinner">…</span>}
      {focused && results.length > 0 && (
        <ul className="ind-search-results">
          {results.map(bp => (
            <li key={bp.blueprint_type_id} onClick={() => handlePick(bp)}>
              <span className="ind-res-name">{bp.product_name}</span>
              <span className="ind-res-meta">{bp.category_name} · {bp.group_name}</span>
            </li>
          ))}
        </ul>
      )}
      {searchErr && <div className="ind-search-error">{searchErr}</div>}
    </div>
  )
}

function CalcControls({ me, setMe, te, setTe, runs, setRuns, facilityIdx, setFacilityIdx }) {
  return (
    <div className="ind-controls">
      <label>
        <span>ME</span>
        <input type="number" min="0" max="10" value={me}
          onChange={e => setMe(Math.min(10, Math.max(0, +e.target.value)))} />
      </label>
      <label>
        <span>TE</span>
        <input type="number" min="0" max="20" value={te}
          onChange={e => setTe(Math.min(20, Math.max(0, +e.target.value)))} />
      </label>
      <label>
        <span>Runs</span>
        <input type="number" min="1" max="100000" value={runs}
          onChange={e => setRuns(Math.max(1, +e.target.value))} />
      </label>
      <label style={{ flex: '0 0 auto' }}>
        <span>Facility</span>
        <select className="filter-select"
          value={facilityIdx}
          onChange={e => setFacilityIdx(+e.target.value)}>
          {FACILITIES.map(([label], i) => (
            <option key={i} value={i}>{label}</option>
          ))}
        </select>
      </label>
    </div>
  )
}

function MaterialsTable({ materials, prices, runs, me, facilityMePct, totalCost, buildSeconds }) {
  if (!materials.length) return null
  return (
    <div className="ind-section">
      <div className="ind-section-header">
        <h3>Materials ({runs} run{runs !== 1 ? 's' : ''})</h3>
        <span className="ind-build-time">Build time: {secondsToHMS(buildSeconds)}</span>
      </div>
      <table className="ind-table">
        <thead>
          <tr>
            <th style={{ textAlign: 'left' }}>Material</th>
            <th>Base qty</th>
            <th>Adjusted qty</th>
            <th>Unit price (Jita)</th>
            <th>Line cost</th>
            <th>% of total</th>
          </tr>
        </thead>
        <tbody>
          {materials.map(m => {
            const adjQty  = calcAdjustedQty(m.base_quantity, runs, me, facilityMePct)
            const price   = prices[m.material_type_id] ?? 0
            const lineCost = adjQty * price
            const share   = totalCost > 0 ? (lineCost / totalCost) * 100 : 0
            return (
              <tr key={m.material_type_id}>
                <td style={{ color: 'var(--cyan)' }}>{m.name}</td>
                <td style={{ textAlign: 'right' }}>{num(m.base_quantity * runs, 0)}</td>
                <td style={{ textAlign: 'right', color: adjQty < m.base_quantity * runs ? 'var(--green)' : 'var(--text-primary)' }}>
                  {num(adjQty, 0)}
                </td>
                <td style={{ textAlign: 'right', color: 'var(--text-dim)' }}>
                  {price ? isk(price) + ' ISK' : <span style={{ color: 'var(--text-muted)' }}>no data</span>}
                </td>
                <td style={{ textAlign: 'right' }}>{price ? isk(lineCost) + ' ISK' : '—'}</td>
                <td style={{ textAlign: 'right', color: 'var(--text-muted)' }}>
                  {price ? share.toFixed(1) + '%' : '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={4} style={{ textAlign: 'right', color: 'var(--text-dim)', paddingTop: 8 }}>
              Total material cost ({runs} run{runs !== 1 ? 's' : ''})
            </td>
            <td style={{ textAlign: 'right', color: 'var(--gold)', fontWeight: 700, paddingTop: 8 }}>
              {isk(totalCost)} ISK
            </td>
            <td />
          </tr>
        </tfoot>
      </table>
    </div>
  )
}

function HubComparison({ hubPrices, costPerUnit, productQtyPerRun, sellOverhead }) {
  if (!hubPrices.length) return null
  return (
    <div className="ind-section">
      <h3>Market comparison (per unit)</h3>
      <table className="ind-table">
        <thead>
          <tr>
            <th style={{ textAlign: 'left' }}>Hub</th>
            <th>Sell price</th>
            <th>Net after fees</th>
            <th>Profit / unit</th>
            <th>Margin %</th>
            <th>Profit ({productQtyPerRun} units/run)</th>
          </tr>
        </thead>
        <tbody>
          {hubPrices.map(h => {
            const sell    = h.sell_price
            if (!sell) return (
              <tr key={h.hub_name}>
                <td style={{ color: 'var(--text-dim)' }}>{h.hub_name}</td>
                <td colSpan={5} style={{ color: 'var(--text-muted)', textAlign: 'center' }}>no market data</td>
              </tr>
            )
            const net     = sell * (1 - sellOverhead)
            const profit  = net - costPerUnit
            const margin  = costPerUnit > 0 ? (profit / costPerUnit) * 100 : 0
            const color   = margin >= 20 ? '#4ade80' : margin >= 10 ? '#e8b84b' : margin >= 0 ? '#fb923c' : '#f87171'
            return (
              <tr key={h.hub_name}>
                <td style={{ color: h.hub_name === 'Jita' ? 'var(--text-muted)' : 'var(--text-primary)' }}>
                  {h.hub_name}
                  {h.hub_name === 'Jita' && <span style={{ color: 'var(--text-muted)', fontSize: 11, marginLeft: 4 }}>(supply)</span>}
                </td>
                <td style={{ textAlign: 'right' }}>{isk(sell)} ISK</td>
                <td style={{ textAlign: 'right', color: 'var(--text-dim)' }}>{isk(net)} ISK</td>
                <td style={{ textAlign: 'right', color }}>
                  {profit >= 0 ? '+' : ''}{isk(profit)} ISK
                </td>
                <td style={{ textAlign: 'right', color, fontWeight: Math.abs(margin) >= 10 ? 600 : 400 }}>
                  {margin.toFixed(1)}%
                </td>
                <td style={{ textAlign: 'right', color: 'var(--gold)' }}>
                  {profit > 0 ? isk(profit * productQtyPerRun) + ' ISK' : '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 8 }}>
        Net revenue = sell price × (1 − {(sellOverhead * 100).toFixed(1)}% broker + sales tax). Adjust BROKER_FEE_PCT / SALES_TAX_PCT in .env.
      </p>
    </div>
  )
}

// ── Main page ──────────────────────────────────────────────────────────────────

export default function Industry() {
  const [blueprint, setBlueprint]     = useState(null)   // full detail from API
  const [prices, setPrices]           = useState({})     // material type_id → Jita price
  const [hubPrices, setHubPrices]     = useState([])     // per-hub sell prices for product
  const [loadingBp, setLoadingBp]     = useState(false)
  const [sellOverhead, setSellOverhead] = useState(DEFAULT_SELL_OVERHEAD)
  const [me, setMe]                   = useState(0)
  const [te, setTe]                   = useState(0)
  const [runs, setRuns]               = useState(1)
  const [facilityIdx, setFacilityIdx] = useState(0)

  // Load fee config from server once on mount
  useEffect(() => {
    fetch('/api/config')
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data?.sell_overhead_pct != null) setSellOverhead(data.sell_overhead_pct) })
      .catch(() => {})
  }, [])

  const facilityMePct = FACILITIES[facilityIdx][1]

  // Load blueprint detail + prices when a search result is picked
  const handleSelect = useCallback(async (searchResult) => {
    setLoadingBp(true)
    setPrices({})
    setHubPrices([])
    try {
      const detail = await fetch(`/api/industry/blueprint/${searchResult.blueprint_type_id}`)
        .then(r => r.json())
      setBlueprint(detail)

      // Fetch Jita prices for all materials in parallel
      if (detail.materials?.length) {
        const ids = detail.materials.map(m => m.material_type_id).join(',')
        const [matPrices, hubs] = await Promise.all([
          fetch(`/api/industry/prices?type_ids=${ids}`).then(r => r.json()),
          fetch(`/api/industry/hub-prices/${detail.product_type_id}`).then(r => r.json()),
        ])
        // API returns string keys
        const numericPrices = Object.fromEntries(
          Object.entries(matPrices).map(([k, v]) => [parseInt(k), v])
        )
        setPrices(numericPrices)
        setHubPrices(hubs)
      }
    } catch (e) {
      console.error(e)
    } finally {
      setLoadingBp(false)
    }
  }, [])

  // Derived calculations
  const { totalCost, costPerUnit, buildSeconds } = useMemo(() => {
    if (!blueprint?.materials) return { totalCost: 0, costPerUnit: 0, buildSeconds: 0 }
    let total = 0
    for (const m of blueprint.materials) {
      const adjQty = calcAdjustedQty(m.base_quantity, runs, me, facilityMePct)
      const price  = prices[m.material_type_id] ?? 0
      total += adjQty * price
    }
    const produced   = blueprint.product_qty * runs
    const perUnit    = produced > 0 ? total / produced : 0
    const buildSecs  = calcBuildTime(blueprint.base_time_seconds, te, facilityIdx) * runs
    return { totalCost: total, costPerUnit: perUnit, buildSeconds: buildSecs }
  }, [blueprint, prices, runs, me, te, facilityIdx, facilityMePct])

  const productQtyPerRun = blueprint ? blueprint.product_qty * runs : 0

  return (
    <div className="page ind-page">
      {/* ── Search ── */}
      <div className="ind-search-row">
        <h2 className="ind-title">Industry Calculator</h2>
        <SearchBox onSelect={handleSelect} />
      </div>

      {/* ── Loading ── */}
      {loadingBp && <div className="loading">Loading blueprint data…</div>}

      {/* ── Blueprint not yet selected ── */}
      {!blueprint && !loadingBp && (
        <div className="ind-empty">
          <div style={{ fontSize: 48, marginBottom: 12 }}>⚙️</div>
          <p>Search for any manufacturable item above.</p>
          <p style={{ color: 'var(--text-muted)', marginTop: 8, fontSize: 12 }}>
            Prices use live Jita sell orders (15-min cache), falling back to 30-day history average.
          </p>
        </div>
      )}

      {/* ── Blueprint detail ── */}
      {blueprint && !loadingBp && (
        <>
          {/* Header cards */}
          <div className="ind-header-row">
            <div className="ind-bp-card">
              <div className="ind-bp-title">{blueprint.product_name}</div>
              <div className="ind-bp-sub">
                {blueprint.category_name} · {blueprint.group_name}
              </div>
              <div className="ind-bp-meta">
                <span>Blueprint: <em>{blueprint.blueprint_name}</em></span>
                <span>Produces: <strong>{blueprint.product_qty} unit{blueprint.product_qty !== 1 ? 's' : ''} per run</strong></span>
                <span>Base build time: <strong>{secondsToHMS(blueprint.base_time_seconds)}</strong></span>
                <span>Vol/unit: <strong>{blueprint.product_volume?.toFixed(2)} m³</strong></span>
              </div>
            </div>

            <div className="ind-summary-cards">
              <div className="detail-card">
                <div className="label">Total material cost</div>
                <div className="val">{isk(totalCost)} ISK</div>
              </div>
              <div className="detail-card">
                <div className="label">Cost per unit</div>
                <div className="val cyan">{isk(costPerUnit)} ISK</div>
              </div>
              <div className="detail-card">
                <div className="label">Units produced</div>
                <div className="val">{num(productQtyPerRun, 0)}</div>
              </div>
              <div className="detail-card">
                <div className="label">Build time</div>
                <div className="val" style={{ fontSize: 16 }}>{secondsToHMS(buildSeconds)}</div>
              </div>
            </div>
          </div>

          {/* Controls */}
          <CalcControls
            me={me} setMe={setMe}
            te={te} setTe={setTe}
            runs={runs} setRuns={setRuns}
            facilityIdx={facilityIdx} setFacilityIdx={setFacilityIdx}
          />

          {/* Materials */}
          <MaterialsTable
            materials={blueprint.materials}
            prices={prices}
            runs={runs}
            me={me}
            facilityMePct={facilityMePct}
            totalCost={totalCost}
            buildSeconds={buildSeconds}
          />

          {/* Hub comparison */}
          <HubComparison
            hubPrices={hubPrices}
            costPerUnit={costPerUnit}
            productQtyPerRun={productQtyPerRun}
            sellOverhead={sellOverhead}
          />
        </>
      )}
    </div>
  )
}
