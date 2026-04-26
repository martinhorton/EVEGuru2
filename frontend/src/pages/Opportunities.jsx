import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AgGridReact } from 'ag-grid-react'
import 'ag-grid-community/styles/ag-grid.css'
import 'ag-grid-community/styles/ag-theme-quartz.css'
import { isk, num, pct, relativeTime } from '../utils/format'

const HUBS = ['All', 'Amarr', 'Dodixie', 'Rens', 'Hek']

function marginStyle(params) {
  const v = params.value
  if (v == null) return {}
  if (v >= 30)  return { color: '#4ade80', fontWeight: 600 }
  if (v >= 15)  return { color: '#86efac' }
  if (v >= 10)  return { color: '#e8b84b' }
  return { color: '#fb923c' }
}

function dosStyle(params) {
  // D.O.S. = days of supply.  Low = shortage (good opportunity), high = oversupplied.
  const v = params.value
  if (v == null) return {}
  if (v < 1)  return { color: '#f87171', fontWeight: 600 }  // under 1 day — critical shortage
  if (v < 5)  return { color: '#fb923c' }                   // under 5 days — low stock
  if (v < 14) return { color: '#e8b84b' }                   // under 2 weeks — moderate
  return { color: 'var(--text-dim)' }                       // well supplied
}

const COL_DEFS = [
  {
    field: 'type_name', headerName: 'Item', flex: 2, minWidth: 180,
    cellStyle: { color: 'var(--cyan)', cursor: 'pointer' },
    filter: 'agTextColumnFilter',
  },
  {
    field: 'category_name', headerName: 'Category', width: 130,
    filter: 'agTextColumnFilter',
    cellStyle: { color: 'var(--text-dim)', fontSize: 12 },
  },
  {
    field: 'group_name', headerName: 'Group', width: 140,
    filter: 'agTextColumnFilter',
    cellStyle: { color: 'var(--text-dim)', fontSize: 12 },
  },
  {
    field: 'target_hub_name', headerName: 'Hub', width: 120,
    valueFormatter: p => p.value?.replace(/\s*-.*$/, '') ?? '—',
    filter: 'agTextColumnFilter',
  },
  {
    field: 'avg_daily_volume', headerName: 'Avg Vol/day', width: 120,
    valueFormatter: p => num(p.value, 0),
    type: 'numericColumn',
  },
  {
    field: 'current_supply_units', headerName: 'Supply', width: 100,
    valueFormatter: p => num(p.value, 0),
    type: 'numericColumn',
  },
  {
    field: 'shortage_ratio', headerName: 'D.O.S.', width: 90,
    valueFormatter: p => p.value != null ? p.value.toFixed(1) + 'd' : '—',
    cellStyle: dosStyle,
    type: 'numericColumn',
  },
  {
    field: 'jita_sell_price', headerName: 'Jita Sell', width: 120,
    valueFormatter: p => isk(p.value) + ' ISK',
    type: 'numericColumn',
    cellStyle: { color: 'var(--text-dim)' },
  },
  {
    field: 'target_sell_price', headerName: 'Hub Sell', width: 120,
    valueFormatter: p => isk(p.value) + ' ISK',
    type: 'numericColumn',
  },
  {
    field: 'total_cost', headerName: 'Total Cost', width: 120,
    valueFormatter: p => isk(p.value) + ' ISK',
    type: 'numericColumn',
    cellStyle: { color: 'var(--text-dim)' },
  },
  {
    field: 'margin_pct', headerName: 'Margin %', width: 110,
    valueFormatter: p => pct(p.value),
    cellStyle: marginStyle,
    type: 'numericColumn',
    sort: 'desc',
  },
  {
    field: 'estimated_daily_profit', headerName: 'Est. Daily ISK', width: 140,
    valueFormatter: p => isk(p.value) + ' ISK',
    cellStyle: { color: 'var(--gold)' },
    type: 'numericColumn',
  },
  {
    field: 'detected_at', headerName: 'Found', width: 100,
    valueFormatter: p => relativeTime(p.value),
    cellStyle: { color: 'var(--text-muted)' },
  },
]

export default function Opportunities() {
  const [rows, setRows]               = useState([])
  const [loading, setLoading]         = useState(true)
  const [error, setError]             = useState(null)
  const [hub, setHub]                 = useState('All')
  const [minMargin, setMinMargin]     = useState(10)
  const [categories, setCategories]   = useState([])
  const [selectedCat, setSelectedCat] = useState('')
  const [groups, setGroups]           = useState([])
  const [selectedGroup, setSelectedGroup] = useState('')
  const navigate = useNavigate()
  const gridRef  = useRef()

  // Load category list on mount
  useEffect(() => {
    fetch('/api/categories')
      .then(r => r.ok ? r.json() : [])
      .then(data => setCategories(data))
      .catch(() => {})
  }, [])

  // Load groups when category changes
  useEffect(() => {
    setSelectedGroup('')
    setGroups([])
    if (!selectedCat) return
    fetch(`/api/categories/${selectedCat}/groups`)
      .then(r => r.ok ? r.json() : [])
      .then(data => setGroups(data))
      .catch(() => {})
  }, [selectedCat])

  const fetchData = useCallback(() => {
    setLoading(true)
    const params = new URLSearchParams({ min_margin: minMargin })
    if (hub !== 'All')   params.set('hub', hub)
    if (selectedCat)     params.set('category_id', selectedCat)
    if (selectedGroup)   params.set('group_id', selectedGroup)
    fetch('/api/opportunities?' + params)
      .then(r => { if (!r.ok) throw new Error(r.statusText); return r.json() })
      .then(data => { setRows(data); setError(null) })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [hub, minMargin, selectedCat, selectedGroup])

  useEffect(() => { fetchData() }, [fetchData])

  useEffect(() => {
    const id = setInterval(fetchData, 5 * 60 * 1000)
    return () => clearInterval(id)
  }, [fetchData])

  const onRowClicked = useCallback(({ data }) => {
    navigate(`/item/${data.type_id}/${data.target_station_id}`, {
      state: { opp: data }
    })
  }, [navigate])

  const defaultColDef = useMemo(() => ({
    sortable: true,
    resizable: true,
    suppressMovable: false,
    filterParams: { buttons: ['reset'] },
  }), [])

  return (
    <div className="page">
      <div className="filter-bar">
        <span className="filter-label">Hub:</span>
        {HUBS.map(h => (
          <button key={h} className={'hub-btn' + (hub === h ? ' active' : '')} onClick={() => setHub(h)}>{h}</button>
        ))}

        <span className="filter-label" style={{ marginLeft: 16 }}>Category:</span>
        <select
          className="filter-select"
          value={selectedCat}
          onChange={e => setSelectedCat(e.target.value)}
        >
          <option value="">All</option>
          {categories.map(c => (
            <option key={c.category_id} value={c.category_id}>{c.category_name}</option>
          ))}
        </select>

        {groups.length > 0 && (
          <>
            <span className="filter-label">Group:</span>
            <select
              className="filter-select"
              value={selectedGroup}
              onChange={e => setSelectedGroup(e.target.value)}
            >
              <option value="">All</option>
              {groups.map(g => (
                <option key={g.group_id} value={g.group_id}>{g.group_name}</option>
              ))}
            </select>
          </>
        )}

        <span className="filter-label" style={{ marginLeft: 16 }}>Min margin:</span>
        <input
          className="margin-input"
          type="number" min="0" max="100" step="1"
          value={minMargin}
          onChange={e => setMinMargin(Number(e.target.value))}
        />
        <span className="filter-label">%</span>
        <button className="refresh-btn" onClick={fetchData}>↻ Refresh</button>
        {!loading && <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>{rows.length} opportunities</span>}
      </div>

      {error && <div className="error-msg">Failed to load: {error}</div>}

      <div className="grid-wrap">
        <AgGridReact
          ref={gridRef}
          className="ag-theme-quartz-dark"
          style={{ height: '100%', width: '100%' }}
          rowData={rows}
          columnDefs={COL_DEFS}
          defaultColDef={defaultColDef}
          rowClass="clickable-row"
          onRowClicked={onRowClicked}
          animateRows
          pagination
          paginationPageSize={100}
          loading={loading}
        />
      </div>
    </div>
  )
}
