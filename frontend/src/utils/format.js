export function isk(value) {
  if (value == null) return '—'
  const abs = Math.abs(value)
  if (abs >= 1e12) return (value / 1e12).toFixed(2) + 'T'
  if (abs >= 1e9)  return (value / 1e9).toFixed(2)  + 'B'
  if (abs >= 1e6)  return (value / 1e6).toFixed(2)  + 'M'
  if (abs >= 1e3)  return (value / 1e3).toFixed(2)  + 'K'
  return value.toFixed(2)
}

export function num(value, decimals = 0) {
  if (value == null) return '—'
  return value.toLocaleString('en-GB', { maximumFractionDigits: decimals })
}

export function pct(value, decimals = 1) {
  if (value == null) return '—'
  return value.toFixed(decimals) + '%'
}

export function relativeTime(isoString) {
  if (!isoString) return '—'
  const diff = Date.now() - new Date(isoString).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1)  return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24)  return `${hrs}h ago`
  return `${Math.floor(hrs / 24)}d ago`
}
