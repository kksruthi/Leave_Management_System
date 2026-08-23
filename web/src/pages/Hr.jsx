import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { Relocate } from '../components/Relocate'
import { Alert, Card, Empty, Spinner, Stat, StatusBadge, TypeBadge,
         fmt, fmtDate, fmtDateTime, useAsync } from '../components/ui'

/* ---------------------------------------------------------------- overview */
export function HrOverview() {
  const { data, loading, error, reload } = useAsync(() => api.hrOverview())
  const [busy, setBusy] = useState(false)
  if (loading) return <Spinner />
  if (error) return <Alert level="danger">{error}</Alert>

  const escalate = async () => {
    setBusy(true)
    try { await api.hrEscalate(); reload() } finally { setBusy(false) }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Organisation</h1>
          <p className="sub">Leave across every region and entity.</p>
        </div>
        <button className="btn btn-sm" onClick={escalate} disabled={busy}>
          {busy ? 'Running…' : 'Run overdue sweep'}
        </button>
      </div>

      <div className="grid g3" style={{ marginBottom: 14 }}>
        <Stat label="Employees" value={data.employees} />
        <Stat label="On leave today" value={data.on_leave_today} />
        <Stat label="Pending approvals" value={data.pending_approvals}
              tone={data.pending_approvals > 0 ? 'warning' : undefined} />
        <Stat label="Requests this month" value={data.requests_this_month} />
        <Stat label="Leave consumed" value={fmt(data.leave_consumed_days)} foot="days, all time" />
        <Stat label="Policy exceptions" value={data.policy_exceptions} />
      </div>

      <Card title="Alerts" subtitle="Things that need a person to look at them.">
        {!data.alerts.length ? <Empty>Nothing needs attention.</Empty> : data.alerts.map((a, i) => (
          <Alert key={i} level={a.level}>
            <div>
              <strong>{a.message}</strong>
              {a.detail?.length > 0 && (
                <ul style={{ margin: '6px 0 0', paddingLeft: 18 }} className="small">
                  {a.detail.slice(0, 6).map((d, j) => (
                    <li key={j}>
                      {d.employee && `${d.employee} · `}
                      {d.leave_type && `${d.leave_type} · `}
                      {d.days && `${fmt(d.days)} days`}
                      {d.balance && `balance ${fmt(d.balance)}`}
                      {d.expires_on && ` expires ${fmtDate(d.expires_on)}`}
                      {d.request_id && `request #${d.request_id}, tier ${d.tier} (${d.role})`}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Alert>
        ))}
      </Card>
    </>
  )
}

/* --------------------------------------------------------------- employees */
export function HrEmployees() {
  const [filters, setFilters] = useState({ q: '', region: '', status: '' })
  const { data, loading, error } = useAsync(
    () => api.hrEmployees(filters), [filters.q, filters.region, filters.status])
  const { data: meta } = useAsync(() => api.meta())

  return (
    <>
      <div className="page-head">
        <div><h1>Employees</h1><p className="sub">Search and inspect any record.</p></div>
      </div>

      <Card>
        <div className="row" style={{ marginBottom: 12 }}>
          <input placeholder="Search name or email" value={filters.q}
                 onChange={e => setFilters(f => ({ ...f, q: e.target.value }))} />
          <select value={filters.region} onChange={e => setFilters(f => ({ ...f, region: e.target.value }))}>
            <option value="">All regions</option>
            {(meta?.regions || []).map(r => <option key={r} value={r}>{r}</option>)}
          </select>
          <select value={filters.status} onChange={e => setFilters(f => ({ ...f, status: e.target.value }))}>
            <option value="">All statuses</option>
            <option value="active">Active</option>
            <option value="terminated">Terminated</option>
          </select>
        </div>

        {loading ? <Spinner /> : error ? <Alert level="danger">{error}</Alert> : (
          <table>
            <thead>
              <tr><th>Name</th><th>Region</th><th>Dept</th><th>Manager</th>
                  <th>Joined</th><th className="num">FTE</th><th>Status</th></tr>
            </thead>
            <tbody>
              {data.map(e => (
                <tr key={e.id}>
                  <td>
                    <Link to={`/hr/employees/${e.id}`}>{e.name}</Link>
                    <div className="small muted">{e.role}</div>
                  </td>
                  <td className="small">{e.region}</td>
                  <td className="small">{e.department || '—'}</td>
                  <td className="small">{e.manager || '—'}</td>
                  <td className="small">{fmtDate(e.join_date)}</td>
                  <td className="num">{Math.round(Number(e.employment_fraction) * 100)}%</td>
                  <td>
                    <span className={`badge badge-${e.status === 'active' ? 'approved' : 'cancelled'}`}>
                      <span className="dot" style={{ background: 'currentColor' }} />
                      {e.status}{e.exit_date ? ` ${fmtDate(e.exit_date)}` : ''}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}

export function HrEmployee() {
  const { id } = useParams()
  const { data, loading, error, reload } = useAsync(() => api.hrEmployee(id), [id])
  if (loading) return <Spinner />
  if (error) return <Alert level="danger">{error}</Alert>
  const e = data.employee

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{e.name}</h1>
          <p className="sub">{e.role} · {e.region} · {e.department || 'No department'}</p>
        </div>
        <Link className="btn btn-sm" to="/hr/employees">← All employees</Link>
      </div>

      <div className="grid g2">
        <Card title="Employment">
          <table>
            <tbody>
              <tr><td>Email</td><td className="num small">{e.email}</td></tr>
              <tr><td>Manager</td><td className="num">{e.manager || '—'}</td></tr>
              <tr><td>Joined</td><td className="num">{fmtDate(e.join_date)}</td></tr>
              <tr><td>Exit date</td><td className="num">{e.exit_date ? fmtDate(e.exit_date) : '—'}</td></tr>
              <tr><td>FTE</td><td className="num">{Math.round(Number(e.employment_fraction) * 100)}%</td></tr>
              <tr><td>Status</td><td className="num">{e.status}</td></tr>
              <tr><td>Last sign-in</td><td className="num small">{fmtDateTime(e.last_login_at)}</td></tr>
            </tbody>
          </table>
        </Card>

        <Card title="Balances">
          <table>
            <thead><tr><th>Type</th><th className="num">Balance</th>
                       <th className="num">Entitlement</th><th className="num">Carry-over</th></tr></thead>
            <tbody>
              {data.dashboard.leave_types.map(t => (
                <tr key={t.leave_type_id}>
                  <td><TypeBadge type={t.leave_type_id} /></td>
                  <td className="num">{fmt(t.balance)}</td>
                  <td className="num">{t.entitlement_days_per_year ? fmt(t.entitlement_days_per_year) : '—'}</td>
                  <td className="num">{fmt(t.carryover_balance)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>

      <Relocate employee={e} onDone={reload} />

      {data.exceptions.length > 0 && (
        <Card title="Entitlement exceptions">
          <table>
            <thead><tr><th>Type</th><th className="num">Days/yr</th><th>Reason</th><th>From</th></tr></thead>
            <tbody>
              {data.exceptions.map((x, i) => (
                <tr key={i}>
                  <td><TypeBadge type={x.leave_type_id} /></td>
                  <td className="num">{fmt(x.entitlement_days_per_year)}</td>
                  <td className="small">{x.reason}</td>
                  <td className="small">{fmtDate(x.effective_from)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <Card title="Leave history">
        {!data.requests.length ? <Empty>No requests.</Empty> : (
          <table>
            <thead><tr><th>Type</th><th>Dates</th><th className="num">Days</th>
                       <th>Status</th><th>Note</th><th /></tr></thead>
            <tbody>
              {data.requests.map(r => (
                <tr key={r.id}>
                  <td><TypeBadge type={r.leave_type_id} /></td>
                  <td className="small">{fmtDate(r.start_date, { day: 'numeric', month: 'short' })} – {fmtDate(r.end_date)}</td>
                  <td className="num">{fmt(r.duration_days)}</td>
                  <td><StatusBadge status={r.status} /></td>
                  <td className="small muted">{r.reason || '—'}</td>
                  <td><Link className="small" to={`/hr/requests/${r.id}`}>Audit →</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}

/* ---------------------------------------------------------------- requests */
export function HrRequests() {
  const [filters, setFilters] = useState({ status: '', region: '', leave_type: '' })
  const { data, loading, error } = useAsync(
    () => api.hrRequests(filters), [filters.status, filters.region, filters.leave_type])
  const { data: meta } = useAsync(() => api.meta())

  return (
    <>
      <div className="page-head">
        <div><h1>Leave requests</h1><p className="sub">Every request, across the organisation.</p></div>
      </div>
      <Card>
        <div className="row" style={{ marginBottom: 12 }}>
          <select value={filters.status} onChange={e => setFilters(f => ({ ...f, status: e.target.value }))}>
            <option value="">Any status</option>
            {['pending', 'approved', 'rejected', 'cancelled'].map(s => <option key={s}>{s}</option>)}
          </select>
          <select value={filters.region} onChange={e => setFilters(f => ({ ...f, region: e.target.value }))}>
            <option value="">All regions</option>
            {(meta?.regions || []).map(r => <option key={r}>{r}</option>)}
          </select>
          <select value={filters.leave_type} onChange={e => setFilters(f => ({ ...f, leave_type: e.target.value }))}>
            <option value="">All types</option>
            {(meta?.leave_types || []).map(t => <option key={t}>{t}</option>)}
          </select>
        </div>

        {loading ? <Spinner /> : error ? <Alert level="danger">{error}</Alert> :
          !data.length ? <Empty>No matching requests.</Empty> : (
          <table>
            <thead><tr><th>Employee</th><th>Type</th><th>Dates</th><th className="num">Days</th>
                       <th className="num">Paid</th><th>Status</th><th /></tr></thead>
            <tbody>
              {data.map(r => (
                <tr key={r.id}>
                  <td>{r.employee_name}</td>
                  <td><TypeBadge type={r.leave_type_id} /></td>
                  <td className="small">{fmtDate(r.start_date, { day: 'numeric', month: 'short' })} – {fmtDate(r.end_date)}</td>
                  <td className="num">{fmt(r.duration_days)}</td>
                  <td className="num">{fmt(r.paid_days)}</td>
                  <td><StatusBadge status={r.status} /></td>
                  <td><Link className="small" to={`/hr/requests/${r.id}`}>Audit →</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}

/* ------------------------------------------------------------------- audit */
export function HrAudit() {
  const { id } = useParams()
  const { data, loading, error } = useAsync(() => api.hrAudit(id), [id])
  if (loading) return <Spinner />
  if (error) return <Alert level="danger">{error}</Alert>
  const r = data.request

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Request #{r.id}</h1>
          <p className="sub">{r.employee_name} · {fmtDate(r.start_date)} – {fmtDate(r.end_date)}</p>
        </div>
        <Link className="btn btn-sm" to="/hr/requests">← All requests</Link>
      </div>

      <div className="grid g2">
        <Card title="Request">
          <table><tbody>
            <tr><td>Type</td><td className="num"><TypeBadge type={r.leave_type_id} /></td></tr>
            <tr><td>Status</td><td className="num"><StatusBadge status={r.status} /></td></tr>
            <tr><td>Duration</td><td className="num">{fmt(r.duration_days)} days</td></tr>
            <tr><td>Paid / unpaid</td><td className="num">{fmt(r.paid_days)} / {fmt(r.unpaid_days)}</td></tr>
            <tr><td>Submitted</td><td className="num small">{fmtDateTime(r.submitted_at)}</td></tr>
            <tr><td>Employee note</td><td className="num small">{r.reason || '—'}</td></tr>
          </tbody></table>
        </Card>

        <Card title="Policy applied">
          {data.policy ? (
            <>
              <p className="small">{data.policy.explain}</p>
              <table style={{ marginTop: 8 }}><tbody>
                <tr><td>Policy row</td><td className="num mono">#{data.policy.id}</td></tr>
                <tr><td>Entitlement</td><td className="num">{fmt(data.policy.entitlement_days_per_year)} days/yr</td></tr>
              </tbody></table>
              <p className="sub small" style={{ marginTop: 8 }}>
                <strong>Compliance note:</strong> {data.policy.compliance_note}
              </p>
            </>
          ) : <Empty>No policy resolved.</Empty>}
        </Card>
      </div>

      <Card title="Timeline" subtitle="Who did what, when, and why.">
        <div className="timeline">
          {data.timeline.map((t, i) => (
            <div className="tl-item" key={i}>
              <div className="tl-when">{fmtDateTime(t.at)}</div>
              <div><strong>{t.actor}</strong> — {t.event}</div>
            </div>
          ))}
        </div>
      </Card>

      <Card title="Ledger entries">
        {!data.ledger.length
          ? <Empty>No ledger movement yet — the leave has not been fully approved.</Empty>
          : (
            <table>
              <thead><tr><th>Date</th><th>Type</th><th className="num">Amount</th><th>Reason</th></tr></thead>
              <tbody>
                {data.ledger.map(l => (
                  <tr key={l.id}>
                    <td className="small">{fmtDate(l.effective_date)}</td>
                    <td><TypeBadge type={l.leave_type_id} /></td>
                    <td className="num">{fmt(l.amount, 3)}</td>
                    <td className="small muted">{l.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </Card>
    </>
  )
}
