import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { ErrorBox, Loading, usePoll } from '../lib'

const ax = { stroke: '#6b7280', fontSize: 11 }
const tip = { contentStyle: { background: '#ffffff', border: '1px solid #e5e7eb' } }
const COLORS = ['#2f5d8a', '#b42318', '#c47f17', '#1b7f4b', '#6d5a9c', '#2b8a9a', '#a23b72', '#6f9a2b', '#c2622d', '#2a8f82', '#b8a21a']

function Panel({ title, children, note }: { title: string; children: React.ReactNode; note?: string }) {
  return <div className="card"><div className="mb-2 text-sm font-semibold">{title}</div>{children}{note && <div className="mt-2 text-[11px] text-muted">{note}</div>}</div>
}

export default function AnalyticsPage() {
  const trends = usePoll(() => api('/api/v1/analytics/trends?days=14'), 30000)
  const inc = usePoll(() => api('/api/v1/analytics/incidents?days=14'), 30000)
  const fuel = usePoll(() => api('/api/v1/analytics/fuel?days=14'), 30000)
  const bat = usePoll(() => api('/api/v1/analytics/battery'), 30000)
  const safety = usePoll(() => api('/api/v1/analytics/safety?limit=8'), 10000)
  const maint = usePoll(() => api('/api/v1/analytics/maintenance?limit=10'), 30000)
  const dtc = usePoll(() => api('/api/v1/analytics/top-dtc'), 30000)
  const err = [trends, inc, fuel, bat, safety, maint, dtc].find((x) => x.error)
  if (err) return <ErrorBox msg={err.error!} />
  if (!trends.data || !inc.data || !fuel.data) return <Loading what="analytics" />

  const types = [...new Set<string>(inc.data.items.map((r: any) => r.type))]
  const byDay: Record<string, any> = {}
  inc.data.items.forEach((r: any) => { byDay[r.day] ??= { day: r.day.slice(5) }; byDay[r.day][r.type] = r.n })
  const sohBuckets = (bat.data?.soh_distribution ?? []).map((b: any) => ({ range: `${60 + (b.bucket - 1) * 5}–${60 + b.bucket * 5}%`, n: b.n }))
  const riskBuckets = (maint.data?.risk_histogram ?? []).map((b: any) => ({ range: `${(b.bucket - 1) * 10}–${b.bucket * 10}%`, n: b.n }))
  const safetyBuckets = (safety.data?.distribution ?? []).map((b: any) => ({ range: `${(b.bucket - 1) * 20}–${b.bucket * 20}`, n: b.n }))
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Fleet analytics</h1>
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Utilisation — active vehicles & distance per day" note="Source: materialised view mv_fleet_daily_kpis (refreshed concurrently).">
          <ResponsiveContainer width="100%" height={240}><LineChart data={trends.data.items}><CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="day" tickFormatter={(x) => String(x).slice(5)} {...ax} />
            <YAxis yAxisId="l" {...ax} /><YAxis yAxisId="r" orientation="right" {...ax} /><Tooltip {...tip} /><Legend />
            <Line yAxisId="l" dataKey="active_vehicles" stroke="#2f5d8a" name="active vehicles" /><Line yAxisId="r" dataKey="distance_km" stroke="#1b7f4b" name="km" /></LineChart></ResponsiveContainer></Panel>
        <Panel title="Incidents by type (alerts per day)">
          <ResponsiveContainer width="100%" height={240}><BarChart data={Object.values(byDay)}><CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="day" {...ax} /><YAxis {...ax} /><Tooltip {...tip} />
            {types.map((t, i) => <Bar key={t} dataKey={t} stackId="a" fill={COLORS[i % COLORS.length]} />)}</BarChart></ResponsiveContainer></Panel>
        <Panel title="Fuel, energy & idle waste" note={`Idle fuel assumes ${'2.5'} L/h (configurable); price $${fuel.data.fuel_price_per_litre}/L.`}>
          <ResponsiveContainer width="100%" height={240}><BarChart data={fuel.data.items}><CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="day" tickFormatter={(x) => String(x).slice(5)} {...ax} /><YAxis {...ax} /><Tooltip {...tip} /><Legend />
            <Bar dataKey="fuel_litres" fill="#c47f17" name="fuel L" /><Bar dataKey="idle_fuel_litres" fill="#b42318" name="idle fuel L" /><Bar dataKey="energy_kwh" fill="#2b8a9a" name="energy kWh" /></BarChart></ResponsiveContainer></Panel>
        <Panel title="EV battery state of health (distribution)">
          {sohBuckets.length ? <ResponsiveContainer width="100%" height={240}><BarChart data={sohBuckets}><CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="range" {...ax} /><YAxis {...ax} /><Tooltip {...tip} /><Bar dataKey="n" fill="#6d5a9c" name="vehicles" /></BarChart></ResponsiveContainer>
            : <div className="py-10 text-center text-sm text-muted">This tenant has no battery vehicles.</div>}
          {bat.data?.lowest_soh?.length ? <div className="mt-2 text-xs text-muted">Lowest SoH: {bat.data.lowest_soh.slice(0, 3).map((v: any) => <Link key={v.id} to={`/vehicles/${v.id}`} className="mr-2 text-brand">{v.vin.trim()} ({v.soh}%)</Link>)}</div> : null}</Panel>
        <Panel title="Driver safety — risk-score distribution & riskiest vehicles">
          <ResponsiveContainer width="100%" height={170}><BarChart data={safetyBuckets}><CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="range" {...ax} /><YAxis {...ax} /><Tooltip {...tip} /><Bar dataKey="n" fill="#b42318" name="vehicles" /></BarChart></ResponsiveContainer>
          <div className="mt-2 space-y-1 text-xs">{safety.data?.top_risky?.slice(0, 5).map((r: any) => <div key={r.id} className="flex justify-between"><Link to={`/vehicles/${r.id}`} className="font-mono text-brand">{r.vin.trim()}</Link><span>risk {r.risk_score} · {r.harsh_brake_30m} brakes · {r.speeding_30m} speeding</span></div>)}</div></Panel>
        <Panel title="Maintenance — predicted failure-risk distribution (ML, synthetic-trained)">
          <ResponsiveContainer width="100%" height={170}><BarChart data={riskBuckets}><CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="range" {...ax} /><YAxis {...ax} /><Tooltip {...tip} /><Bar dataKey="n" fill="#c47f17" name="vehicles" /></BarChart></ResponsiveContainer>
          <div className="mt-2 space-y-1 text-xs">{maint.data?.top_risk?.slice(0, 4).map((r: any) => <div key={r.id} className="flex justify-between"><Link to={`/vehicles/${r.id}`} className="font-mono text-brand">{r.vin.trim()}</Link><span className="text-warn">{Math.round(Number(r.failure_prob) * 100)}%</span></div>)}</div></Panel>
        <Panel title="Most frequent diagnostic codes (7 days)">
          <div className="space-y-1">{dtc.data?.items?.length ? dtc.data.items.map((r: any) => <div key={r.code} className="flex items-center gap-2 text-sm"><span className="w-16 font-mono">{r.code}</span>
            <div className="h-2 flex-1 rounded bg-panel2"><div className="h-2 rounded bg-warn" style={{ width: `${Math.min(100, r.count / dtc.data.items[0].count * 100)}%` }} /></div><span className="w-12 text-right text-muted">{r.count}</span></div>) : <div className="text-sm text-muted">No repeated DTC alerts.</div>}</div></Panel>
      </div>
    </div>
  )
}
