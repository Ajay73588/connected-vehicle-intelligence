// Typed API client. Tokens live in sessionStorage (cleared when the tab closes) and are refreshed once on a 401.
export interface Tokens { access_token: string; refresh_token: string }
export interface Me { user_id: number; tenant: string; email: string; roles: string[]; permissions: string[] }

const KEY = 'cvi.tokens'
let tokens: Tokens | null = (() => {
  try { return JSON.parse(sessionStorage.getItem(KEY) ?? 'null') } catch { return null }
})()

export const setTokens = (t: Tokens | null) => {
  tokens = t
  if (t) sessionStorage.setItem(KEY, JSON.stringify(t)); else sessionStorage.removeItem(KEY)
}
export const hasTokens = () => tokens !== null

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

let refreshing: Promise<boolean> | null = null
async function refresh(): Promise<boolean> {
  if (!tokens) return false
  refreshing ??= fetch('/auth/refresh', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: tokens.refresh_token }),
  }).then(async (r) => {
    if (!r.ok) { setTokens(null); return false }
    setTokens(await r.json()); return true
  }).finally(() => { refreshing = null })
  return refreshing
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}, retry = true): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) }
  if (init.json !== undefined) headers['Content-Type'] = 'application/json'
  if (tokens) headers.Authorization = `Bearer ${tokens.access_token}`
  const res = await fetch(path, { ...init, headers, body: init.json !== undefined ? JSON.stringify(init.json) : init.body })
  if (res.status === 401 && retry && tokens && !path.startsWith('/auth/')) {
    if (await refresh()) return api<T>(path, init, false)
    window.dispatchEvent(new Event('cvi:logout'))
  }
  if (!res.ok) {
    let msg = res.statusText
    try { const b = await res.json(); msg = typeof b.detail === 'string' ? b.detail : JSON.stringify(b.detail ?? b) } catch { /* not json */ }
    throw new ApiError(res.status, msg)
  }
  return res.json() as Promise<T>
}

export async function login(email: string, password: string): Promise<Me> {
  const t = await api<Tokens>('/auth/login', { method: 'POST', json: { email, password } }, false)
  setTokens(t)
  return api<Me>('/auth/me')
}

export const qs = (o: Record<string, unknown>) =>
  '?' + Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join('&')
