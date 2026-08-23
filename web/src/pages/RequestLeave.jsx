import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { DayBreakdown } from '../components/DayBreakdown'
import { Alert, Card, TypeBadge, fmt, LEAVE_NAME, useAsync } from '../components/ui'
import { today } from '../dates'

export default function RequestLeave() {
  const navigate = useNavigate()
  const { data: meta } = useAsync(() => api.meta())
  const [form, setForm] = useState({
    leave_type_id: 'EL', start_date: today(), end_date: today(),
    start_half_day: false, end_half_day: false, reason: '', override_reason: '',
  })
  const [preview, setPreview] = useState(null)
  const [checking, setChecking] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [useOverride, setUseOverride] = useState(false)

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  // Live preview: the employee sees the working before they commit to it.
  useEffect(() => {
    if (!form.start_date || !form.end_date) return
    let live = true
    setChecking(true)
    const body = { ...form, override_reason: useOverride ? (form.override_reason || 'Override requested') : null }
    api.previewRequest(body)
      .then(p => live && setPreview(p))
      .catch(e => live && setError(e.message))
      .finally(() => live && setChecking(false))
    return () => { live = false }
  }, [form.leave_type_id, form.start_date, form.end_date,
      form.start_half_day, form.end_half_day, form.override_reason, useOverride])

  const submit = async (e) => {
    e.preventDefault()
    setSubmitting(true); setError(null)
    try {
      const body = { ...form, override_reason: useOverride ? (form.override_reason || 'Override requested') : null }
      const created = await api.createRequest(body)
      navigate(`/requests?highlight=${created.id}`)
    } catch (err) { setError(err.message) }
    finally { setSubmitting(false) }
  }

  const c = preview?.classification
  const blocked = preview && !preview.can_submit

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Request leave</h1>
          <p className="sub">Every figure below updates as you change the dates.</p>
        </div>
      </div>

      {error && <Alert level="danger">{error}</Alert>}

      <div className="grid g2">
        <Card title="Your request">
          <form onSubmit={submit}>
            <div className="field">
              <label htmlFor="type">Leave type</label>
              <select id="type" value={form.leave_type_id}
                      onChange={e => set('leave_type_id', e.target.value)}>
                {(meta?.leave_types || ['EL']).map(t => (
                  <option key={t} value={t}>{LEAVE_NAME[t] || t} ({t})</option>
                ))}
              </select>
            </div>

            <div className="row">
              <div className="field">
                <label htmlFor="from">From</label>
                <input id="from" type="date" value={form.start_date}
                       onChange={e => set('start_date', e.target.value)} required />
                <label className="check" style={{ marginTop: 7 }}>
                  <input type="checkbox" checked={form.start_half_day}
                         onChange={e => set('start_half_day', e.target.checked)} />
                  Half day
                </label>
              </div>
              <div className="field">
                <label htmlFor="to">To</label>
                <input id="to" type="date" value={form.end_date} min={form.start_date}
                       onChange={e => set('end_date', e.target.value)} required />
                <label className="check" style={{ marginTop: 7 }}>
                  <input type="checkbox" checked={form.end_half_day}
                         onChange={e => set('end_half_day', e.target.checked)} />
                  Half day
                </label>
              </div>
            </div>

            <div className="field">
              <label htmlFor="reason">Reason / comment</label>
              <textarea id="reason" rows={2} value={form.reason}
                        placeholder="Visible to you and HR — not shown to your manager."
                        onChange={e => set('reason', e.target.value)} />
            </div>

            {blocked && (
              <div className="field">
                <label className="check">
                  <input type="checkbox" checked={useOverride}
                         onChange={e => setUseOverride(e.target.checked)} />
                  Request an exception to the rules above
                </label>
                {useOverride && (
                  <textarea rows={2} style={{ marginTop: 7 }} value={form.override_reason}
                            placeholder="Why does this need to bypass the policy? Approvers will see this."
                            onChange={e => set('override_reason', e.target.value)} />
                )}
              </div>
            )}

            <button className="btn btn-primary" disabled={submitting || checking || !preview?.can_submit}>
              {submitting ? 'Submitting…' : 'Submit request'}
            </button>
            {checking && <span className="muted small" style={{ marginLeft: 10 }}>Checking…</span>}
          </form>
        </Card>

        <div className="stack">
          <Card title="How this is counted"
                subtitle="Weekends and public holidays are excluded automatically.">
            {preview?.days?.length
              ? <DayBreakdown days={preview.days} total={preview.duration_days} />
              : <p className="muted small">Pick your dates to see the breakdown.</p>}
          </Card>

          {preview && (
            <Card title="What it costs you">
              {preview.blockers.map((b, i) => (
                <Alert key={i} level="danger">{b}</Alert>
              ))}
              {preview.warnings.map((w, i) => (
                <Alert key={i} level="warning">{w}</Alert>
              ))}

              {c && (
                <table style={{ marginTop: 6 }}>
                  <tbody>
                    <tr>
                      <td>Paid days</td>
                      <td className="num"><strong>{fmt(c.paid_days)}</strong></td>
                    </tr>
                    <tr>
                      <td>Unpaid days</td>
                      <td className="num" style={Number(c.unpaid_days) > 0
                        ? { color: 'var(--critical)', fontWeight: 600 } : undefined}>
                        {fmt(c.unpaid_days)}
                      </td>
                    </tr>
                    {c.draws.filter(d => Number(d.days) > 0).map(d => (
                      <tr key={d.leave_type_id}>
                        <td className="small muted" style={{ paddingLeft: 22 }}>
                          drawn from <TypeBadge type={d.leave_type_id} />
                        </td>
                        <td className="num small muted">{fmt(d.days)}</td>
                      </tr>
                    ))}
                    <tr>
                      <td>{form.leave_type_id} balance before</td>
                      <td className="num">{fmt(preview.balance_before)}</td>
                    </tr>
                    <tr>
                      <td><strong>Balance after approval</strong></td>
                      <td className="num"><strong>{fmt(preview.balance_after)}</strong></td>
                    </tr>
                  </tbody>
                </table>
              )}

              {c?.reason && <p className="sub small" style={{ marginTop: 10 }}>{c.reason}</p>}
            </Card>
          )}
        </div>
      </div>
    </>
  )
}
