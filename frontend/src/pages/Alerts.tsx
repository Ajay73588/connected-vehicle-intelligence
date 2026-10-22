import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMe } from '../App'
import { api, qs } from '../api'
import { Badge, ErrorBox, Loading, SevBadge, fmtTime } from '../lib'

export default function AlertsPage() {
  const me = useMe()
  const [severity, setSeverity] = useState('')
  const [status, setStatus] = useState('open')
  const [items, setItems] = useState<any[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState<number | null>(null)

  const load = async (reset: boolean, cur?: string | null) => {
    setLoading(true)
    try {
      const r = await api('/api/v1/alerts' + qs({ severity, status, limit: 40, cursor: reset ? undefined : cur }))
      setItems((prev) => (reset ? r.items : [...prev, ...r.items])); setCursor(r.next_cursor); setErr('')
    } catch (e) { setErr((e as Error).message) } finally { setLoading(false) }
  }
  useEffect(() => { load(true); const t = setInterval(() => load(true), 5000); return () => clearInterval(t) }, [severity, status]) // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (id: number, s: 'acknowledged' | 'resolved') => {
    await api(`/api/v1/alerts/${id}`, { method: 'PATCH', json: { status: s } }); load(true)
  }
  const canWrite = me.permissions.includes('alerts:write')
  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold">Alerts</h1>
      <div className="flex flex-wrap gap-2">
        <select className="input max-w-[160px]" value={severity} onChange={(e) => setSeverity(e.target.value)}><option value="">all severities</option><option>critical</option><option>warning</option><option>info</option></select>
        <select className="input max-w-[160px]" value={status} onChange={(e) => setStatus(e.target.value)}><option value="">all statuses</option><option>open</option><option>acknowledged</option><option>resolved</option></select>
      </div>
      {err && <ErrorBox msg={err} />}
      <div className="card overflow-x-auto p-0"><table className="w-full"><thead><tr>{['Severity', 'Time', 'Vehicle', 'Alert / cause', 'Confidence', 'Recommended action', 'Status', ''].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead><tbody>
        {items.map((a) => <tr key={a.id} className="border-t border-line align-top hover:bg-panel2">
          <td className="td"><SevBadge s={a.severity} /></td><td className="td whitespace-nowrap text-xs text-muted">{fmtTime(a.event_ts)}</td>
          <td className="td"><Link className="font-mono text-xs text-brand" to={`/vehicles/${a.vehicle_id}`}>{a.vin.trim()}</Link></td>
          <td className="td"><div className="font-medium">{a.title}</div><div className="text-xs text-muted">{a.cause}</div>
            {open === a.id && <pre className="mt-1 overflow-x-auto rounded bg-panel2 p-2 text-[11px] text-muted">{JSON.stringify(a.details, null, 1)}</pre>}
            <button className="mt-1 text-[11px] text-brand" onClick={() => setOpen(open === a.id ? null : a.id)}>{open === a.id ? 'hide' : 'evidence'}</button></td>
          <td className="td">{Math.round(Number(a.confidence) * 100)}%</td><td className="td max-w-xs text-xs text-ok">{a.recommended_action}</td>
          <td className="td"><Badge tone="border-line text-gray-700">{a.status}</Badge></td>
          <td className="td whitespace-nowrap">{canWrite && a.status === 'open' && <button className="btn-ghost mr-1" onClick={() => act(a.id, 'acknowledged')}>Ack</button>}
            {canWrite && a.status !== 'resolved' && <button className="btn-ghost" onClick={() => act(a.id, 'resolved')}>Resolve</button>}</td></tr>)}
      </tbody></table>{loading && !items.length && <Loading what="alerts" />}{!loading && !items.length && <div className="p-6 text-sm text-muted">No alerts match.</div>}</div>
      {cursor && <button className="btn-ghost" onClick={() => load(false, cursor)}>Load more</button>}
    </div>
  )
}
