/* Thin fetch wrapper. Holds the token, attaches it, and turns a 401 into a
   single sign-out rather than each screen inventing its own handling. */
const TOKEN_KEY = 'leave.token'

export const token = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (v) => localStorage.setItem(TOKEN_KEY, v),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

let onUnauthorized = () => {}
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn }

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status }
}

async function request(path, { method = 'GET', body, params } = {}) {
  const url = new URL(path, window.location.origin)
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v)
    })
  }
  const headers = { 'Content-Type': 'application/json' }
  const t = token.get()
  if (t) headers.Authorization = `Bearer ${t}`

  const res = await fetch(url, {
    method, headers, body: body ? JSON.stringify(body) : undefined,
  })

  if (res.status === 401) { token.clear(); onUnauthorized(); throw new ApiError('Session expired.', 401) }
  if (res.status === 204) return null

  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail = typeof data.detail === 'string'
      ? data.detail
      : Array.isArray(data.detail) ? data.detail.map(d => d.msg).join('; ') : 'Request failed.'
    throw new ApiError(detail, res.status)
  }
  return data
}

export const api = {
  login: (email, password) => request('/api/auth/login', { method: 'POST', body: { email, password } }),
  me: () => request('/api/auth/me'),
  meta: () => request('/api/meta'),

  myDashboard: (as_of) => request('/api/me/dashboard', { params: { as_of } }),
  myRequests: () => request('/api/me/requests'),
  previewRequest: (body) => request('/api/me/requests/preview', { method: 'POST', body }),
  createRequest: (body) => request('/api/me/requests', { method: 'POST', body }),
  cancelRequest: (id) => request(`/api/me/requests/${id}/cancel`, { method: 'POST' }),

  teamOverview: () => request('/api/team/overview'),
  teamCalendar: (year, month) => request('/api/team/calendar', { params: { year, month } }),
  teamBalances: () => request('/api/team/balances'),
  approvals: () => request('/api/approvals'),
  decide: (stepId, decision, comment) =>
    request(`/api/approvals/${stepId}/decide`, { method: 'POST', body: { decision, comment } }),
  forward: (stepId, to_role, note) =>
    request(`/api/approvals/${stepId}/forward`, { method: 'POST', body: { to_role, note } }),

  notifications: () => request('/api/notifications'),
  readNotification: (id) => request(`/api/notifications/${id}/read`, { method: 'POST' }),
  readAllNotifications: () => request('/api/notifications/read-all', { method: 'POST' }),

  hrOverview: () => request('/api/hr/overview'),
  hrEmployees: (params) => request('/api/hr/employees', { params }),
  hrEmployee: (id) => request(`/api/hr/employees/${id}`),
  hrRelocationPreview: (id, to_region, on) =>
    request(`/api/hr/employees/${id}/relocation-preview`, { params: { to_region, on } }),
  hrRelocate: (id, body) =>
    request(`/api/hr/employees/${id}/relocate`, { method: 'POST', body }),
  hrRequests: (params) => request('/api/hr/requests', { params }),
  hrAudit: (id) => request(`/api/hr/requests/${id}/audit`),
  hrPolicies: (region) => request('/api/hr/policies', { params: { region } }),
  hrPreviewRoll: (body) => request('/api/hr/policies/preview-roll', { method: 'POST', body }),
  hrRollForward: (body) => request('/api/hr/policies/roll-forward', { method: 'POST', body }),
  hrHolidays: (region, year) => request('/api/hr/holidays', { params: { region, year } }),
  hrAddHoliday: (body) => request('/api/hr/holidays', { method: 'POST', body }),
  hrEscalate: () => request('/api/hr/escalate', { method: 'POST' }),

  hrLeaveTypes: () => request('/api/hr/leave-types'),
  hrCreateLeaveType: (body) => request('/api/hr/leave-types', { method: 'POST', body }),
  hrUpdateLeaveType: (code, body) =>
    request(`/api/hr/leave-types/${code}`, { method: 'PATCH', body }),

  hrPolicyStatus: () => request('/api/hr/policy-status'),
  hrPreviewPublish: (body) =>
    request('/api/hr/policies/preview-publish', { method: 'POST', body }),
  hrPublish: (body) => request('/api/hr/policies/publish', { method: 'POST', body }),
  hrPolicyRemind: () => request('/api/hr/policies/remind', { method: 'POST' }),

  simulateOptions: () => request('/api/simulate/options'),
  simulateState: (id) => request(`/api/simulate/${id}`),
  simulateRun: (id, action) => request(`/api/simulate/${id}/${action}`, { method: 'POST' }),
}
