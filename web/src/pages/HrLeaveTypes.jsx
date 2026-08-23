import { useState } from 'react'
import { api } from '../api'
import { Alert, Card, Spinner, useAsync } from '../components/ui'

const COLORS = [
  ['series-1', 'Blue'], ['series-2', 'Brick'], ['series-3', 'Green'],
  ['series-4', 'Violet'], ['series-5', 'Magenta'], ['neutral', 'Grey'],
]

/* Leave types, as data.
   ==========================================================================

   "Add Bereavement Leave" used to be a code change: the type codes were
   string literals in the policy seed, the substitution chain, the colour map
   and the UI. Now they are rows, and this screen is where HR adds one.

   The thing worth understanding on this screen is the split:

     * **This page** says what a type IS — its code, its name, its colour.
     * **Policies** say what it is WORTH, per region and tenure bracket.

   Creating a type therefore grants nobody anything. The "Offered in" column
   stays empty until somebody writes a policy for it, and that is the safe
   default — the alternative would be handing a new entitlement to every
   employee in the company the instant a name was typed.

   It is also why Texas has no Casual Leave without any code anywhere saying
   "except Texas": CL exists as a type, and Texas simply has no CL policy.

   Two things are deliberately impossible here: **deleting** a type and
   **renaming its code**. Ledger rows, requests and policies all reference the
   code, and a request from 2024 has to keep meaning what it meant in 2024.
   Retiring removes it from the request form and leaves history intact. */
export default function HrLeaveTypes() {
  const { data, loading, error, reload } = useAsync(() => api.hrLeaveTypes())
  const [msg, setMsg] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(null)

  const act = async (fn, ok) => {
    setErr(null); setMsg(null)
    try { await fn(); setMsg(ok); reload() }
    catch (e) { setErr(e.message) }
    finally { setBusy(null) }
  }

  const toggle = (t) => {
    setBusy(t.code)
    return act(
      () => api.hrUpdateLeaveType(t.code, { is_active: !t.is_active }),
      t.is_active
        ? `${t.code} retired. It has disappeared from the request form; every past `
          + `record still reads correctly.`
        : `${t.code} is offered again.`
    )
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Leave types</h1>
          <p className="sub">
            What kinds of leave exist. What each one is <em>worth</em> is set per
            region on the Policies screen.
          </p>
        </div>
      </div>

      {msg && <Alert level="good">{msg}</Alert>}
      {err && <Alert level="danger">{err}</Alert>}

      <Card>
        {loading ? <Spinner /> : error ? <Alert level="danger">{error}</Alert> : (
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>Type</th><th>Name</th><th>Offered in</th>
                  <th className="num">Policies</th><th className="num">Requests</th>
                  <th>Status</th><th></th>
                </tr>
              </thead>
              <tbody>
                {data.map(t => (
                  <tr key={t.code} style={t.is_active ? undefined : { opacity: .6 }}>
                    <td>
                      <span className="badge badge-type">
                        <span className="dot" style={{ background: `var(--${t.color_token})` }} />
                        {t.code}
                      </span>
                    </td>
                    <td>
                      {t.name}
                      {t.description && <div className="small muted">{t.description}</div>}
                    </td>
                    <td className="small">
                      {t.regions.length ? t.regions.join(', ') : (
                        <span className="muted">nowhere — no policy written yet</span>
                      )}
                    </td>
                    <td className="num">{t.policy_count}</td>
                    <td className="num">
                      {t.request_count}
                      {t.open_request_count > 0 && (
                        <div className="small muted">{t.open_request_count} open</div>
                      )}
                    </td>
                    <td>
                      {t.is_unpaid_fallback
                        ? <span className="badge badge-accent">Unpaid fallback</span>
                        : t.is_active
                          ? <span className="badge badge-approved"><span className="dot" style={{ background: 'currentColor' }} />Offered</span>
                          : <span className="badge badge-cancelled">Retired</span>}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      {!t.is_unpaid_fallback && (
                        <button className="btn btn-sm" disabled={busy === t.code}
                                onClick={() => toggle(t)}>
                          {t.is_active ? 'Retire' : 'Reinstate'}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <NewType onDone={(m) => { setMsg(m); reload() }} />
    </>
  )
}

function NewType({ onDone }) {
  const [form, setForm] = useState({
    code: '', name: '', description: '', color_token: 'series-5',
  })
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setErr(null)
    try {
      const t = await api.hrCreateLeaveType({ ...form, code: form.code.toUpperCase() })
      onDone(`${t.code} created. It is offered to nobody until you write a policy `
             + `for it — add one on the Policies screen.`)
      setForm({ code: '', name: '', description: '', color_token: 'series-5' })
    } catch (e2) { setErr(e2.message) }
    finally { setBusy(false) }
  }

  return (
    <Card title="Add a leave type"
          subtitle="The code is permanent — history references it. Choose it as carefully as a column name.">
      {err && <Alert level="danger">{err}</Alert>}
      <form onSubmit={submit}>
        <div className="row">
          <div className="field" style={{ flex: '0 0 130px' }}>
            <label htmlFor="lt-code">Code</label>
            <input id="lt-code" value={form.code} required maxLength={16}
                   placeholder="BL"
                   onChange={e => set('code', e.target.value.toUpperCase())} />
          </div>
          <div className="field">
            <label htmlFor="lt-name">Display name</label>
            <input id="lt-name" value={form.name} required
                   placeholder="Bereavement Leave"
                   onChange={e => set('name', e.target.value)} />
          </div>
          <div className="field" style={{ flex: '0 0 160px' }}>
            <label htmlFor="lt-colour">Chart colour</label>
            <select id="lt-colour" value={form.color_token}
                    onChange={e => set('color_token', e.target.value)}>
              {COLORS.map(([token, label]) => (
                <option key={token} value={token}>{label}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="field">
          <label htmlFor="lt-desc">Description (shown to employees)</label>
          <input id="lt-desc" value={form.description}
                 placeholder="Paid leave following a death in the immediate family."
                 onChange={e => set('description', e.target.value)} />
        </div>
        <div className="small muted" style={{ marginBottom: 10 }}>
          Colours come from the validated palette, not a free-form picker — every
          slot has been checked for contrast and colour-vision deficiency, and a
          hand-picked hex would quietly break that.
        </div>
        <button className="btn btn-primary" disabled={busy}>
          {busy ? 'Creating…' : 'Create leave type'}
        </button>
      </form>
    </Card>
  )
}
