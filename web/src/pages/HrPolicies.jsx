import { useState } from 'react'
import { api } from '../api'
import { Alert, Card, Empty, Spinner, TypeBadge, fmt, fmtDate, useAsync } from '../components/ui'

export function HrPolicies() {
  const { data: meta } = useAsync(() => api.meta())
  const [region, setRegion] = useState('India-TamilNadu')
  const [showHistory, setShowHistory] = useState(false)
  /* Bumped after a publish so BOTH the policy table and the status panel
     re-fetch. Reloading only the table is why the Policy year card kept
     showing "open-ended" after HR had just published a year — the data had
     changed, the panel had not been asked again. */
  const [version, setVersion] = useState(0)
  const { data, loading, error, reload } = useAsync(
    () => api.hrPolicies(region), [region, version]
  )

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Policies</h1>
          <p className="sub">
            A policy row is never edited once used — a change creates a new version.
          </p>
        </div>
      </div>

      <Card>
        <div className="row" style={{ marginBottom: 12 }}>
          <select value={region} onChange={e => setRegion(e.target.value)}>
            {(meta?.regions || []).map(r => <option key={r}>{r}</option>)}
          </select>
          <label className="check" style={{ flex: 'none', alignSelf: 'center' }}>
            <input type="checkbox" checked={showHistory}
                   onChange={e => setShowHistory(e.target.checked)} />
            Show superseded versions
          </label>
        </div>

        {loading ? <Spinner /> : error ? <Alert level="danger">{error}</Alert> : (
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>Type</th><th>Tenure</th><th className="num">Days/yr</th>
                  <th>Accrual</th><th className="num">Carry-over</th><th className="num">Notice</th>
                  <th className="num">Max</th><th>Enforcement</th><th>Effective</th><th>Version</th>
                </tr>
              </thead>
              <tbody>
                {data.filter(p => showHistory || p.is_current).map(p => (
                  <tr key={p.id} style={p.is_current ? undefined : { opacity: .55 }}>
                    <td><TypeBadge type={p.leave_type_id} /></td>
                    <td className="small mono">
                      {fmt(p.tenure_min_years, 0)}–{p.tenure_max_years ? fmt(p.tenure_max_years, 0) : '∞'} yr
                    </td>
                    <td className="num">{fmt(p.entitlement_days_per_year)}</td>
                    <td className="small">{p.accrual_method}</td>
                    <td className="num">
                      {fmt(p.carryover_max_days)}
                      {p.carryover_expiry && <div className="small muted">exp {p.carryover_expiry}</div>}
                    </td>
                    <td className="num">{p.min_notice_days}d</td>
                    <td className="num">{p.max_consecutive_days ?? '—'}</td>
                    <td className="small">
                      {p.enforcement}
                      {p.allow_backdated && <div className="small muted">backdating ok</div>}
                    </td>
                    <td className="small mono">
                      {fmtDate(p.effective_from, { day: '2-digit', month: 'short', year: 'numeric' })}
                      {' → '}
                      {p.effective_to
                        ? fmtDate(p.effective_to, { day: '2-digit', month: 'short', year: 'numeric' })
                        : 'open'}
                    </td>
                    <td className="small">
                      {p.policy_year || '—'}
                      {p.supersedes_id && <div className="small muted">← #{p.supersedes_id}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <PolicyYearStatus region={region} version={version} />
      <RollForward region={region} policies={data} onDone={() => setVersion(v => v + 1)} />
    </>
  )
}

/* Where this region stands in its yearly cycle.
   ==========================================================================

   A policy now has a one-year TERM. That makes "the previous year's policy no
   longer applies" true in the database rather than only in a paragraph — and
   it also means that if nobody publishes the next year, leave stops
   calculating on the day the term ends.

   That cliff is the consequence the rule demands; silently extending an
   expired policy would mean accruing against numbers nobody approved. So the
   cliff is never a surprise: this card counts down, and the server writes a
   notification to every HR admin and director at 90, 30 and 7 days. */
function PolicyYearStatus({ region, version }) {
  const { data, loading, reload } = useAsync(() => api.hrPolicyStatus(), [version])
  const [msg, setMsg] = useState(null)
  if (loading || !data) return null
  const s = data.find(x => x.region === region)
  if (!s) return null

  const level = { expired: 'danger', renewal_due: 'warning', open_ended: 'warning',
                  unconfigured: 'danger', renewed: 'good', active: 'info' }[s.state] || 'info'

  const remind = async () => {
    const res = await api.hrPolicyRemind()
    setMsg(res.sent ? `${res.sent} reminder(s) sent.` : 'No region is inside a reminder window.')
    reload()
  }

  return (
    <Card title="Policy year" subtitle={`${region} · the term this region is currently running.`}>
      <Alert level={level}>{s.headline}</Alert>
      {msg && <Alert level="good">{msg}</Alert>}
      <div className="grid g4" style={{ marginTop: 10 }}>
        <div className="stat">
          <div className="label">Policy year</div>
          <div className="value">{s.policy_year ?? '—'}</div>
          <div className="foot">{s.leave_type_ids.join(' · ') || 'nothing in force'}</div>
        </div>
        <div className="stat">
          <div className="label">Term start</div>
          <div className="value" style={{ fontSize: 20 }}>{s.term_start ? fmtDate(s.term_start) : '—'}</div>
        </div>
        <div className="stat">
          <div className="label">Term end</div>
          <div className="value" style={{ fontSize: 20 }}>
            {s.term_end ? fmtDate(s.term_end) : 'open-ended'}
          </div>
        </div>
        <div className={`stat ${s.state === 'renewal_due' || s.state === 'expired' ? 'emphasis' : ''}`}>
          <div className="label">Days remaining</div>
          <div className="value">{s.days_remaining ?? '—'}</div>
          <div className="foot">reminders at 90 / 30 / 7</div>
        </div>
      </div>
      <div className="chips" style={{ marginTop: 12 }}>
        <button className="btn btn-sm" onClick={remind}>Run the reminder sweep now</button>
      </div>
    </Card>
  )
}

function RollForward({ region, policies, onDone }) {
  const thisYear = new Date().getFullYear()
  const [fromYear, setFromYear] = useState(thisYear)
  const [leaveYearEnd, setLeaveYearEnd] = useState(region.startsWith('India') ? '03-31' : '12-31')
  const [rows, setRows] = useState([{ target: '', field: 'entitlement_days_per_year', value: '' }])

  /* Every policy row currently in force, as a pick-list. A leave type has one
     entry per TENURE BAND, plus an "all bands" entry — because "raise EL for
     5+ years" and "raise EL for everybody" are different decisions and the
     form previously only offered the second one. */
  const targets = []
  const seenType = new Set()
  ;(policies || []).filter(p => p.is_current).forEach(p => {
    if (!seenType.has(p.leave_type_id)) {
      seenType.add(p.leave_type_id)
      targets.push({ key: p.leave_type_id, label: `${p.leave_type_id} — all tenure bands` })
    }
    const min = fmt(p.tenure_min_years, 0)
    const band = p.tenure_max_years ? `${min}–${fmt(p.tenure_max_years, 0)} yr` : `${min}+ yr`
    targets.push({
      key: `${p.leave_type_id}@${min}`,
      label: `${p.leave_type_id} — ${band} (now ${fmt(p.entitlement_days_per_year)}/yr)`,
    })
  })
  const [reason, setReason] = useState('')
  const [plan, setPlan] = useState(null)
  const [msg, setMsg] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  const buildChanges = () => {
    const changes = {}
    rows.filter(r => r.target && r.field && r.value !== '').forEach(r => {
      changes[r.target] = { ...(changes[r.target] || {}), [r.field]: r.value }
    })
    return Object.keys(changes).length ? changes : null
  }

  const run = async (apply) => {
    setBusy(true); setErr(null); setMsg(null)
    const body = {
      region, from_year: Number(fromYear), changes: buildChanges(),
      change_reason: reason || 'Annual policy review', leave_year_end: leaveYearEnd,
    }
    try {
      if (apply) {
        const res = await api.hrPublish(body)
        setMsg(
          `Published leave year ${res.policy_year}: ${res.created} policy rows, `
          + `in force until ${res.term_end}. Everyone in ${region} has been notified.`
        )
        setPlan(null); onDone()
      } else {
        setPlan(await api.hrPreviewPublish(body))
      }
    } catch (e) { setErr(e.message) }
    finally { setBusy(false) }
  }

  return (
    <Card title="Publish next leave year"
          subtitle="Carry this year forward unchanged, or raise specific numbers. Either way a new version is written, given a one-year term, and announced to the region.">
      {msg && <Alert level="good">{msg}</Alert>}
      {err && <Alert level="danger">{err}</Alert>}

      <div className="row">
        <div className="field">
          <label>Closing leave year</label>
          <input type="number" value={fromYear} onChange={e => setFromYear(e.target.value)} />
        </div>
        <div className="field">
          <label>Leave year ends (MM-DD)</label>
          <input value={leaveYearEnd} onChange={e => setLeaveYearEnd(e.target.value)} />
        </div>
      </div>

      <label>Changes (leave empty to continue unchanged)</label>
      {rows.map((r, i) => (
        <div className="row" key={i} style={{ marginBottom: 8 }}>
          <select value={r.target}
                  onChange={e => setRows(rs => rs.map((x, j) => j === i ? { ...x, target: e.target.value } : x))}>
            <option value="">Choose what to change…</option>
            <option value="*">Every policy in the region</option>
            {targets.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}
          </select>
          <select value={r.field}
                  onChange={e => setRows(rs => rs.map((x, j) => j === i ? { ...x, field: e.target.value } : x))}>
            {['entitlement_days_per_year', 'carryover_max_days', 'min_notice_days',
              'max_consecutive_days', 'enforcement', 'proration_method',
              'rounding_dp', 'compliance_note'].map(f => <option key={f}>{f}</option>)}
          </select>
          <input placeholder="New value" value={r.value}
                 onChange={e => setRows(rs => rs.map((x, j) => j === i ? { ...x, value: e.target.value } : x))} />
        </div>
      ))}
      <button className="btn btn-sm" type="button"
              onClick={() => setRows(rs => [...rs, { target: '', field: 'entitlement_days_per_year', value: '' }])}>
        + Another change
      </button>

      <div className="field" style={{ marginTop: 12 }}>
        <label>Reason (recorded on every new row)</label>
        <input value={reason} onChange={e => setReason(e.target.value)}
               placeholder="e.g. FY2027 annual policy review" />
      </div>

      <div className="chips">
        <button className="btn" disabled={busy} onClick={() => run(false)}>Preview</button>
        <button className="btn btn-primary" disabled={busy || !plan} onClick={() => run(true)}>
          Publish new version
        </button>
      </div>

      {plan && (
        <div style={{ marginTop: 14 }}>
          <Alert level="info">
            {plan.region}: leave year {plan.from_year} → {plan.to_year}, running
            {' '}{fmtDate(plan.term_start)} to {fmtDate(plan.term_end)}.
            <strong> {plan.changed_count}</strong> of {plan.changes.length} rows change.
            The {plan.from_year} rows close the day before the new term starts and
            stop resolving from then on.
          </Alert>
          {(plan.warnings || []).map((w, i) => <Alert key={i} level="warning">{w}</Alert>)}
          <table>
            <thead><tr><th>Policy</th><th>Change</th></tr></thead>
            <tbody>
              {plan.changes.map(c => {
                const entries = Object.entries(c.changed || {})
                return (
                  <tr key={c.policy_id}>
                    <td>{c.leave_type_id} <span className="muted small">{c.tenure}</span></td>
                    <td className={entries.length ? 'small' : 'muted small'}>
                      {entries.length
                        ? entries.map(([k, [a, b]]) => `${k}: ${a} → ${b}`).join(', ')
                        : 'carried forward unchanged'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

/* ------------------------------------------------------------- holidays */
export function HrHolidays() {
  const { data: meta } = useAsync(() => api.meta())
  const [region, setRegion] = useState('India-TamilNadu')
  const [year, setYear] = useState(new Date().getFullYear())
  const { data, loading, error, reload } = useAsync(
    () => api.hrHolidays(region, year), [region, year])
  const [form, setForm] = useState({ holiday_date: '', name: '', is_working_day: false })
  const [msg, setMsg] = useState(null)

  const add = async (e) => {
    e.preventDefault()
    await api.hrAddHoliday({ region, ...form })
    setMsg(`Saved ${form.name}.`)
    setForm({ holiday_date: '', name: '', is_working_day: false })
    reload()
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Holiday calendar</h1>
          <p className="sub">
            Statutory days come from the <code>holidays</code> library. Company days are added here.
          </p>
        </div>
      </div>

      <div className="grid g2">
        <Card>
          <div className="row" style={{ marginBottom: 12 }}>
            <select value={region} onChange={e => setRegion(e.target.value)}>
              {(meta?.calendar_regions || []).map(r => <option key={r}>{r}</option>)}
            </select>
            <input type="number" value={year} onChange={e => setYear(Number(e.target.value))} />
          </div>

          {loading ? <Spinner /> : error ? <Alert level="danger">{error}</Alert> : (
            <>
              <p className="sub small">Source: {data.source}</p>
              {!data.holidays.length ? <Empty>No holidays found.</Empty> : (
                <table>
                  <thead><tr><th>Date</th><th>Name</th><th>Kind</th></tr></thead>
                  <tbody>
                    {data.holidays.map(h => (
                      <tr key={h.date}>
                        <td className="mono small">{fmtDate(h.date)}</td>
                        <td>{h.name}</td>
                        <td>
                          <span className={`badge badge-${h.kind === 'company' ? 'type' : 'approved'}`}>
                            <span className="dot" style={{ background: 'currentColor' }} />{h.kind}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </Card>

        <Card title="Add a company holiday"
              subtitle="Or mark a statutory day as one this employer works.">
          {msg && <Alert level="good">{msg}</Alert>}
          <form onSubmit={add}>
            <div className="field">
              <label>Date</label>
              <input type="date" required value={form.holiday_date}
                     onChange={e => setForm(f => ({ ...f, holiday_date: e.target.value }))} />
            </div>
            <div className="field">
              <label>Name</label>
              <input required value={form.name} placeholder="e.g. Founders Day"
                     onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />
            </div>
            <label className="check" style={{ marginBottom: 12 }}>
              <input type="checkbox" checked={form.is_working_day}
                     onChange={e => setForm(f => ({ ...f, is_working_day: e.target.checked }))} />
              This is actually a working day (override the library)
            </label>
            <button className="btn btn-primary">Save</button>
          </form>

          {data?.overrides?.length > 0 && (
            <table style={{ marginTop: 14 }}>
              <thead><tr><th>Company entries</th><th /></tr></thead>
              <tbody>
                {data.overrides.map(o => (
                  <tr key={o.id}>
                    <td>{o.name}<div className="small muted">{fmtDate(o.date)}</div></td>
                    <td className="num small">{o.is_working_day ? 'working day' : 'holiday'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </>
  )
}
