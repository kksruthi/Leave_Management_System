import { useState } from 'react'
import { fmtDate } from './ui'

/* The approval copilot panel.
   ==========================================================================

   The one thing a manager needs to decide a leave request is who else is
   already off — and it is the one thing the screen never showed them. This
   panel puts the count in front of the decision.

   It leads with a verdict because a manager scanning eight requests needs a
   sort order, and it shows the working underneath because a verdict nobody
   can check is a verdict that gets followed blindly. Everything here is a
   query result; there is no model in the loop and no guessing.

   The verdict never disables a button. It is advice about cover, from someone
   who counted — the manager still decides. */
const STYLE = {
  safe:  { cls: 'good',    icon: '✓', label: 'Safe to approve' },
  check: { cls: 'warning', icon: '!', label: 'Worth a check' },
  risky: { cls: 'danger',  icon: '⚠', label: 'Thin cover' },
}

export function Copilot({ advice }) {
  const [open, setOpen] = useState(false)
  if (!advice) return null
  const style = STYLE[advice.verdict] || STYLE.check

  return (
    <div className={`alert alert-${style.cls}`} style={{ display: 'block' }}>
      <div className="between" style={{ alignItems: 'flex-start' }}>
        <div>
          <strong>
            <span aria-hidden="true">{style.icon}</span> {style.label}
          </strong>
          <div style={{ marginTop: 2 }}>{advice.headline}</div>
        </div>
        <button type="button" className="btn btn-sm btn-ghost"
                onClick={() => setOpen(o => !o)}>
          {open ? 'Hide' : 'Why?'}
        </button>
      </div>

      {/* The coverage bar. One person per cell, so "3 of 5" is a shape rather
          than a sentence to parse. */}
      <div style={{ display: 'flex', gap: 3, marginTop: 9 }}>
        {Array.from({ length: advice.team_size }, (_, i) => (
          <span key={i} title={i < advice.peak_away ? 'away' : 'working'}
                style={{
                  flex: 1, height: 7, borderRadius: 3,
                  background: i < advice.peak_away
                    ? 'currentColor'
                    : 'color-mix(in srgb, currentColor 22%, transparent)',
                }} />
        ))}
      </div>
      <div className="small" style={{ marginTop: 4, opacity: .85 }}>
        {advice.peak_away} away · {advice.remaining} working · team of {advice.team_size}
      </div>

      {open && (
        <div style={{ marginTop: 10 }}>
          <ul style={{ margin: '0 0 10px', paddingLeft: 18 }}>
            {advice.signals.map((s, i) => (
              <li key={i} style={{ marginBottom: 5 }}>
                <strong>{s.label}</strong>
                <div className="small" style={{ opacity: .9 }}>{s.detail}</div>
              </li>
            ))}
          </ul>

          {advice.overlapping.length > 0 && (
            <>
              <div className="small" style={{ fontWeight: 650, marginBottom: 4 }}>
                Overlapping leave
              </div>
              <table style={{ marginBottom: 10 }}>
                <tbody>
                  {advice.overlapping.map((o, i) => (
                    <tr key={i}>
                      <td className="small">{o.employee}</td>
                      <td className="small mono">{o.leave_type_id}</td>
                      <td className="small">
                        {fmtDate(o.start_date, { day: 'numeric', month: 'short' })}
                        {' – '}
                        {fmtDate(o.end_date, { day: 'numeric', month: 'short' })}
                      </td>
                      <td className="small">
                        {o.status === 'pending'
                          ? <span className="badge badge-pending">pending</span>
                          : <span className="badge badge-approved">approved</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          <div className="small" style={{ opacity: .75 }}>{advice.basis}</div>
        </div>
      )}
    </div>
  )
}
