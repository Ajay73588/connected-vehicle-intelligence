import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { api, hasTokens, login, setTokens, type Me } from './api'
import { Badge, ErrorBox } from './lib'
import AdminPage from './pages/Admin'
import AlertsPage from './pages/Alerts'
import AnalyticsPage from './pages/Analytics'
import CopilotPage from './pages/Copilot'
import DashboardPage from './pages/Dashboard'
import MapPage from './pages/MapPage'
import { VehicleDetailPage, VehiclesPage } from './pages/Vehicles'

const MeCtx = createContext<Me | null>(null)
export const useMe = () => useContext(MeCtx)!

function Login({ onLogin }: { onLogin: (m: Me) => void }) {
  const [email, setEmail] = useState('fleet_manager@acme.demo')
  const [password, setPassword] = useState('Demo@12345')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setErr('')
    try { onLogin(await login(email, password)) } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  const demo = [['fleet_manager@acme.demo', 'Acme Logistics · fleet manager'], ['admin@acme.demo', 'Acme Logistics · admin'], ['analyst@acme.demo', 'Acme · analyst (masked location)'],
    ['fleet_manager@borealis.demo', 'Borealis EV · fleet manager'], ['fleet_manager@metro.demo', 'Metro Transit · fleet manager']]
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-md space-y-4">
        <div><div className="text-xl font-semibold">Fleet console</div>
          <p className="mt-0.5 text-sm text-muted">Sign in to continue</p></div>
        <form onSubmit={submit} className="card space-y-3">
          <input className="input" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email" autoComplete="username" />
          <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="password" autoComplete="current-password" />
          {err && <ErrorBox msg={err} />}
          <button className="btn w-full" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
        </form>
        <div className="card text-xs text-muted">
          <div className="mb-2 font-semibold text-gray-700">Demo accounts (synthetic data) — password <code>Demo@12345</code></div>
          {demo.map(([e, d]) => <button key={e} onClick={() => setEmail(e)} className="block w-full rounded px-2 py-1 text-left hover:bg-panel2">{e} <span className="text-muted">— {d}</span></button>)}
        </div>
      </div>
    </div>
  )
}

const NAV = [['/', 'Dashboard'], ['/map', 'Live map'], ['/vehicles', 'Vehicles'], ['/alerts', 'Alerts'], ['/analytics', 'Analytics'], ['/copilot', 'AI Copilot'], ['/admin', 'Operations']]

function Shell({ me, onLogout }: { me: Me; onLogout: () => void }) {
  return (
    <MeCtx.Provider value={me}>
      <div className="flex h-full">
        <aside className="flex w-52 shrink-0 flex-col border-r border-line bg-white">
          <div className="border-b border-line px-4 py-3 text-sm font-semibold">Fleet console</div>
          <nav className="flex-1 py-2">
            {NAV.map(([to, label]) => (
              <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => `block border-l-2 px-4 py-1.5 text-sm ${isActive ? 'border-brand bg-gray-50 font-medium text-gray-900' : 'border-transparent text-gray-600 hover:bg-gray-50'}`}>{label}</NavLink>
            ))}
          </nav>
          <div className="space-y-1 border-t border-line p-3 text-xs text-muted">
            <div className="truncate text-gray-700">{me.email}</div>
            <div>tenant <Badge tone="border-line text-gray-700">{me.tenant}</Badge></div>
            <div>{me.roles.join(', ')}</div>
            <button onClick={onLogout} className="btn-ghost mt-2 w-full">Sign out</button>
          </div>
        </aside>
        <main className="min-w-0 flex-1 overflow-y-auto p-4">
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/map" element={<MapPage />} />
            <Route path="/vehicles" element={<VehiclesPage />} />
            <Route path="/vehicles/:id" element={<VehicleDetailPage />} />
            <Route path="/alerts" element={<AlertsPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/copilot" element={<CopilotPage />} />
            <Route path="/admin" element={<AdminPage />} />
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </main>
      </div>
    </MeCtx.Provider>
  )
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [ready, setReady] = useState(!hasTokens())
  const nav = useNavigate()
  const logout = useCallback(() => { setTokens(null); setMe(null); nav('/') }, [nav])
  useEffect(() => { if (hasTokens()) api<Me>('/auth/me').then(setMe).catch(() => setTokens(null)).finally(() => setReady(true)) }, [])
  useEffect(() => { window.addEventListener('cvi:logout', logout); return () => window.removeEventListener('cvi:logout', logout) }, [logout])
  if (!ready) return <div className="p-6 text-muted">Loading…</div>
  return me ? <Shell me={me} onLogout={logout} /> : <Login onLogin={setMe} />
}
