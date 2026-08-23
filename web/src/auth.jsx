import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { api, token, setUnauthorizedHandler } from './api'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  const signOut = useCallback(() => { token.clear(); setUser(null) }, [])

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null))
    if (!token.get()) { setLoading(false); return }
    api.me().then(setUser).catch(() => token.clear()).finally(() => setLoading(false))
  }, [])

  const signIn = async (email, password) => {
    const res = await api.login(email, password)
    token.set(res.access_token)
    setUser(res.user)
    return res.user
  }

  // `can` reads the permission list the server sent, so the UI and the API
  // agree on what a role means without the frontend re-deriving it.
  const can = (permission) => !!user?.permissions?.includes(permission)

  return (
    <AuthContext.Provider value={{ user, loading, signIn, signOut, can }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)
