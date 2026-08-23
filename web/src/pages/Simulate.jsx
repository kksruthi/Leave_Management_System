import { useState } from 'react'
import { api } from '../api'
import { Alert, Card, Empty, Spinner, TypeBadge, fmt, fmtDate, useAsync } from '../components/ui'

/* The Simulation screen.
   ==========================================================================

   Everything dynamic about this engine happens over TIME: accrual month by
   month, a tenure bracket crossing, a region transfer re-pricing an
   entitlement, a leave year closing, a policy version superseding another.
   Open the app on one Tuesday and none of it is visible — you see one number
   and have to take the rest on trust.

   This page makes the engine perform. Each button calls the same function a
   scheduled job or an HR screen would call; the only thing added is a
   snapshot either side of it, so the change has somewhere to appear. Nothing
   here is a mock: the rows written are the rows everyone else then sees.

   The ledger underneath is the proof. It is append-only, so every simulation
   leaves a line in it, and the timeline at the bottom is that audit trail
   rendered for a human. */
export default function Simulate() {
  const { data: opts, loading } = useAsync(() => api.simulateOptions())
  const [employeeId, setEmployeeId] = useState(null)
  const [state, setState] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(null)
  const [err, setErr] = useState(null)

  if (loading) return <Spinner />

  const pick = async (id) => {
    setEmployeeId(id); setResult(null); setErr(null); setState(null)
    if (id) setState(await api.simulateState(id))
  }

  const run = async (action) => {
    setBusy(action); setErr(null)
    try {
      const res = await api.simulateRun(employeeId, action)
      setResult(res)
      setState({ snapshot: res.after, timeline: res.timeline })
    } catch (e) { setErr(e.message) }
    finally { setBusy(null) }
  }

  const snap = state?.snapshot

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Simulation</h1>
          <p className="sub">
            Run the engine and watch it move. Every action here calls the same
            code as the real job — it writes real rows, and the timeline below
            is the audit trail it leaves.
          </p>
        </div>
      </div>

      <Card>
        <div className="field" style={{ maxWidth: 420, marginBottom: 0 }}>
          <label htmlFor="who">Simulate for</label>
          <select id="who" value={employeeId || ''} onChange={e => pick(Number(e.target.value) || null)}>
            <option value="">Choose someone…</option>
            {opts.employees.map(p => (
              <option key={p.id} value={p.id}>
                {p.name} — {p.region} · {p.role === 'hr_admin' ? 'HR' : p.role}
              </option>
            ))}
          </select>
        </div>
      </Card>

      {!employeeId && (
        <Card><Empty>Pick someone to see their current state and what you can do to it.</Empty></Card>
      )}

      {snap && (
        <>
          <div className="grid g4" style={{ marginTop: 14 }}>
            <div className="stat">
              <div className="label">Region</div>
              <div className="value" style={{ fontSize: 19 }}>{snap.region}</div>
              <div className="foot">leave year opens {fmtDate(snap.leave_year_start)}</div>
            </div>
            <div className="stat">
              <div className="label">Tenure</div>
              <div className="value">{snap.tenure_years} yr</div>
              <div className="foot">drives the entitlement bracket</div>
            </div>
            <div className="stat">
              <div className="label">Policy year</div>
              <div className="value">{snap.policy_year ?? '—'}</div>
              <div className="foot">version their days are priced against</div>
            </div>
            <div className="stat">
              <div className="label">As of</div>
              <div className="value" style={{ fontSize: 19 }}>{fmtDate(snap.as_of)}</div>
              <div className="foot">every figure is summed to this date</div>
            </div>
          </div>

          <Card title="Current state" className="card-accent">
            <table>
              <thead>
                <tr>
                  <th>Type</th><th className="num">Entitlement</th><th>Accrual</th>
                  <th className="num">Current yr</th><th className="num">Carry-over</th>
                  <th className="num">Taken</th><th className="num">Balance</th>
                </tr>
              </thead>
              <tbody>
                {snap.types.map(t => (
                  <tr key={t.leave_type_id}>
                    <td><TypeBadge type={t.leave_type_id} /></td>
                    <td className="num">{t.entitlement ? `${fmt(t.entitlement)}/yr` : '—'}</td>
                    <td className="small muted">{t.accrual_method || '—'}</td>
                    <td className="num">{fmt(t.current)}</td>
                    <td className="num">{fmt(t.carryover)}</td>
                    <td className="num">{Number(t.taken) > 0 ? fmt(t.taken) : '—'}</td>
                    <td className="num"><strong>{fmt(t.balance)}</strong></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {err && <Alert level="danger">{err}</Alert>}

          <Card title="Run a simulation"
                subtitle="These write to the database. The result is what every other screen will then show.">
            <div className="stack">
              {opts.actions.map(a => (
                <div key={a.key} className="between"
                     style={{ borderBottom: '1px solid var(--grid)', paddingBottom: 12 }}>
                  <div style={{ maxWidth: 620 }}>
                    <strong>{a.label}</strong>
                    <div className="small muted" style={{ marginTop: 2 }}>{a.detail}</div>
                    <div className="small" style={{ marginTop: 3, color: 'var(--accent)' }}>
                      {a.shows}
                    </div>
                  </div>
                  <button className="btn btn-primary" disabled={busy !== null}
                          onClick={() => run(a.key)}>
                    {busy === a.key ? 'Running…' : 'Run'}
                  </button>
                </div>
              ))}
            </div>
          </Card>

          {result && <ResultPanel result={result} />}

          <Card title="Ledger timeline"
                subtitle="Append-only. Every simulation above left one of these lines.">
            {!state.timeline.length ? <Empty>No ledger rows yet.</Empty> : (
              <table>
                <thead>
                  <tr>
                    <th>Date</th><th>Type</th><th className="num">Amount</th>
                    <th>Bucket</th><th>Expires</th><th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {state.timeline.map(r => (
                    <tr key={r.id}>
                      <td className="mono small">{r.date}</td>
                      <td><TypeBadge type={r.leave_type_id} /></td>
                      <td className="num"
                          style={{ color: Number(r.amount) < 0 ? 'var(--critical)' : 'var(--good)' }}>
                        {Number(r.amount) > 0 ? '+' : ''}{fmt(r.amount, 3)}
                      </td>
                      <td className="small">
                        {r.bucket === 'carryover'
                          ? <span className="badge badge-accent">carry-over</span>
                          : <span className="muted">current</span>}
                      </td>
                      <td className="small mono">{r.expires_on || '—'}</td>
                      <td className="small">{r.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}
    </>
  )
}

function ResultPanel({ result }) {
  return (
    <Card title="What changed" className="card-accent">
      <Alert level="good">{result.note}</Alert>
      {result.time_travelled && (
        <Alert level="info">
          This action takes effect on {fmtDate(result.viewed_at)}, so the
          "after" column is read on that date — reading it today would report
          that nothing moved. Where a balance drops to zero, that is a new
          leave year starting: the old year has closed and the new one has not
          accrued yet. Carry-over is the part that survives, and it is listed
          separately.
        </Alert>
      )}

      {result.brackets && (
        <table>
          <thead>
            <tr><th>Anniversary</th><th>Date</th><th className="num">Before</th>
                <th className="num">After</th><th>Status</th></tr>
          </thead>
          <tbody>
            {result.brackets.map(b => (
              <tr key={b.years}>
                <td>{b.years} year{b.years === 1 ? '' : 's'}</td>
                <td className="mono small">{b.date}</td>
                <td className="num">{fmt(b.before)}/yr</td>
                <td className="num">
                  <strong style={b.changed ? { color: 'var(--accent)' } : undefined}>
                    {fmt(b.after)}/yr
                  </strong>
                </td>
                <td className="small">
                  {b.reached
                    ? <span className="badge badge-approved">
                        <span className="dot" style={{ background: 'currentColor' }} />reached
                      </span>
                    : <span className="badge badge-cancelled">future</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {result.changes.length === 0 && !result.brackets && (
        <Empty>Nothing moved — the state already reflected this action.</Empty>
      )}

      {result.changes.length > 0 && (
        <table>
          <thead><tr><th>What</th><th className="num">Before</th><th className="num">After</th></tr></thead>
          <tbody>
            {result.changes.map((c, i) => (
              <tr key={i}>
                <td>{c.what}</td>
                <td className="num muted">{c.from}</td>
                <td className="num"><strong style={{ color: 'var(--accent)' }}>{c.to}</strong></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}
