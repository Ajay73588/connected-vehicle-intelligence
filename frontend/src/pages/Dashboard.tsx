import { Area, AreaChart, Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { ErrorBox, Kpi, Loading, SevBadge, healthTone, usePoll } from '../lib'

const axis = { stroke: '#6b7280', fontSize: 11 }

export default function DashboardPage() {
  const s = usePoll(() => api('/api/v1/analytics/summary'), 3000)
  const live = usePoll(() => api('/api/v1/analytics/live?hours=12'), 5000)
  const trends = usePoll(() => api('/api/v1/analytics/trends?days=14'), 30000)
  const alerts = usePoll(() => api('/api/v1/alerts?status=open&limit=8'), 4000)
  const risk = usePoll(() => api('/api/v1/analytics/maintenance?limit=6'), 15000)

  if (s.error) return <ErrorBox msg={s.error} />
  if (!s.data) return <Loading what="dashboard" />
  const d = s.data
  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between">
        <h1 className="text-xl font-semibold">Executive dashboard</h1>
        <span className="text-xs text-muted">live · refreshes every 3 s</span>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-5">
        <Kpi label="Total vehicles" value={d.total_vehicles.toLocaleString()} sub={`${d.active_vehicles.toLocaleString()} reporting in the last 5 min`} />
        <Kpi label="Open alerts" value={d.open_alerts.total.toLocaleString()} tone={d.open_alerts.critical ? 'text-crit' : ''} sub={`${d.open_alerts.critical} critical · ${d.open_alerts.warning} warning`} />
        <Kpi label="Fleet health" value={d.fleet_health.avg_score} tone={healthTone(d.fleet_health.avg_score)} sub={`${d.fleet_health.vehicles_below_60} vehicles below 60`} />
        <Kpi label="Safety score" value={d.safety.score} tone={healthTone(d.safety.score)} sub={`${d.safety.high_risk_vehicles} high-risk drivers/vehicles`} />
        <Kpi label="Maintenance risk" value={d.maintenance_risk.vehicles_over_50pct} sub="vehicles with >50% 14-day failure risk" tone={d.maintenance_risk.vehicles_over_50pct ? 'text-warn' : ''} />
        <Kpi label="Utilisation today" value={`${d.utilization.pct}%`} sub={`${d.utilization.vehicles_moving_today.toLocaleString()} vehicles moved · ${d.utilization.distance_km.toLocaleString()} km`} />
        <Kpi label="Fuel / energy today" value={`${d.energy.fuel_litres.toLocaleString()} L`} sub={`${d.energy.energy_kwh.toLocaleString()} kWh (EV)`} />
        <Kpi label="Idling today" value={`${d.idling.hours} h`} sub={`≈ ${d.idling.est_fuel_litres} L · $${d.idling.est_cost_usd}`} />
        <Kpi label="Events this hour" value={d.realtime.events_this_hour.toLocaleString()} sub={`${d.realtime.harsh_events_this_hour} harsh · ${d.realtime.speeding_this_hour} speeding samples`} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card"><div className="mb-2 text-sm font-semibold">Telemetry ingested per hour (UTC)</div>
          {live.data ? <ResponsiveContainer width="100%" height={200}><AreaChart data={live.data.items}>
            <CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="hour" tickFormatter={(h) => String(h).slice(8) + 'h'} {...axis} /><YAxis {...axis} />
            <Tooltip contentStyle={{ background: '#ffffff', border: '1px solid #e5e7eb' }} />
            <Area dataKey="events" stroke="#2f5d8a" fill="#2f5d8a33" name="events" /><Area dataKey="harsh" stroke="#c47f17" fill="#c47f1733" name="harsh events" /></AreaChart></ResponsiveContainer> : <Loading />}
        </div>
        <div className="card"><div className="mb-2 text-sm font-semibold">Distance driven & active vehicles (14 days)</div>
          {trends.data ? <ResponsiveContainer width="100%" height={200}><BarChart data={trends.data.items}>
            <CartesianGrid stroke="#e5e7eb" strokeDasharray="3 3" /><XAxis dataKey="day" tickFormatter={(x) => String(x).slice(5)} {...axis} /><YAxis {...axis} />
            <Tooltip contentStyle={{ background: '#ffffff', border: '1px solid #e5e7eb' }} /><Bar dataKey="distance_km" fill="#2f5d8a" name="km" /></BarChart></ResponsiveContainer> : <Loading />}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="card"><div className="mb-2 flex items-center justify-between text-sm font-semibold">Latest open alerts <Link to="/alerts" className="text-xs text-brand">all alerts →</Link></div>
          {alerts.data ? <div className="divide-y divide-line">{alerts.data.items.map((a: any) => (
            <Link key={a.id} to={`/vehicles/${a.vehicle_id}`} className="block py-2 hover:bg-panel2">
              <div className="flex items-center gap-2"><SevBadge s={a.severity} /><span className="text-sm font-medium">{a.title}</span><span className="ml-auto font-mono text-xs text-muted">{a.vin.trim()}</span></div>
              <div className="mt-1 text-xs text-muted">{a.cause}</div></Link>))}
            {!alerts.data.items.length && <div className="py-4 text-sm text-muted">No open alerts.</div>}</div> : <Loading />}
        </div>
        <div className="card"><div className="mb-2 text-sm font-semibold">Highest predicted failure risk (ML)</div>
          {risk.data ? <table className="w-full"><thead><tr><th className="th">Vehicle</th><th className="th">Model</th><th className="th">14-day risk</th><th className="th">Drivers</th></tr></thead><tbody>
            {risk.data.top_risk.map((r: any) => <tr key={r.id} className="border-t border-line"><td className="td"><Link className="font-mono text-xs text-brand" to={`/vehicles/${r.id}`}>{r.vin.trim()}</Link></td>
              <td className="td text-xs">{r.make} {r.model}</td><td className="td font-semibold text-warn">{Math.round(Number(r.failure_prob) * 100)}%</td>
              <td className="td text-xs text-muted">{(r.top_factors ?? []).slice(0, 2).map((f: any) => f.label).join('; ')}</td></tr>)}</tbody></table> : <Loading />}
          <div className="mt-2 text-[11px] text-muted">Model trained on synthetic data — see docs/ml.md.</div>
        </div>
      </div>
    </div>
  )
}
