import { fmtDate, leaveColor, LEAVE_NAME } from './ui'
import { toISODate, parseISODate } from '../dates'

/* Per-person bars across one month.

   Bars are positioned by day index rather than drawn per-cell so a single
   absence reads as one continuous block — which is the thing a manager is
   actually scanning for. Every bar carries its leave-type label: the
   light-mode aqua used for SL sits below 3:1 against the surface, so the
   palette's relief rule applies and identity must not rest on hue alone.
   Pending leave is hatched as well as labelled, so "not yet approved" is
   visible without relying on opacity. */
export function TeamCalendar({ data }) {
  if (!data) return null
  const { first_day, last_day, employees, holidays, conflicts } = data
  const start = parseISODate(first_day)
  const end = parseISODate(last_day)
  const dayCount = Math.round((end - start) / 86400000) + 1
  const holidayDates = new Set(holidays.map(h => h.date))
  const conflictDates = new Set(conflicts.map(c => c.date))

  const dayList = Array.from({ length: dayCount }, (_, i) => {
    const d = new Date(start); d.setDate(start.getDate() + i)
    const iso = toISODate(d)
    return { iso, num: d.getDate(), weekend: [0, 6].includes(d.getDay()) }
  })

  const index = (iso) => {
    const d = parseISODate(iso)
    return Math.max(0, Math.min(dayCount - 1, Math.round((d - start) / 86400000)))
  }

  const types = [...new Set(employees.flatMap(e => e.entries.map(x => x.leave_type_id)))]

  return (
    <div className="calwrap">
      <div className="calgrid">
        <div className="calhead" style={{ marginBottom: 4 }}>
          <div className="calname muted small">Team member</div>
          <div className="caldays" style={{
            gridTemplateColumns: `repeat(${dayCount}, 1fr)`, height: 20 }}>
            {dayList.map(d => (
              <div key={d.iso}
                   className={`caldaycell${d.weekend ? ' we' : ''}${holidayDates.has(d.iso) ? ' hol' : ''}`}
                   title={holidayDates.has(d.iso)
                     ? holidays.find(h => h.date === d.iso)?.name : undefined}>
                {d.num}
              </div>
            ))}
          </div>
        </div>

        {employees.map(emp => (
          <div className="calrow" key={emp.id}>
            <div className="calname" title={emp.name}>{emp.name}</div>
            <div className="caldays" style={{ gridTemplateColumns: `repeat(${dayCount}, 1fr)` }}>
              {dayList.map(d => (
                <div key={d.iso}
                     className={`caldaycell${d.weekend ? ' we' : ''}${holidayDates.has(d.iso) ? ' hol' : ''}`} />
              ))}
              {emp.entries.map(entry => {
                const from = index(entry.start_date)
                const to = index(entry.end_date)
                const span = to - from + 1
                return (
                  <div
                    key={entry.id}
                    className={`calbar ${entry.status}`}
                    style={{
                      left: `${(from / dayCount) * 100}%`,
                      width: `calc(${(span / dayCount) * 100}% - 2px)`,
                      background: leaveColor(entry.leave_type_id),
                    }}
                    title={`${emp.name} · ${LEAVE_NAME[entry.leave_type_id] || entry.leave_type_id} · `
                      + `${fmtDate(entry.start_date)} – ${fmtDate(entry.end_date)} · ${entry.status}`}
                  >
                    {span >= 2 ? entry.leave_type_id : ''}
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="legend">
        {types.map(t => (
          <span className="item" key={t}>
            <span className="swatch" style={{ background: leaveColor(t) }} />
            {LEAVE_NAME[t] || t}
          </span>
        ))}
        <span className="item">
          <span className="swatch" style={{
            background: 'var(--muted)',
            backgroundImage: 'repeating-linear-gradient(135deg,transparent,transparent 3px,rgba(255,255,255,.5) 3px,rgba(255,255,255,.5) 6px)',
          }} />
          Hatched = pending approval
        </span>
        <span className="item">
          <span className="swatch" style={{ background: 'color-mix(in srgb, var(--serious) 30%, transparent)' }} />
          Public holiday
        </span>
      </div>

      {conflicts.length > 0 && (
        <p className="sub small" style={{ marginTop: 10 }}>
          <strong>{conflictDates.size} day{conflictDates.size === 1 ? '' : 's'}</strong> with
          more than one person away — {conflicts.slice(0, 4).map(c =>
            `${fmtDate(c.date, { day: 'numeric', month: 'short' })} (${c.people.join(', ')})`
          ).join('; ')}{conflicts.length > 4 ? '…' : ''}
        </p>
      )}
    </div>
  )
}
