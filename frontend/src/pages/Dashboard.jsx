import React, { useEffect, useState } from 'react'
import { isk, num, pct, relativeTime } from '../utils/format'

export default function Dashboard({ stats }) {
  const [recentOpps, setRecentOpps] = useState([])

  useEffect(() => {
    fetch('/api/opportunities?limit=10')
      .then(r => r.json())
      .then(setRecentOpps)
      .catch(() => {})
  }, [])

  return (
    <div className="page">
      <div className="stat-cards">
        <div className="stat-card">
          <div className="label">Active Opportunities</div>
          <div className="value cyan">{stats ? num(stats.active_count) : '—'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Best Margin</div>
          <div className="value green">{stats ? pct(stats.best_margin) : '—'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Est. Daily ISK</div>
          <div className="value">{stats ? isk(stats.total_daily_profit) + ' ISK' : '—'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Active Hubs</div>
          <div className="value cyan">{stats ? num(stats.hub_count) : '—'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Last Scan</div>
          <div className="value" style={{ fontSize: 16, color: 'var(--text-dim)' }}>
            {stats?.last_scan ? relativeTime(stats.last_scan) : '—'}
          </div>
        </div>
      </div>

      <div>
        <div className="section-title">Top opportunities right now</div>
        <div style={{ background: 'var(--bg-surface)', border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                {['Item', 'Hub', 'Margin', 'Jita Price', 'Hub Price', 'Est. Daily ISK', 'Found'].map(h => (
                  <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.6px', color: 'var(--text-muted)', borderBottom: '1px solid var(--border)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {recentOpps.length === 0
                ? <tr><td colSpan={7} style={{ padding: 24, color: 'var(--text-muted)', textAlign: 'center' }}>No opportunities yet — scans run every 5 minutes</td></tr>
                : recentOpps.map(o => (
                  <tr key={o.id} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td style={{ padding: '9px 14px', color: 'var(--cyan)' }}>{o.type_name}</td>
                    <td style={{ padding: '9px 14px', color: 'var(--text-dim)', fontSize: 12 }}>{o.target_hub_name?.replace(/\s*-.*$/, '')}</td>
                    <td style={{ padding: '9px 14px', color: o.margin_pct >= 20 ? 'var(--green)' : 'var(--gold)', fontWeight: 600 }}>{pct(o.margin_pct)}</td>
                    <td style={{ padding: '9px 14px', color: 'var(--text-dim)' }}>{isk(o.jita_sell_price)} ISK</td>
                    <td style={{ padding: '9px 14px' }}>{isk(o.target_sell_price)} ISK</td>
                    <td style={{ padding: '9px 14px', color: 'var(--gold)' }}>{isk(o.estimated_daily_profit)} ISK</td>
                    <td style={{ padding: '9px 14px', color: 'var(--text-muted)', fontSize: 12 }}>{relativeTime(o.detected_at)}</td>
                  </tr>
                ))
              }
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
