import { useState } from 'react'
import { useAuth } from '../auth'
import { Alert, ThemeToggle } from '../components/ui'

/* The sign-in screen deliberately shows NO credentials.
   ==========================================================================

   It used to list four demo accounts with the shared password printed
   underneath. That is convenient exactly once and a liability afterwards:
   every screenshot, every screen-share, every recorded demo hands out a
   working login. The review asked for it to go, and it is gone.

   The demo accounts still exist — `python seed_users.py` prints the full list
   of 29 with the shared password when it runs, which is the right place for
   it: a terminal the operator already controls, not a page anyone can reach.

   The password field also no longer arrives pre-filled. */
export default function Login() {
  const { signIn } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setError(null)
    try { await signIn(email.trim(), password) }
    catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  return (
    <div className="login-wrap">
      <div>
        <div className="login-brand">
          Leave Management Software
        </div>

        <div className="login-card card">
          <div className="between" style={{ marginBottom: 14 }}>
            <div>
              <h2 style={{ marginBottom: 2 }}>Sign in</h2>
              <p className="sub small">Use your work email address.</p>
            </div>
            <ThemeToggle />
          </div>

          {error && <Alert level="danger">{error}</Alert>}

          <form onSubmit={submit}>
            <div className="field">
              <label htmlFor="email">Work email</label>
              <input id="email" type="email" value={email} autoComplete="username"
                     placeholder="name@northbridge.example" autoFocus
                     onChange={e => setEmail(e.target.value)} required />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input id="password" type="password" value={password}
                     autoComplete="current-password"
                     onChange={e => setPassword(e.target.value)} required />
            </div>
            <button className="btn btn-primary" style={{ width: '100%' }} disabled={busy}>
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        </div>

        <div className="login-foot">
          Trouble signing in? Contact your HR administrator.
        </div>
      </div>
    </div>
  )
}
