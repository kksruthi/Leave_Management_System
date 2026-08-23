import { useState } from 'react'
import { api } from '../api'
import { DayBreakdown } from '../components/DayBreakdown'
import { Alert, Card, Empty, Spinner, TypeBadge,
         fmt, fmtDate, fmtDateTime, useAsync } from '../components/ui'
import { Copilot } from '../components/Copilot'

const ROLE_LABEL = { manager: 'Manager', hr_admin: 'HR', director: 'Director' }

/* The approval screen, after manual routing.
   ==========================================================================

   The important change: **approving and forwarding are different buttons**.

   Before, the system decided from a rules table that a 12-day request needed
   HR, pre-created an HR tier, and relabelled the manager's button "Approve &
   send to HR". A manager who clicked it believed they had granted the leave;
   in fact they had passed it on. And a manager who thought HR *should* see a
   4-day request had no way to send it.

   Now:

     * **Approve** grants the leave and deducts the balance. Final, by that
       person, on their authority.
     * **Forward to HR / Forward to Director** explicitly does NOT grant it.
       The step closes as `forwarded`, a new active step opens for the target,
       and the recipient is named on the button so nobody is forwarding into
       a void.
     * The rules table still runs — as a **recommendation** shown above the
       buttons, with its reason. A manager who overrides it is making a
       judgement call on the record. A manager who never saw it was merely
       uninformed, which is worse.

   The ladder is one step at a time: a manager can reach HR, HR can reach the
   director, and nobody can skip or send it back. */
export default function Approvals() {
  const { data, loading, error, reload } = useAsync(() => api.approvals())
  const [comments, setComments] = useState({})
  const [busy, setBusy] = useState(null)
  const [msg, setMsg] = useState(null)
  const [err, setErr] = useState(null)

  if (loading) return <Spinner />
  if (error) return <Alert level="danger">{error}</Alert>

  const decide = async (item, decision) => {
    const comment = (comments[item.step_id] || '').trim()
    if (decision === 'rejected' && !comment) {
      setErr('A reason is required when rejecting — the employee will see it.')
      return
    }
    setBusy(item.step_id); setErr(null); setMsg(null)
    try {
      const res = await api.decide(item.step_id, decision, comment || null)
      setMsg(decision === 'rejected'
        ? `Request #${res.id} rejected. ${res.employee_name} has been notified.`
        : `Request #${res.id} approved. The days have been deducted from ${res.employee_name}'s balance.`)
      reload()
    } catch (e) { setErr(e.message) }
    finally { setBusy(null) }
  }

  const forward = async (item, role) => {
    const note = (comments[item.step_id] || '').trim()
    setBusy(item.step_id); setErr(null); setMsg(null)
    try {
      const res = await api.forward(item.step_id, role, note || null)
      setMsg(`Request #${res.id} sent to ${res.forwarded_to_name || ROLE_LABEL[role] || role}. `
             + 'It is no longer in your queue and the leave has not been granted.')
      reload()
    } catch (e) { setErr(e.message) }
    finally { setBusy(null) }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Approvals</h1>
          <p className="sub">
            {data.length} request{data.length === 1 ? '' : 's'} waiting on you.
          </p>
        </div>
      </div>

      {msg && <Alert level="good">{msg}</Alert>}
      {err && <Alert level="danger">{err}</Alert>}

      {!data.length ? <Card><Empty>Nothing to approve. Your queue is clear.</Empty></Card> : (
        <div className="stack">
          {data.map(item => {
            const r = item.request
            const options = item.forward_options || []
            const recommended = item.recommended || []
            const recommendedRoles = new Set(recommended.map(x => x.role))
            return (
              <Card key={item.step_id} className="card-accent">
                <div className="between" style={{ alignItems: 'flex-start' }}>
                  <div>
                    <div className="chips" style={{ marginBottom: 6 }}>
                      <TypeBadge type={r.leave_type_id} />
                      {Number(r.unpaid_days) > 0 && (
                        <span className="badge badge-rejected">
                          {fmt(r.unpaid_days)} unpaid
                        </span>
                      )}
                      {item.tier > 1 && (
                        <span className="badge badge-forwarded">
                          Forwarded to you · tier {item.tier}
                        </span>
                      )}
                      {item.is_overdue && <span className="badge badge-rejected">Overdue</span>}
                      {item.on_behalf_of && (
                        <span className="badge badge-type">for {item.on_behalf_of}</span>
                      )}
                    </div>
                    <h2 style={{ marginBottom: 2 }}>{r.employee_name}</h2>
                    <div>
                      {fmtDate(r.start_date)} – {fmtDate(r.end_date)}
                      <span className="muted"> · {fmt(r.duration_days)} days
                        {' · '}{fmt(r.paid_days)} paid</span>
                    </div>
                    <div className="small muted" style={{ marginTop: 4 }}>
                      {item.routing_reason}
                      {item.due_at && <> · due {fmtDateTime(item.due_at)}</>}
                    </div>
                    {r.reason_withheld && (
                      <div className="small muted" style={{ marginTop: 4 }}>
                        The employee attached a private note. It is visible to them and HR,
                        not to approvers.
                      </div>
                    )}
                  </div>

                  <div style={{ textAlign: 'right', minWidth: 150 }}>
                    <div className="small muted">Balance before</div>
                    <div className="mono" style={{ fontSize: 20 }}>{fmt(item.balance_before)}</div>
                    <div className="small muted" style={{ marginTop: 6 }}>If you approve</div>
                    <div className="mono" style={{ fontSize: 20 }}>{fmt(item.balance_after)}</div>
                  </div>
                </div>

                <div className="grid g2" style={{ marginTop: 14 }}>
                  <DayBreakdown days={item.days} total={r.duration_days} dense />

                  <div>
                    <Copilot advice={item.advice} />
                    {recommended.length > 0 && (
                      <Alert level="warning">
                        <strong>Policy suggests {recommended.map(x => ROLE_LABEL[x.role] || x.role).join(' and ')} review this.</strong>
                        <div className="small" style={{ marginTop: 3 }}>
                          {recommended.map(x => x.reason).join(' ')} You can still approve it
                          yourself — the decision is yours, and it is recorded either way.
                        </div>
                      </Alert>
                    )}
                    <Alert level="info">{item.options.help}</Alert>

                    <label htmlFor={`c${item.step_id}`}>
                      Comment — required to reject, optional otherwise.
                      It travels with the request if you forward it.
                    </label>
                    <textarea id={`c${item.step_id}`} rows={2}
                              value={comments[item.step_id] || ''}
                              onChange={e => setComments(c => ({ ...c, [item.step_id]: e.target.value }))} />

                    <div className="chips" style={{ marginTop: 10 }}>
                      {/* Navy, not orange: approving is a commitment, and the
                          accent is reserved for "the next step", which here
                          may well be forwarding instead. */}
                      <button className="btn btn-navy" disabled={busy === item.step_id}
                              onClick={() => decide(item, 'approved')}>
                        {busy === item.step_id ? 'Saving…' : 'Approve'}
                      </button>

                      {options.map(o => (
                        <button key={o.role}
                                className={`btn ${recommendedRoles.has(o.role) ? 'btn-primary' : ''}`}
                                disabled={busy === item.step_id}
                                title={o.recipient ? `Goes to ${o.recipient}` : undefined}
                                onClick={() => forward(item, o.role)}>
                          {o.label}{o.recipient ? ` · ${o.recipient}` : ''}
                        </button>
                      ))}

                      <button className="btn btn-danger" disabled={busy === item.step_id}
                              onClick={() => decide(item, 'rejected')}>
                        Reject
                      </button>
                    </div>

                    {options.length === 0 && (
                      <div className="small muted" style={{ marginTop: 8 }}>
                        You are the final approver for this request — there is nobody
                        above you to forward it to.
                      </div>
                    )}
                  </div>
                </div>
              </Card>
            )
          })}
        </div>
      )}
    </>
  )
}
