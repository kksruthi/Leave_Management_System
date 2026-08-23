import { fmt, fmtDate } from './ui'

/* The calculation, shown rather than asserted.

   "3 days" is a number an employee has to take on faith. The same request
   rendered day by day — with the public holiday named and the weekend greyed —
   is a number they can check in two seconds. That difference is most of
   whether people trust the system. */
export function DayBreakdown({ days, total, dense = false }) {
  if (!days?.length) return null
  return (
    <div>
      <div className="daylist">
        {days.map((d) => (
          <div key={d.date} className={`dayrow ${d.kind}`}>
            <span className="date">{fmtDate(d.date, { day: '2-digit', month: 'short' })}</span>
            <span className="dow">{d.weekday}</span>
            <span className="label">{d.label}</span>
            <span className="charge">
              {Number(d.charged_days) > 0 ? `${fmt(d.charged_days)} d` : '—'}
            </span>
          </div>
        ))}
        <div className="daytotal">
          <span>Leave requested</span>
          <span className="mono">{fmt(total)} days</span>
        </div>
      </div>
      {!dense && (
        <p className="sub small" style={{ marginTop: 8 }}>
          Weekends and public holidays are not deducted from your balance.
        </p>
      )}
    </div>
  )
}
