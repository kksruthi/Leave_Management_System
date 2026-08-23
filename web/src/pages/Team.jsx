import { useState } from 'react'
import { api } from '../api'
import { TeamCalendar } from '../components/TeamCalendar'
import { Alert, Card, Empty, Spinner, Stat, StatusBadge, TypeBadge,
         fmt, fmtDate, useAsync } from '../components/ui'

export function TeamOverview() {
  const { data, loading, error } = useAsync(() => api.teamOverview())
  if (loading) return <Spinner />
  if (error) return <Alert level="danger">{error}</Alert>

  return (
    <>
      <div className="page-head">
        <div>
          <h1>My team</h1>
          <p className="sub">Who is away, and what needs your decision.</p>
        </div>
      </div>

      <div className="grid g3" style={{ marginBottom: 14 }}>
        <Stat label="Pending approvals" value={data.pending_approvals}
              tone={data.pending_approvals > 0 ? 'warning' : undefined}
              foot={data.pending_approvals ? 'Waiting on you' : 'Queue clear'} />
        <Stat label="Team members" value={data.team_size} />
        <Stat label="On leave today" value={data.on_leave_today.length}
              foot={data.on_leave_today.map(r => r.employee).join(', ') || 'Everyone in'} />
      </div>

      <Card title="Upcoming leave" subtitle="Approved and pending, next in date order.">
        {!data.upcoming.length ? <Empty>No upcoming leave.</Empty> : (
          <table>
            <thead>
              <tr><th>Employee</th><th>Type</th><th>Dates</th>
                  <th className="num">Days</th><th>Status</th></tr>
            </thead>
            <tbody>
              {data.upcoming.map((r, i) => (
                <tr key={i}>
                  <td>{r.employee}</td>
                  <td><TypeBadge type={r.leave_type_id} /></td>
                  <td>{fmtDate(r.start_date, { day: 'numeric', month: 'short' })} – {fmtDate(r.end_date)}</td>
                  <td className="num">{fmt(r.duration_days)}</td>
                  <td><StatusBadge status={r.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}

export function TeamCalendarPage() {
  const now = new Date()
  const [ym, setYm] = useState({ year: now.getFullYear(), month: now.getMonth() + 1 })
  const { data, loading, error } = useAsync(
    () => api.teamCalendar(ym.year, ym.month), [ym.year, ym.month])

  const shift = (delta) => setYm(({ year, month }) => {
    const m = month + delta
    if (m < 1) return { year: year - 1, month: 12 }
    if (m > 12) return { year: year + 1, month: 1 }
    return { year, month: m }
  })

  const monthName = new Date(ym.year, ym.month - 1, 1)
    .toLocaleDateString(undefined, { month: 'long', year: 'numeric' })

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Team calendar</h1>
          <p className="sub">Who is away when. Leave type is shown; reasons are private.</p>
        </div>
        <div className="chips">
          <button className="btn btn-sm" onClick={() => shift(-1)}>‹ Prev</button>
          <span className="btn btn-sm" style={{ cursor: 'default' }}>{monthName}</span>
          <button className="btn btn-sm" onClick={() => shift(1)}>Next ›</button>
        </div>
      </div>

      <Card>
        {loading ? <Spinner /> : error ? <Alert level="danger">{error}</Alert>
          : data.employees.length ? <TeamCalendar data={data} />
          : <Empty>No team members to show.</Empty>}
      </Card>
    </>
  )
}

export function TeamBalances() {
  const { data, loading, error } = useAsync(() => api.teamBalances())
  if (loading) return <Spinner />
  if (error) return <Alert level="danger">{error}</Alert>

  const types = [...new Set(data.flatMap(e => Object.keys(e.balances)))]
    .filter(t => t !== 'Unpaid').sort()

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Team balances</h1>
          <p className="sub">
            Balances only. Entitlement rules and personal records stay with HR.
          </p>
        </div>
      </div>
      <Card>
        {!data.length ? <Empty>No team members.</Empty> : (
          <table>
            <thead>
              <tr>
                <th>Employee</th><th>Region</th>
                {types.map(t => <th key={t} className="num">{t}</th>)}
                <th className="num">Pending</th>
              </tr>
            </thead>
            <tbody>
              {data.map(e => (
                <tr key={e.id}>
                  <td>{e.name}<div className="small muted">{e.department || '—'}</div></td>
                  <td className="small muted">{e.region}</td>
                  {types.map(t => (
                    <td key={t} className="num">
                      {e.balances[t] !== undefined ? fmt(e.balances[t]) : '—'}
                    </td>
                  ))}
                  <td className="num">{e.pending_requests || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}
