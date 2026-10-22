import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

/** Poll an async loader every `ms` (and on mount). Keeps the previous data while refreshing; exposes error + loading. */
export function usePoll<T>(load: () => Promise<T>, ms: number | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const loadRef = useRef(load)
  loadRef.current = load
  const run = useCallback(async () => {
    try { setData(await loadRef.current()); setError(null) } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }, [])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setLoading(true); run(); if (!ms) return; const t = setInterval(run, ms); return () => clearInterval(t) }, [ms, run, ...deps])
  return { data, error, loading, reload: run }
}

export const sevColor = (s: string) => s === 'critical' ? 'bg-red-50 text-crit border-red-200' : s === 'warning' ? 'bg-amber-50 text-warn border-amber-200' : 'bg-blue-50 text-brand border-blue-200'

export function Badge({ children, tone }: { children: ReactNode; tone: string }) {
  return <span className={`inline-block rounded-sm border px-1.5 py-px text-xs ${tone}`}>{children}</span>
}

export const SevBadge = ({ s }: { s: string }) => <Badge tone={sevColor(s)}>{s}</Badge>

export function Kpi({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="card">
      <div className="text-xs text-muted">{label}</div>
      <div className={`mt-0.5 text-xl font-semibold tabular-nums ${tone ?? ''}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </div>
  )
}

export function Loading({ what = 'data' }: { what?: string }) { return <div className="p-6 text-sm text-muted">Loading {what}…</div> }
export function ErrorBox({ msg }: { msg: string }) { return <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-crit">{msg}</div> }

export const healthTone = (h: number | null | undefined) => h == null ? 'text-muted' : h < 50 ? 'text-crit' : h < 75 ? 'text-warn' : 'text-ok'
export const pct = (x: number | string | null | undefined) => x == null ? '—' : `${Math.round(Number(x) * 100)}%`
export const fmtTime = (iso?: string | number | null) => iso ? new Date(iso).toLocaleString() : '—'
export const ago = (ms: number) => { const s = Math.max(0, Math.round((Date.now() - ms) / 1000)); return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago` }

/** Minimal, safe markdown subset (bold, inline code, bullet/numbered lines). Never injects raw HTML. */
export function Markdown({ text }: { text: string }) {
  const inline = (s: string): ReactNode[] => s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((p, i) =>
    p.startsWith('**') ? <strong key={i}>{p.slice(2, -2)}</strong> : p.startsWith('`') ? <code key={i} className="rounded bg-panel2 px-1 text-[0.85em] text-blue-300">{p.slice(1, -1)}</code> : p)
  return <div className="space-y-1 text-sm leading-relaxed">{text.split('\n').map((l, i) =>
    /^\s*(-|\d+\.)\s/.test(l) ? <div key={i} className="ml-3 flex gap-2"><span className="text-muted">{l.trim().match(/^(-|\d+\.)/)![0].replace('-', '•')}</span><span>{inline(l.replace(/^\s*(-|\d+\.)\s/, ''))}</span></div>
      : l.trim() ? <p key={i}>{inline(l)}</p> : <div key={i} className="h-1" />)}</div>
}
