import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { DayBreakdown } from '../components/DayBreakdown'
import { Alert, Card, Empty, Spinner, StatusBadge, TypeBadge,
         fmt, fmtDate, fmtDateTime, useAsync } from '../components/ui'

export default function MyRequests() {
  const [params] = useSearchParams()
  const highlight = Number(params.get('highlight'))
  const { data, loading, error, reload } = useAsync(() => api.myRequests())
  const [open, setOpen] = useState(highlight || null)
  const [busy, setBusy] = useState(null)
  const [msg, setMsg] = useState(null)

  if (loading) return <Spinner />
  if (error) return <Alert level="danger">{error}</Alert>

  const cancel = async (id) => {
    setBusy(id); setMsg(null)
    try { await api.cancelRequest(id); setMsg('Request cancelled.'); reload() }
    catch (e) { setMsg(e.message) }
    finally { setBusy(null) }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>My requests</h1>
          <p className="sub">Everything you have submitted, and what happened to it.</p>
        </div>
      </div>

      {msg && <Alert level="info">{msg}</Alert>}

      {!data.length ? <Card><Empty>You have not requested any leave yet.</Empty></Card> : (
        <div className="stack">
          {data.map(r => (
            <Card key={r.id}>
              <div className="between" style={{ cursor: 'pointer' }}
                   onClick={() => setOpen(open === r.id ? null : r.id)}>
                <div>
                  <div className="chips" style={{ marginBottom: 5 }}>
                    <TypeBadge type={r.leave_type_id} />
                    <StatusBadge status={r.status} />
                    {r.reason_withheld && <span className="badge badge-type">note attached</span>}
                  </div>
                  <strong>{fmtDate(r.start_date)} – {fmtDate(r.end_date)}</strong>
                  <span className="muted"> · {fmt(r.duration_days)} days</span>
                  <div className="small muted">
                    {fmt(r.paid_days)} paid · {fmt(r.unpaid_days)} unpaid ·
                    submitted {fmtDateTime(r.submitted_at)}
                  </div>
                </div>
                <div className="chips">
                  {r.can_cancel && (
                    <button className="btn btn-sm btn-danger" disabled={busy === r.id}
                            onClick={(e) => { e.stopPropagation(); cancel(r.id) }}>
                      {busy === r.id ? 'Cancelling…' : 'Cancel'}
                    </button>
                  )}
                  <button className="btn btn-sm">{open === r.id ? 'Hide' : 'Details'}</button>
                </div>
              </div>

              {open === r.id && (
                <div style={{ marginTop: 14, borderTop: '1px solid var(--grid)', paddingTop: 14 }}>
                  {r.reason && (
                    <p className="small"><strong>Your note:</strong> {r.reason}</p>
                  )}
                  <h3 style={{ marginTop: 10 }}>Approval trail</h3>
                  <div className="timeline">
                    {r.chain.map(step => (
                      <div className="tl-item" key={step.tier}>
                        <div className="tl-when">
                          {step.acted_at ? fmtDateTime(step.acted_at) : 'not yet actioned'}
                        </div>
                        <div>
                          <strong>Tier {step.tier} · {step.role === 'hr_admin' ? 'HR' : step.role}</strong>
                          {' '}<StatusBadge status={step.status} />
                        </div>
                        <div className="small muted">{step.routing_reason}</div>
                        {step.acted_by && (
                          <div className="small">
                            {step.status === 'approved' ? 'Approved' : 'Rejected'} by {step.acted_by}
                            {step.decision_reason && <> — “{step.decision_reason}”</>}
                          </div>
                        )}
                        {!step.acted_by && step.assigned_to && (
                          <div className="small muted">With {step.assigned_to}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </Card>
          ))}
        </div>
      )}
    </>
  )
}
