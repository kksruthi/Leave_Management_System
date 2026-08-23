import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { fmtDateTime } from './ui'

/* The notification bell.
   ==========================================================================

   Notifications are written in the same database transaction as the event
   they describe (see `app/notifications.py`), so this list can never show an
   approval that was rolled back. That is why it polls a plain endpoint rather
   than subscribing to anything: there is no delivery step to be unreliable.

   Polling every 45 seconds is a deliberate compromise. An approver waiting on
   a request refreshes anyway; someone who has left the tab open for an hour
   should not have made 3,600 requests. The count also refreshes immediately
   after any action that could produce one, via the `refresh` callback the
   shell passes down. */
const POLL_MS = 45000

export function Bell() {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [data, setData] = useState({ unread: 0, items: [] })
  const box = useRef(null)

  const load = useCallback(async () => {
    try { setData(await api.notifications()) } catch { /* a bell is not worth an error screen */ }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, POLL_MS)
    return () => clearInterval(id)
  }, [load])

  // Click-away and Escape both close it, because a panel you cannot dismiss
  // without hitting the exact button again is a panel people learn to avoid.
  useEffect(() => {
    if (!open) return
    const onDown = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const openPanel = async () => {
    const next = !open
    setOpen(next)
    if (next) await load()
  }

  const click = async (n) => {
    setOpen(false)
    if (!n.read) {
      await api.readNotification(n.id)
      load()
    }
    if (n.link) navigate(n.link)
  }

  const readAll = async () => { await api.readAllNotifications(); load() }

  return (
    <div className="bell" ref={box}>
      <button className="bell-btn" onClick={openPanel}
              aria-label={data.unread ? `Notifications, ${data.unread} unread` : 'Notifications'}
              aria-expanded={open}>
        <span aria-hidden="true">🔔</span>
        {data.unread > 0 && (
          <span className="bell-count">{data.unread > 99 ? '99+' : data.unread}</span>
        )}
      </button>

      {open && (
        <div className="bell-panel" role="dialog" aria-label="Notifications">
          <div className="bell-head">
            <span>Notifications</span>
            {data.unread > 0 && (
              <button className="btn btn-sm btn-ghost" onClick={readAll}>
                Mark all read
              </button>
            )}
          </div>

          {data.items.length === 0 && (
            <div className="empty" style={{ padding: 30 }}>Nothing yet.</div>
          )}

          {data.items.map(n => (
            <button key={n.id} className={`note ${n.read ? '' : 'unread'}`}
                    onClick={() => click(n)}>
              <div className="t">{n.title}</div>
              {n.body && <div className="b">{n.body}</div>}
              <div className="w">{fmtDateTime(n.created_at)}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
