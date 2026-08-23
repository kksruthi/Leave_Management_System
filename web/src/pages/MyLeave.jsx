import { useState } from 'react'
import { api } from '../api'
import { Card, Stat, StatusBadge, TypeBadge, Alert, Spinner, Empty,
         fmt, fmtDate, useAsync, LEAVE_NAME, leaveColor } from '../components/ui'
import { Link } from 'react-router-dom'
import { today } from '../dates'

export default function MyLeave() {
  /* Every balance in this system is point-in-time: `get_live_balance` takes
     an as-of date and sums the ledger to that day. Pinning the screen to
     "now" hides that, and hides anything that has since expired — carry-over
     in particular, which is only visible between the year start and its
     three-month deadline. So the date is a control, not a constant. */
  const [asOf, setAsOf] = useState(today())
  const { data, loading, error } = useAsync(() => api.myDashboard(asOf), [asOf])

  if (loading) return <Spinner />
  if (error) return <Alert level="danger">{error}</Alert>

  const types = data.leave_types.filter(t => t.policy_found)
  const paid = types.filter(t => t.is_paid)

  return (
    <>
      <div className="page-head">
        <div>
          <h1>My leave</h1>
          <p className="sub">
            {data.name} · {data.region} · {data.tenure_years} year
            {data.tenure_years === 1 ? '' : 's'} of service
          </p>
        </div>
        <div className="chips" style={{ alignItems: 'center' }}>
          <label htmlFor="asof" className="small muted" style={{ margin: 0 }}>
            Balance as of
          </label>
          <input id="asof" type="date" value={asOf} style={{ width: 160 }}
                 onChange={e => setAsOf(e.target.value || today())} />
          <Link className="btn btn-primary" to="/request">Request leave</Link>
        </div>
      </div>

      {asOf !== today() && (
        <Alert level="info">
          Showing the ledger as it stood on {fmtDate(asOf)}. Every figure below is
          summed to that date — this is the same point-in-time query the audit
          screen uses, not a projection.
        </Alert>
      )}

      <div className="grid g4" style={{ marginBottom: 14 }}>
        {/* The headline number is what you can actually take, and the foot
            line shows where it came from. Showing only a total invites the
            "why do I have 50 days against a 30-day entitlement?" question —
            the answer being that a total hides a closed year's accrual and
            an expiring carry-over pot behind one figure. */}
        {paid.map(t => (
          <Stat key={t.leave_type_id}
                label={LEAVE_NAME[t.leave_type_id] || t.leave_type_id}
                value={fmt(t.available_days)}
                foot={
                  <>
                    {fmt(t.current_year_balance)} this year
                    {Number(t.carryover_balance) > 0
                      ? ` + ${fmt(t.carryover_balance)} carried over`
                      : ''}
                    <div>
                      {fmt(t.taken_days)} taken · {fmt(t.entitlement_days_per_year)} days/year
                    </div>
                  </>
                } />
        ))}
      </div>

      <div className="grid g2">
        <Card title="Balance detail"
              subtitle="Carry-over is tracked separately because it expires. Approved leave that has not started yet is committed but not yet deducted.">
          <table>
            <thead>
              <tr>
                <th>Type</th>
                <th className="num">Current yr</th>
                <th className="num">Carry-over</th>
                <th className="num">Taken</th>
                <th className="num">Approved ahead</th>
                <th className="num">Pending</th>
                <th className="num">Available</th>
              </tr>
            </thead>
            <tbody>
              {types.map(t => (
                <tr key={t.leave_type_id}>
                  <td><TypeBadge type={t.leave_type_id} /></td>
                  <td className="num">{fmt(t.current_year_balance)}</td>
                  <td className="num">{fmt(t.carryover_balance)}</td>
                  <td className="num">{Number(t.taken_days) > 0 ? fmt(t.taken_days) : '—'}</td>
                  {/* Approved leave that starts in the future. The ledger
                      deduction is dated to the first day of the leave, so it
                      has not moved the balance yet — but the days are spent,
                      and showing them as available would be a lie. */}
                  <td className="num" title="Approved, starts later — already committed">
                    {Number(t.scheduled_days) > 0 ? `−${fmt(t.scheduled_days)}` : '—'}
                  </td>
                  <td className="num">{Number(t.pending_days) > 0 ? `−${fmt(t.pending_days)}` : '—'}</td>
                  <td className="num"><strong>{fmt(t.available_days)}</strong></td>
                </tr>
              ))}
            </tbody>
          </table>

          {types.some(t => t.expiring?.length) && (
            <div style={{ marginTop: 12 }}>
              {types.flatMap(t => (t.expiring || []).map(x => (
                <Alert key={`${t.leave_type_id}-${x.date}`} level="warning">
                  <strong>{fmt(x.days)} days</strong> of {t.leave_type_id} carry-over
                  expires on {fmtDate(x.date)}. Use it before then or it lapses.
                </Alert>
              )))}
            </div>
          )}
        </Card>

        <Card title="Next accrual" subtitle="What you will receive, and when.">
          <table>
            <thead><tr><th>Type</th><th>Date</th><th className="num">Amount</th></tr></thead>
            <tbody>
              {types.filter(t => t.next_accrual).map(t => (
                <tr key={t.leave_type_id}>
                  <td><TypeBadge type={t.leave_type_id} /></td>
                  <td>{fmtDate(t.next_accrual.date)}</td>
                  <td className="num">+{fmt(t.next_accrual.amount, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {[...new Set(types.filter(t => t.next_accrual?.note).map(t => t.next_accrual.note))]
            .map((note, i) => (
              <p key={i} className="sub small" style={{ marginTop: 8 }}>{note}</p>
            ))}
        </Card>
      </div>

      <div className="grid g2" style={{ marginTop: 14 }}>
        <Card title="Pending requests">
          {data.pending_requests.length === 0
            ? <Empty>Nothing awaiting approval.</Empty>
            : (
              <table>
                <thead><tr><th>Type</th><th>Dates</th><th className="num">Days</th><th>Stage</th></tr></thead>
                <tbody>
                  {data.pending_requests.map(r => (
                    <tr key={r.request_id}>
                      <td><TypeBadge type={r.leave_type_id} /></td>
                      <td>{fmtDate(r.start_date, { day: 'numeric', month: 'short' })} – {fmtDate(r.end_date)}</td>
                      <td className="num">{fmt(r.duration_days)}</td>
                      <td className="small">
                        {r.current_role
                          ? <>Awaiting {r.current_role === 'hr_admin' ? 'HR' : r.current_role}</>
                          : <StatusBadge status="pending" />}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
        </Card>

        <Card title="Upcoming approved leave">
          {data.upcoming_approved.length === 0
            ? <Empty>No approved leave booked.</Empty>
            : (
              <table>
                <thead><tr><th>Type</th><th>Dates</th><th className="num">Days</th></tr></thead>
                <tbody>
                  {data.upcoming_approved.map(r => (
                    <tr key={r.id}>
                      <td><TypeBadge type={r.leave_type_id} /></td>
                      <td>{fmtDate(r.start_date, { day: 'numeric', month: 'short' })} – {fmtDate(r.end_date)}</td>
                      <td className="num">{fmt(r.duration_days)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
        </Card>
      </div>

      <Card title="Recent activity" className="" >
        <RecentHistory />
      </Card>
    </>
  )
}

function RecentHistory() {
  const { data, loading } = useAsync(() => api.myRequests())
  if (loading) return <Spinner />
  const rows = (data || []).slice(0, 6)
  if (!rows.length) return <Empty>No leave history yet.</Empty>
  return (
    <table>
      <thead><tr><th>Type</th><th>Dates</th><th className="num">Days</th><th>Status</th><th>Decided by</th></tr></thead>
      <tbody>
        {rows.map(r => {
          const last = [...r.chain].reverse().find(c => c.acted_by)
          return (
            <tr key={r.id}>
              <td><TypeBadge type={r.leave_type_id} /></td>
              <td>{fmtDate(r.start_date, { day: 'numeric', month: 'short' })} – {fmtDate(r.end_date)}</td>
              <td className="num">{fmt(r.duration_days)}</td>
              <td><StatusBadge status={r.status} /></td>
              <td className="small muted">{last ? `${last.acted_by}` : '—'}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
