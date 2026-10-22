import { useEffect, useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Link, useParams } from 'react-router-dom'
import { api, qs } from '../api'
import { Badge, ErrorBox, Kpi, Loading, SevBadge, fmtTime, healthTone, usePoll } from '../lib'

export function VehiclesPage() {
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('')
  const [maxHealth, setMaxHealth] = useState('')
  const [items, setItems] = useState<any[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)

  const load = async (reset: boolean) => {
    setLoading(true)
    try {
      const r = await api('/api/v1/vehicles' + qs({ q: q.length >= 3 ? q : undefined, status, max_health: maxHealth, limit: 50, cursor: reset ? undefined : cursor }))
      setItems(reset ? r.items : [...items, ...r.items]); setCursor(r.next_cursor); setErr('')
    } catch (e) { setErr((e as Error).message) } finally { setLoading(false) }
  }
  useEffect(() => { const t = setTimeout(() => load(true), 250); return () => clearTimeout(t) }, [q, status, maxHealth]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold">Vehicles</h1>
      <div className="flex flex-wrap gap-2">
        <input className="input max-w-xs" placeholder="VIN prefix (min 3 chars)" value={q} onChange={(e) => setQ(e.target.value.toUpperCase())} maxLength={17} />
        <select className="input max-w-[160px]" value={status} onChange={(e) => setStatus(e.target.value)}><option value="">any status</option><option>active</option><option>maintenance</option><option>retired</option></select>
        <select className="input max-w-[200px]" value={maxHealth} onChange={(e) => setMaxHealth(e.target.value)}><option value="">any health</option><option value="49">critical (&lt;50)</option><option value="74">degraded (&lt;75)</option></select>
      </div>
      {err && <ErrorBox msg={err} />}
      <div className="card overflow-x-auto p-0"><table className="w-full"><thead><tr>{['VIN', 'Vehicle', 'Fleet', 'Fuel', 'Health', 'Driver risk', '14-day failure risk', 'Open alerts'].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead><tbody>
        {items.map((v) => <tr key={v.id} className="border-t border-line hover:bg-panel2">
          <td className="td"><Link className="font-mono text-xs text-brand" to={`/vehicles/${v.id}`}>{v.vin.trim()}</Link></td>
          <td className="td">{v.make} {v.model} <span className="text-muted">{v.model_year}</span></td><td className="td text-muted">{v.fleet}</td><td className="td"><Badge tone="border-line text-gray-700">{v.fuel_type}</Badge></td>
          <td className={`td font-semibold ${healthTone(v.health_score)}`}>{v.health_score ?? '—'}</td><td className="td">{v.risk_score ?? '—'}</td>
          <td className="td">{v.failure_prob != null ? <span className={Number(v.failure_prob) > 0.5 ? 'font-semibold text-warn' : ''}>{Math.round(Number(v.failure_prob) * 100)}%</span> : '—'}</td>
          <td className="td">{v.open_alerts ? <span className="text-warn">{v.open_alerts}</span> : 0}</td></tr>)}
      </tbody></table>{loading && <Loading what="vehicles" />}{!loading && !items.length && <div className="p-6 text-sm text-muted">No vehicles match.</div>}</div>
      {cursor && <button className="btn-ghost" disabled={loading} onClick={() => load(false)}>Load more (keyset pagination)</button>}
    </div>
  )
}

export function VehicleDetailPage() {
  const { id } = useParams()
  const d = usePoll(() => api(`/api/v1/vehicles/${id}`), 3000, [id])
  const tele = usePoll(() => api(`/api/v1/vehicles/${id}/telemetry?minutes=60&limit=300`), 5000, [id])
  const maint = usePoll(() => api(`/api/v1/vehicles/${id}/maintenance`), 30000, [id])
  const hist = usePoll(() => api(`/api/v1/vehicles/${id}/history?hours=24`), 60000, [id])
  if (d.error) return <ErrorBox msg={d.error} />
  if (!d.data) return <Loading what="vehicle" />
  const { vehicle: v, prediction, driver, recent_alerts, live } = d.data
  const factors = Object.entries(v.factors ?? {}) as [string, number][]
  const events = (tele.data?.events ?? []).map((e: any) => ({ t: new Date(e.ts).toLocaleTimeString(), speed: e.speed_kmh, coolant: e.coolant_temp_c, oil: e.oil_pressure_kpa, soh: e.battery_soh_pct, batt: e.battery_temp_c }))
  const dtc: Record<string, number> = {}
  ;(tele.data?.events ?? []).forEach((e: any) => (e.dtc ?? []).forEach((c: string) => { dtc[c] = (dtc[c] ?? 0) + 1 }))
  return (
    <div className="space-y-4">
      <div><Link to="/vehicles" className="text-xs text-brand">← vehicles</Link>
        <h1 className="text-xl font-semibold">{v.make} {v.model} {v.model_year} <span className="ml-2 font-mono text-sm text-muted">{v.vin.trim()}</span></h1>
        <div className="text-xs text-muted">{v.fleet} · {v.fuel_type} · {v.vehicle_class} · {driver ? `driver ${driver.display_name}` : 'no assigned driver'}</div></div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Kpi label="Health score" value={v.health_score ?? '—'} tone={healthTone(v.health_score)} sub={factors.map(([k, x]) => `${k.replace(/_/g, ' ')} −${Math.round(x)}`).join(', ') || 'no active penalties'} />
        <Kpi label="Driver risk" value={v.risk_score ?? '—'} sub={`${v.harsh_brake_30m ?? 0} harsh brakes · ${v.harsh_accel_30m ?? 0} harsh accel · ${v.speeding_30m ?? 0} speeding (30 min)`} />
        <Kpi label="14-day failure risk" value={prediction ? `${Math.round(Number(prediction.failure_prob) * 100)}%` : '—'} tone={prediction && Number(prediction.failure_prob) > 0.5 ? 'text-warn' : ''} sub={prediction?.model_version} />
        <Kpi label="Live" value={live ? `${live.speed_kmh} km/h` : 'offline'} sub={live ? `odometer ${Number(live.odometer_km).toFixed(0)} km · ${fmtTime(Number(live.ts))}` : 'no recent telemetry'} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card"><div className="mb-2 text-sm font-semibold">Live signals (last 60 min, raw telemetry from MongoDB)</div>
          {events.length ? <ResponsiveContainer width="100%" height={220}><LineChart data={events}><CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="t" stroke="#6b7280" fontSize={10} minTickGap={30} /><YAxis stroke="#6b7280" fontSize={11} />
            <Tooltip contentStyle={{ background: '#ffffff', border: '1px solid #e5e7eb' }} />
            <Line dataKey="speed" stroke="#2f5d8a" dot={false} name="speed km/h" /><Line dataKey="coolant" stroke="#b42318" dot={false} name="coolant °C" /><Line dataKey="oil" stroke="#1b7f4b" dot={false} name="oil kPa" />
            <Line dataKey="soh" stroke="#6d5a9c" dot={false} name="battery SoH %" /><Line dataKey="batt" stroke="#c47f17" dot={false} name="battery °C" /></LineChart></ResponsiveContainer> : <div className="py-10 text-center text-sm text-muted">No telemetry in the last hour (start the simulator or run a scenario).</div>}
        </div>
        <div className="card"><div className="mb-2 text-sm font-semibold">Historical signals (warm/cold Parquet via DuckDB, hourly)</div>
          {hist.data?.points?.length ? <ResponsiveContainer width="100%" height={220}><LineChart data={hist.data.points.map((p: any) => ({ ...p, h: p.hour.slice(11, 16) }))}><CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="h" stroke="#6b7280" fontSize={11} /><YAxis stroke="#6b7280" fontSize={11} />
            <Tooltip contentStyle={{ background: '#ffffff', border: '1px solid #e5e7eb' }} /><Line dataKey="avg_speed" stroke="#2f5d8a" name="avg speed" /><Line dataKey="max_coolant" stroke="#b42318" name="max coolant" /></LineChart></ResponsiveContainer>
            : <div className="py-10 text-center text-sm text-muted">{hist.loading ? 'Loading…' : 'No archived history yet (Parquet files are written every few seconds once events flow).'}</div>}
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="card lg:col-span-2"><div className="mb-2 text-sm font-semibold">Alerts</div>
          {recent_alerts.length ? <div className="divide-y divide-line">{recent_alerts.map((a: any) => <div key={a.id} className="py-2"><div className="flex items-center gap-2"><SevBadge s={a.severity} /><b className="text-sm">{a.alert_type_code.replace(/_/g, ' ')}</b>
            <Badge tone="border-line text-muted">{a.status}</Badge><span className="ml-auto text-xs text-muted">confidence {Math.round(Number(a.confidence) * 100)}% · {fmtTime(a.event_ts)}</span></div>
            <div className="mt-1 text-xs text-gray-700">{a.cause}</div><div className="text-xs text-ok">→ {a.recommended_action}</div></div>)}</div> : <div className="text-sm text-muted">No alerts for this vehicle.</div>}
        </div>
        <div className="space-y-4">
          <div className="card"><div className="mb-2 text-sm font-semibold">Diagnostics (DTC, last hour)</div>
            {Object.keys(dtc).length ? Object.entries(dtc).map(([c, n]) => <div key={c} className="flex justify-between text-sm"><span className="font-mono">{c}</span><span className="text-warn">{n}×</span></div>) : <div className="text-sm text-muted">No trouble codes.</div>}</div>
          <div className="card"><div className="mb-2 text-sm font-semibold">Maintenance prediction</div>
            {prediction ? <><div className="text-sm">Failure probability within {prediction.horizon_days} days: <b className="text-warn">{Math.round(Number(prediction.failure_prob) * 100)}%</b></div>
              <ul className="mt-2 space-y-1 text-xs text-muted">{(prediction.top_factors ?? []).map((f: any) => <li key={f.feature}>• {f.label} ({f.value}) — {Math.round(f.risk_contribution * 100)} pts</li>)}</ul></> : <div className="text-sm text-muted">Not scored yet.</div>}
            <div className="mt-3 border-t border-line pt-2 text-xs text-muted">Last services: {(maint.data?.records ?? []).slice(0, 2).map((r: any) => `${r.performed_at} (${r.kind})`).join(', ') || '—'}</div></div>
        </div>
      </div>
    </div>
  )
}
