/* Calendar dates, not instants.

   `new Date().toISOString().slice(0, 10)` is the bug this file exists to
   prevent. `toISOString()` converts to UTC first, so for anyone east of
   Greenwich it returns *yesterday* for most of the evening, and for anyone
   west of it, *tomorrow* in the small hours. This product runs in
   India-TamilNadu (UTC+05:30) and USA-Texas (UTC-05:00/-06:00): both are
   wrong for part of every day, in opposite directions.

   A leave date is a calendar date. It has no time zone, no instant, and no
   business being routed through UTC. Everything here reads the local
   calendar fields the browser already has and formats them by hand.

   The server speaks the same language: it stores DATE columns and compares
   them against a region's local calendar, so an ISO `YYYY-MM-DD` string is
   the whole contract between the two. */

/** Local calendar date of a Date object as `YYYY-MM-DD`. Never uses UTC. */
export function toISODate(d) {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** Today, where the user actually is. */
export const today = () => toISODate(new Date())

/**
 * Parse `YYYY-MM-DD` into a Date pinned to local midnight.
 *
 * `new Date('2026-08-18')` is parsed as UTC midnight by spec, which lands on
 * the 17th in Texas. Appending the time makes it a local-time literal.
 */
export const parseISODate = (iso) => new Date(iso + 'T00:00:00')

/** `iso` shifted by `n` days, still as a calendar date. */
export function addDays(iso, n) {
  const d = parseISODate(iso)
  d.setDate(d.getDate() + n)     // handles month ends and DST for us
  return toISODate(d)
}

/** Whole days from `a` to `b`, inclusive of both ends. */
export function inclusiveDayCount(a, b) {
  // Compare at noon so a DST transition inside the range cannot round the
  // division to the wrong integer.
  const ms = 43200000
  return Math.round(
    (parseISODate(b).getTime() - parseISODate(a).getTime() + ms - ms) / 86400000
  ) + 1
}
