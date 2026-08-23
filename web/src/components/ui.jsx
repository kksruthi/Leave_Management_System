import { useEffect, useState } from 'react'

export const LEAVE_COLOR = {
  EL: 'var(--EL)', CL: 'var(--CL)', SL: 'var(--SL)', Unpaid: 'var(--Unpaid)',
}
export const leaveColor = (t) => LEAVE_COLOR[t] || 'var(--muted)'

export const LEAVE_NAME = {
  EL: 'Earned Leave', CL: 'Casual Leave', SL: 'Sick Leave', Unpaid: 'Unpaid Leave',
}

export const fmt = (v, dp = 2) => {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isNaN(n)) return String(v)
  return n.toFixed(dp).replace(/\.?0+$/, '') || '0'
}

export const fmtDate = (iso, opts) =>
  iso ? new Date(iso + (iso.length === 10 ? 'T00:00:00' : '')).toLocaleDateString(
    undefined, opts || { day: 'numeric', month: 'short', year: 'numeric' }) : '—'

export const fmtDateTime = (iso) =>
  iso ? new Date(iso).toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—'

export function Stat({ label, value, foot, tone }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value" style={tone ? { color: `var(--${tone})` } : undefined}>{value}</div>
      {foot && <div className="foot">{foot}</div>}
    </div>
  )
}

/* Status never travels on colour alone — every badge carries its word. */
export function StatusBadge({ status }) {
  const label = { approved: 'Approved', pending: 'Pending', rejected: 'Rejected',
    cancelled: 'Cancelled', active: 'Awaiting' }[status] || status
  const cls = status === 'active' ? 'pending' : status
  return (
    <span className={`badge badge-${cls}`}>
      <span className="dot" style={{ background: 'currentColor' }} />{label}
    </span>
  )
}

export function TypeBadge({ type }) {
  return (
    <span className="badge badge-type" title={LEAVE_NAME[type] || type}>
      <span className="dot" style={{ background: leaveColor(type) }} />{type}
    </span>
  )
}

export function Card({ title, subtitle, actions, children, className = '' }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <div className="between" style={{ marginBottom: title ? 12 : 0 }}>
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p className="sub">{subtitle}</p>}
          </div>
          {actions}
        </div>
      )}
      {children}
    </section>
  )
}

export function Empty({ children }) { return <div className="empty">{children}</div> }

export function Alert({ level = 'info', children }) {
  const icon = { warning: '⚠', danger: '⛔', info: 'ℹ', good: '✓' }[level] || 'ℹ'
  return <div className={`alert alert-${level}`}><span aria-hidden="true">{icon}</span><div>{children}</div></div>
}

export function Spinner({ label = 'Loading…' }) {
  return <div className="empty">{label}</div>
}

export function useAsync(fn, deps = []) {
  const [state, setState] = useState({ loading: true, data: null, error: null })
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let live = true
    setState(s => ({ ...s, loading: true }))
    Promise.resolve(fn())
      .then(d => live && setState({ loading: false, data: d, error: null }))
      .catch(e => live && setState({ loading: false, data: null, error: e.message }))
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])
  return { ...state, reload: () => setNonce(n => n + 1) }
}

export function ThemeToggle() {
  const [theme, setTheme] = useState(() => localStorage.getItem('leave.theme') || 'light')
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('leave.theme', theme)
  }, [theme])
  return (
    <button className="btn btn-sm" onClick={() => setTheme(t => (t === 'light' ? 'dark' : 'light'))}>
      {theme === 'light' ? '◐ Dark' : '◑ Light'}
    </button>
  )
}
