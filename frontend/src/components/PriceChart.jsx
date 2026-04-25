import React, { useMemo } from 'react'
import {
  ComposedChart, Line, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer
} from 'recharts'
import { isk } from '../utils/format'

// region_id → short label
function regionLabel(regionId) {
  const map = {
    10000002: 'Jita',
    10000043: 'Amarr',
    10000032: 'Dodixie',
    10000030: 'Rens',
    10000042: 'Hek',
  }
  return map[regionId] ?? `Region ${regionId}`
}

const REGION_COLORS = {
  10000002: '#48cae4',  // Jita — cyan
  10000043: '#e8b84b',  // Amarr — gold
  10000032: '#4ade80',  // Dodixie — green
  10000030: '#c084fc',  // Rens — purple
  10000042: '#fb923c',  // Hek — orange
}

export default function PriceChart({ history, targetRegionId }) {
  const { chartData, regions } = useMemo(() => {
    if (!history?.length) return { chartData: [], regions: [] }

    // Collect all unique region_ids and dates
    const regionSet = [...new Set(history.map(r => r.region_id))]
    const dateMap = {}

    for (const row of history) {
      if (!dateMap[row.date]) dateMap[row.date] = { date: row.date }
      dateMap[row.date][`price_${row.region_id}`]  = row.average
      dateMap[row.date][`volume_${row.region_id}`] = row.volume
    }

    const chartData = Object.values(dateMap).sort((a, b) => a.date.localeCompare(b.date))
    return { chartData, regions: regionSet }
  }, [history])

  const CustomTooltip = ({ active, payload, label }) => {
    if (!active || !payload?.length) return null
    return (
      <div style={{
        background: 'var(--bg-elevated)', border: '1px solid var(--border)',
        borderRadius: 6, padding: '10px 14px', fontSize: 12
      }}>
        <div style={{ color: 'var(--text-dim)', marginBottom: 6 }}>{label}</div>
        {payload.map(p => (
          <div key={p.dataKey} style={{ color: p.color, marginBottom: 2 }}>
            {p.name}: {p.dataKey.startsWith('volume') ? p.value?.toLocaleString() : isk(p.value) + ' ISK'}
          </div>
        ))}
      </div>
    )
  }

  if (!chartData.length) {
    return <div className="empty-state">No history data yet — check back after the first daily scan.</div>
  }

  // Show volume bars for the target region only, to keep the chart readable
  const volKey = `volume_${targetRegionId}`

  return (
    <ResponsiveContainer width="100%" height={320}>
      <ComposedChart data={chartData} margin={{ top: 4, right: 20, left: 10, bottom: 4 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
        <XAxis
          dataKey="date"
          tick={{ fill: 'var(--text-dim)', fontSize: 11 }}
          tickFormatter={d => d.slice(5)}
        />
        <YAxis
          yAxisId="price"
          orientation="left"
          tick={{ fill: 'var(--text-dim)', fontSize: 11 }}
          tickFormatter={v => isk(v)}
          width={70}
        />
        <YAxis
          yAxisId="vol"
          orientation="right"
          tick={{ fill: 'var(--text-dim)', fontSize: 11 }}
          tickFormatter={v => v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(0) + 'K' : v}
          width={60}
        />
        <Tooltip content={<CustomTooltip />} />
        <Legend
          wrapperStyle={{ fontSize: 12, color: 'var(--text-dim)', paddingTop: 8 }}
        />
        <Bar
          yAxisId="vol"
          dataKey={volKey}
          name="Volume"
          fill="var(--bg-elevated)"
          stroke="var(--border)"
          opacity={0.6}
        />
        {regions.map(rid => (
          <Line
            key={rid}
            yAxisId="price"
            type="monotone"
            dataKey={`price_${rid}`}
            name={regionLabel(rid)}
            stroke={REGION_COLORS[rid] ?? '#888'}
            strokeWidth={2}
            dot={false}
            connectNulls
          />
        ))}
      </ComposedChart>
    </ResponsiveContainer>
  )
}
