import React, { useEffect, useState } from 'react'
import { Routes, Route, NavLink } from 'react-router-dom'
import Opportunities from './pages/Opportunities'
import ItemDetail from './pages/ItemDetail'
import Dashboard from './pages/Dashboard'
import { isk, num, relativeTime } from './utils/format'

function Navbar({ stats }) {
  return (
    <nav className="navbar">
      <span className="navbar-brand">EVE GURU 2</span>
      <NavLink to="/"          className={({ isActive }) => 'nav-link' + (isActive ? ' active' : '')}>Opportunities</NavLink>
      <NavLink to="/dashboard" className={({ isActive }) => 'nav-link' + (isActive ? ' active' : '')}>Dashboard</NavLink>
      {stats && (
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 24, fontSize: 12, color: 'var(--text-dim)' }}>
          <span><span style={{ color: 'var(--cyan)' }}>{num(stats.active_count)}</span> opportunities</span>
          <span>Best: <span style={{ color: 'var(--green)' }}>{stats.best_margin?.toFixed(1)}%</span></span>
          <span>Est. daily: <span style={{ color: 'var(--gold)' }}>{isk(stats.total_daily_profit)} ISK</span></span>
          {stats.last_scan && <span style={{ color: 'var(--text-muted)' }}>Updated {relativeTime(stats.last_scan)}</span>}
        </div>
      )}
    </nav>
  )
}

export default function App() {
  const [stats, setStats] = useState(null)

  const fetchStats = () =>
    fetch('/api/stats').then(r => r.json()).then(setStats).catch(() => {})

  useEffect(() => {
    fetchStats()
    const id = setInterval(fetchStats, 60000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="layout">
      <Navbar stats={stats} />
      <Routes>
        <Route path="/"                       element={<Opportunities />} />
        <Route path="/dashboard"              element={<Dashboard stats={stats} />} />
        <Route path="/item/:typeId/:stationId" element={<ItemDetail />} />
      </Routes>
    </div>
  )
}
