import { useState } from 'react'
import { useMe } from '../App'
import { api } from '../api'
import { Badge, ErrorBox, Loading, fmtTime, usePoll } from '../lib'

export default function AdminPage() {
  const me = useMe()
  const can = (p: string) => me.permissions.includes(p)
  const scen = usePoll(() => (can('demo:run') ? api('/api/v1/demo/scenarios') : Promise.resolve({ items: [] })), null)
  const audit = usePoll(() => (can('admin:audit') ? api('/api/v1/admin/audit?limit=30') : Promise.resolve(null)), 10000)
  const [msg, setMsg] = useState<string>('')
  const [err, setErr] = useState('')
  const [subject, setSubject] = useState({ subject_type: 'vehicle', subject_ref: '' })

  const run = async (name: string) => {
    setErr(''); setMsg('')
    try { const r = await api(`/api/v1/demo/scenarios/${name}`, { method: 'POST' }); setMsg(`Injected ${r.events_published} events for ${r.vins.length} vehicle(s): ${r.vins.slice(0, 3).join(', ')} — watch Alerts, Map and the vehicle page.`) } catch (e) { setErr((e as Error).message) }
  }
  const erase = async () => {
    if (!confirm(`Permanently erase ${subject.subject_type} ${subject.subject_ref} from every storage tier? This cannot be undone.`)) return
    setErr(''); setMsg('')
    try { const r = await api('/api/v1/admin/erasure', { method: 'POST', json: subject }); setMsg(`Erasure ${r.status}: ${JSON.stringify(r.report)}`); audit.reload() } catch (e) { setErr((e as Error).message) }
  }
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Operations</h1>
      {msg && <div className="rounded border border-green-200 bg-green-50 p-3 text-sm text-ok">{msg}</div>}{err && <ErrorBox msg={err} />}
      <div className="card"><div className="mb-2 text-sm font-semibold">Deterministic demo scenarios</div>
        {can('demo:run') ? <div className="grid gap-2 md:grid-cols-2">{(scen.data?.items ?? []).map((s: any) => <div key={s.name} className="flex items-start justify-between gap-3 rounded border border-line p-3">
          <div><div className="font-mono text-sm">{s.name}</div><div className="text-xs text-muted">{s.description}</div></div><button className="btn shrink-0" onClick={() => run(s.name)}>Run</button></div>)}</div>
          : <div className="text-sm text-muted">Your role cannot run scenarios.</div>}
        <div className="mt-2 text-[11px] text-muted">Events are published to the real ingestion topic and flow through validation → stream processing → alerts. Each scenario drives vehicles of one tenant (engine/dangerous driver: Acme, EV battery: Borealis, idling: Metro).</div></div>
      {can('admin:erasure') && <div className="card"><div className="mb-2 text-sm font-semibold">Right to erasure (GDPR / DPDP)</div>
        <div className="flex flex-wrap gap-2"><select className="input max-w-[140px]" value={subject.subject_type} onChange={(e) => setSubject({ ...subject, subject_type: e.target.value })}><option>vehicle</option><option>driver</option></select>
          <input className="input max-w-xs" placeholder={subject.subject_type === 'vehicle' ? 'VIN' : 'driver id'} value={subject.subject_ref} onChange={(e) => setSubject({ ...subject, subject_ref: e.target.value.trim() })} /><button className="btn" disabled={!subject.subject_ref} onClick={erase}>Erase</button></div></div>}
      {can('admin:audit') && <div className="card"><div className="mb-2 flex items-center gap-2 text-sm font-semibold">Audit log {audit.data && <Badge tone={audit.data.chain_valid ? 'border-green-200 text-ok' : 'border-red-200 text-crit'}>hash chain {audit.data.chain_valid ? 'valid' : 'BROKEN'} · {audit.data.chain_length} entries</Badge>}</div>
        {!audit.data ? <Loading what="audit log" /> : <table className="w-full"><thead><tr>{['Time', 'Actor', 'Action', 'Resource', 'IP'].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead><tbody>
          {audit.data.items.map((a: any) => <tr key={a.id} className="border-t border-line"><td className="td text-xs text-muted">{fmtTime(a.ts)}</td><td className="td">{a.actor_id ?? '—'}</td><td className="td font-mono text-xs">{a.action}</td><td className="td text-xs">{a.resource} {a.resource_id}</td><td className="td text-xs text-muted">{a.ip}</td></tr>)}</tbody></table>}</div>}
    </div>
  )
}
