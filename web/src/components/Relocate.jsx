import { useState } from 'react'
import { api } from '../api'
import { Alert, Card, TypeBadge, fmt, useAsync } from '../components/ui'
import { today } from '../dates'

/* HR relocation.
   ==========================================================================

   Moving someone between regions changes almost everything that is region-
   dependent — the entitlement bracket, the leave year (and therefore which
   accruals are in scope and when carry-over expires), the carry-over cap,
   the public-holiday calendar, and which HR admin their requests route to.

   The one thing it must NOT change is days already earned. So this screen
   previews first and states, per leave type, what will happen and why:

     * monthly types      no adjustment — future accrual switches by itself
     * annual lumps       only the remaining slice of the year is re-priced
     * types the new
       region lacks       balance kept, nothing further accrues

   Preview then apply, because a relocation writes to an append-only ledger
   and "undo" means writing a reversing entry, not deleting a mistake. */
export function Relocate({ employee, onDone }) {
  const { data: meta } = useAsync(() => api.meta())
  const regions = (meta?.regions || []).filter(r => r !== employee.region)
  const [toRegion, setToRegion] = useState('')
  const [on, setOn] = useState(today())
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const run = async (apply) => {
    if (!toRegion) { setErr('Choose a region to move them to.'); return }
    setBusy(true); setErr(null)
    try {
      if (apply) {
        const res = await api.hrRelocate(employee.id, {
          new_region: toRegion, transfer_date: on,
        })
        setResult(res); setPreview(null)
        onDone && onDone()
      } else {
        setPreview(await api.hrRelocationPreview(employee.id, toRegion, on))
        setResult(null)
      }
    } catch (e) { setErr(e.message) }
    finally { setBusy(false) }
  }

  const rows = result?.adjustments || preview?.adjustments || []

  return (
    <Card title="Relocate"
          subtitle={`Currently in ${employee.region}. Moving them re-resolves every region-dependent rule — and leaves days already earned alone.`}>
      {err && <Alert level="danger">{err}</Alert>}

      <div className="row">
        <div className="field">
          <label htmlFor="to-region">Move to</label>
          <select id="to-region" value={toRegion}
                  onChange={e => { setToRegion(e.target.value); setPreview(null); setResult(null) }}>
            <option value="">Choose a region…</option>
            {regions.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="on-date">Effective from</label>
          <input id="on-date" type="date" value={on}
                 onChange={e => { setOn(e.target.value || today()); setPreview(null) }} />
        </div>
      </div>

      <div className="chips">
        <button className="btn" disabled={busy} onClick={() => run(false)}>
          Preview
        </button>
        <button className="btn btn-primary" disabled={busy || !preview}
                onClick={() => run(true)}>
          {busy ? 'Working…' : 'Relocate'}
        </button>
      </div>

      {result && (
        <Alert level="good">
          <strong>{result.name} is now in {result.to_region}.</strong>
          <div className="small" style={{ marginTop: 3 }}>
            Their leave year moves from {result.leave_year_before} to
            {' '}{result.leave_year_after}, so the carry-over deadline, the
            holiday calendar and the approval routing all follow. They have
            been notified.
          </div>
        </Alert>
      )}

      {rows.length > 0 && (
        <>
          {preview && (
            <Alert level="info">
              Nothing has been written yet. This is what pressing Relocate
              would do on {on}.
            </Alert>
          )}
          <table style={{ marginTop: 4 }}>
            <thead>
              <tr>
                <th>Type</th>
                <th className="num">Now</th>
                <th className="num">After</th>
                <th className="num">Adjustment</th>
                <th>Why</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(a => (
                <tr key={a.leave_type_id}>
                  <td><TypeBadge type={a.leave_type_id} /></td>
                  <td className="num">
                    {a.old_entitlement ? `${fmt(a.old_entitlement)}/yr` : '—'}
                  </td>
                  <td className="num">
                    {a.new_entitlement ? `${fmt(a.new_entitlement)}/yr`
                      : <span className="muted">not offered</span>}
                  </td>
                  <td className="num">
                    {Number(a.adjustment) === 0
                      ? <span className="badge badge-approved">
                          <span className="dot" style={{ background: 'currentColor' }} />
                          no change
                        </span>
                      : <strong style={{ color: 'var(--accent)' }}>
                          {Number(a.adjustment) > 0 ? '+' : ''}{fmt(a.adjustment, 3)}
                        </strong>}
                  </td>
                  <td className="small muted" style={{ maxWidth: 420 }}>{a.basis}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Card>
  )
}
