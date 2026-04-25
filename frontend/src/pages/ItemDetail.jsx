import React, { useEffect, useState } from 'react'
import { useParams, useLocation, useNavigate } from 'react-router-dom'
import PriceChart from '../components/PriceChart'
import { isk, num, pct, relativeTime } from '../utils/format'

const STATION_NAMES = {
  60003760: 'Jita IV-4',
  60008494: 'Amarr EFA',
  60011866: 'Dodixie Fed Navy',
  60004588: 'Rens Brutor',
  60005686: 'Hek Boundless',
}

const STATION_REGION = {
  60003760: 10000002,
  60008494: 10000043,
  60011866: 10000032,
  60004588: 10000030,
  60005686: 10000042,
}

export default function ItemDetail() {
  const { typeId, stationId } = useParams()
  const { state }             = useLocation()
  const navigate              = useNavigate()
  const opp                   = state?.opp

  const [item,    setItem]    = useState(null)
  const [history, setHistory] = useState([])
  const [orders,  setOrders]  = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    Promise.all([
      fetch(`/api/items/${typeId}`).then(r => r.json()),
      fetch(`/api/items/${typeId}/history`).then(r => r.json()),
      fetch(`/api/items/${typeId}/orders`).then(r => r.json()),
    ])
      .then(([itemData, histData, orderData]) => {
        setItem(itemData)
        setHistory(histData)
        setOrders(orderData)
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [typeId])

  const targetRegionId = STATION_REGION[Number(stationId)] ?? null
  const stationName    = STATION_NAMES[Number(stationId)] ?? `Station ${stationId}`

  const jitaSells   = orders.filter(o => o.location_id === 60003760 && !o.is_buy_order).slice(0, 10)
  const targetSells = orders.filter(o => o.location_id === Number(stationId) && !o.is_buy_order).slice(0, 10)

  if (loading) return <div className="page"><div className="loading">Loading…</div></div>

  return (
    <div className="page" style={{ gap: 18, overflowY: 'auto' }}>
      <div>
        <button className="back-btn" onClick={() => navigate(-1)}>← Back</button>
      </div>

      <div className="detail-header">
        <span className="detail-title">{item?.name ?? `Item ${typeId}`}</span>
        <span className="detail-sub">Jita → {stationName}</span>
      </div>

      {opp && (
        <div className="detail-cards">
          <div className="detail-card">
            <div className="label">Jita Sell</div>
            <div className="val cyan">{isk(opp.jita_sell_price)} ISK</div>
          </div>
          <div className="detail-card">
            <div className="label">Hub Sell</div>
            <div className="val">{isk(opp.target_sell_price)} ISK</div>
          </div>
          <div className="detail-card">
            <div className="label">Shipping / unit</div>
            <div className="val" style={{ color: 'var(--text-dim)' }}>{isk(opp.shipping_cost)} ISK</div>
          </div>
          <div className="detail-card">
            <div className="label">Total Cost</div>
            <div className="val" style={{ color: 'var(--text-dim)' }}>{isk(opp.total_cost)} ISK</div>
          </div>
          <div className="detail-card">
            <div className="label">Margin</div>
            <div className={`val ${opp.margin_pct >= 20 ? 'green' : ''}`}>{pct(opp.margin_pct)}</div>
          </div>
          <div className="detail-card">
            <div className="label">Avg Daily Vol</div>
            <div className="val cyan">{num(opp.avg_daily_volume, 0)}</div>
          </div>
          <div className="detail-card">
            <div className="label">Supply at Hub</div>
            <div className={`val ${opp.current_supply_units < opp.avg_daily_volume ? 'red' : ''}`}>
              {num(opp.current_supply_units, 0)}
            </div>
          </div>
          <div className="detail-card">
            <div className="label">Est. Daily Profit</div>
            <div className="val green">{isk(opp.estimated_daily_profit)} ISK</div>
          </div>
        </div>
      )}

      <div>
        <div className="section-title">30-day price history</div>
        <div className="chart-wrap">
          <PriceChart history={history} targetRegionId={targetRegionId} />
        </div>
      </div>

      <div>
        <div className="section-title">Current order book</div>
        <div className="orders-grid">
          <OrderBook title="Jita IV-4 — Sell Orders" orders={jitaSells} />
          <OrderBook title={`${stationName} — Sell Orders`} orders={targetSells} />
        </div>
      </div>
    </div>
  )
}

function OrderBook({ title, orders }) {
  return (
    <div className="order-table">
      <h4>{title}</h4>
      {orders.length === 0
        ? <div style={{ padding: '14px', color: 'var(--text-muted)', fontSize: 12 }}>No recent orders</div>
        : (
          <table>
            <thead>
              <tr>
                <th>Price (ISK)</th>
                <th>Volume</th>
                <th>Min</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o, i) => (
                <tr key={i}>
                  <td style={{ color: 'var(--gold)' }}>{isk(o.price)}</td>
                  <td>{num(o.volume_remain, 0)}</td>
                  <td style={{ color: 'var(--text-muted)' }}>{num(o.min_volume, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      }
    </div>
  )
}
