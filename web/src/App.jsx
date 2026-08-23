import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './auth'
import { Bell } from './components/Bell'
import { Spinner, ThemeToggle } from './components/ui'
import Login from './pages/Login'
import MyLeave from './pages/MyLeave'
import RequestLeave from './pages/RequestLeave'
import MyRequests from './pages/MyRequests'
import Approvals from './pages/Approvals'
import { TeamOverview, TeamCalendarPage, TeamBalances } from './pages/Team'
import { HrOverview, HrEmployees, HrEmployee, HrRequests, HrAudit } from './pages/Hr'
import { HrPolicies, HrHolidays } from './pages/HrPolicies'
import HrLeaveTypes from './pages/HrLeaveTypes'
import Simulate from './pages/Simulate'

/* Navigation is derived from PERMISSIONS the server sent, not from a role
   string. A screen appears because the caller holds the permission it needs,
   which is the same thing the API checks — so the menu can never offer
   something the API will refuse. */
const NAV = [
  { group: null, items: [
    { to: '/', label: 'My leave', end: true, need: 'view_own_leave' },
    { to: '/request', label: 'Request leave', need: 'create_leave_request' },
    { to: '/requests', label: 'My requests', need: 'view_own_leave' },
  ]},
  { group: 'Team', items: [
    { to: '/team', label: 'Overview', need: 'view_team_leave' },
    { to: '/team/calendar', label: 'Team calendar', need: 'view_team_leave' },
    { to: '/team/balances', label: 'Team balances', need: 'view_team_leave' },
    { to: '/approvals', label: 'Approvals', need: 'approve_team_leave', badge: true },
  ]},
  { group: 'Administration', items: [
    { to: '/hr', label: 'Organisation', end: true, need: 'view_all_leave' },
    { to: '/hr/employees', label: 'Employees', need: 'manage_employees' },
    { to: '/hr/requests', label: 'Leave requests', need: 'view_all_leave' },
    { to: '/hr/policies', label: 'Policies', need: 'manage_policies' },
    { to: '/hr/leave-types', label: 'Leave types', need: 'manage_policies' },
    { to: '/hr/holidays', label: 'Holiday calendar', need: 'manage_holidays' },
    { to: '/hr/simulate', label: 'Simulation', need: 'view_all_leave' },
  ]},
]

export default function App() {
  const { user, loading, signOut, can } = useAuth()

  if (loading) return <Spinner label="Loading…" />
  if (!user) return <Login />

  const groups = NAV
    .map(g => ({ ...g, items: g.items.filter(i => can(i.need)) }))
    .filter(g => g.items.length)

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div>
            Leave Management
            <span>Software</span>
          </div>
        </div>

        <nav className="nav">
          {groups.map((g, i) => (
            <div key={i}>
              {g.group && <div className="nav-group">{g.group}</div>}
              {g.items.map(item => (
                <NavLink key={item.to} to={item.to} end={item.end}
                         className={({ isActive }) => isActive ? 'active' : ''}>
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div style={{ marginBottom: 8 }}>
            <strong>{user.name}</strong>
            <div className="muted small">
              {user.role === 'hr_admin' ? 'HR admin' : user.role}
              {user.delegating_to && <> · delegating to {user.delegating_to.name}</>}
            </div>
          </div>
          <div className="chips">
            <ThemeToggle />
            <button className="btn btn-sm" onClick={signOut}>Sign out</button>
          </div>
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <Bell />
        </div>
        <Routes>
          <Route path="/" element={<MyLeave />} />
          <Route path="/request" element={<RequestLeave />} />
          <Route path="/requests" element={<MyRequests />} />

          <Route path="/team" element={<Guard need="view_team_leave"><TeamOverview /></Guard>} />
          <Route path="/team/calendar" element={<Guard need="view_team_leave"><TeamCalendarPage /></Guard>} />
          <Route path="/team/balances" element={<Guard need="view_team_leave"><TeamBalances /></Guard>} />
          <Route path="/approvals" element={<Guard need="approve_team_leave"><Approvals /></Guard>} />

          <Route path="/hr" element={<Guard need="view_all_leave"><HrOverview /></Guard>} />
          <Route path="/hr/employees" element={<Guard need="manage_employees"><HrEmployees /></Guard>} />
          <Route path="/hr/employees/:id" element={<Guard need="view_all_leave"><HrEmployee /></Guard>} />
          <Route path="/hr/requests" element={<Guard need="view_all_leave"><HrRequests /></Guard>} />
          <Route path="/hr/requests/:id" element={<Guard need="view_audit"><HrAudit /></Guard>} />
          <Route path="/hr/policies" element={<Guard need="manage_policies"><HrPolicies /></Guard>} />
          <Route path="/hr/leave-types" element={<Guard need="manage_policies"><HrLeaveTypes /></Guard>} />
          <Route path="/hr/holidays" element={<Guard need="manage_holidays"><HrHolidays /></Guard>} />
          <Route path="/hr/simulate" element={<Guard need="view_all_leave"><Simulate /></Guard>} />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}

/* Client-side guard. Cosmetic only — the API enforces the same rule, so a
   hand-typed URL gets a 403 rather than data. This exists to avoid rendering
   a screen that would only fail. */
function Guard({ need, children }) {
  const { can } = useAuth()
  return can(need) ? children : <Navigate to="/" replace />
}
