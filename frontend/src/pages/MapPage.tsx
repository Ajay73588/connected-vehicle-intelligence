import { useCallback, useEffect, useRef, useState } from 'react'
import { Circle, CircleMarker, MapContainer, TileLayer, useMap, useMapEvents } from 'react-leaflet'
import { Link } from 'react-router-dom'
import { api, qs } from '../api'
import { Badge, SevBadge, healthTone, sevColor } from '../lib'

const COLOR: Record<string, string> = { critical: '#b42318', warning: '#c47f17', ok: '#1b7f4b' }

function Viewport({ onBounds }: { onBounds: (b: any) => void }) {
  const map = useMap()
  const fire = useCallback(() => { const b = map.getBounds(); onBounds({ lat_min: b.getSouth(), lat_max: b.getNorth(), lon_min: b.getWest(), lon_max: b.getEast() }) }, [map, onBounds])
  useMapEvents({ moveend: fire })
  useEffect(fire, [fire])
  return null
}

export default function MapPage() {
  const [bounds, setBounds] = useState<any>(null)
  const [vehicles, setVehicles] = useState<any[]>([])
  const [geofences, setGeofences] = useState<any>(null)
  const [selected, setSelected] = useState<any>(null)
  const [detail, setDetail] = useState<any>(null)
  const [sevFilter, setSevFilter] = useState('all')
  const [err, setErr] = useState('')
  const boundsRef = useRef<any>(null)
  boundsRef.current = bounds

  useEffect(() => { api('/api/v1/map/geofences').then(setGeofences).catch((e) => setErr(e.message)) }, [])
  useEffect(() => {
    const load = () => boundsRef.current && api('/api/v1/map/vehicles' + qs(boundsRef.current)).then((r) => { setVehicles(r.vehicles); setErr('') }).catch((e) => setErr(e.message))
    load(); const t = setInterval(load, 3000); return () => clearInterval(t)
  }, [bounds])
  useEffect(() => {
    if (!selected) { setDetail(null); return }
    api('/api/v1/vehicles' + qs({ q: selected.vin.slice(0, 17), limit: 1 })).then(async (l) => {
      if (l.items[0]) setDetail(await api(`/api/v1/vehicles/${l.items[0].id}`))
    }).catch(() => setDetail(null))
  }, [selected])

  const shown = vehicles.filter((v) => sevFilter === 'all' || v.severity === sevFilter)
  const center = geofences?.center ?? { lat: 41.88, lon: -87.63 }
  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Live fleet map</h1>
        <Badge tone="border-line text-gray-700">{shown.length} vehicles in view</Badge>
        {['all', 'critical', 'warning', 'ok'].map((s) => <button key={s} onClick={() => setSevFilter(s)} className={`rounded-sm border px-3 py-1 text-xs ${sevFilter === s ? 'bg-blue-50 border-brand text-brand' : 'border-line text-muted'}`}>{s}</button>)}
        {err && <span className="text-xs text-crit">{err}</span>}
        <span className="ml-auto text-xs text-muted">positions come from Redis geohash cells · refresh 3 s</span>
      </div>
      <div className="grid min-h-[520px] flex-1 gap-3 lg:grid-cols-[1fr_320px]">
        <MapContainer key={`${center.lat},${center.lon}`} center={[center.lat, center.lon]} zoom={9} className="h-full min-h-[520px] w-full">
          <TileLayer attribution="&copy; OpenStreetMap" url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
          <Viewport onBounds={setBounds} />
          {geofences?.items.map((g: any) => <Circle key={g.id} center={[g.center_lat, g.center_lon]} radius={g.radius_m}
            pathOptions={{ color: g.kind === 'restricted' ? '#b42318' : '#2f5d8a', weight: 1, fillOpacity: 0.05, dashArray: '6' }} />)}
          {shown.map((v) => <CircleMarker key={v.vin} center={[v.lat, v.lon]} radius={selected?.vin === v.vin ? 9 : 5}
            pathOptions={{ color: COLOR[v.severity], fillColor: COLOR[v.severity], fillOpacity: 0.8, weight: selected?.vin === v.vin ? 3 : 1 }}
            eventHandlers={{ click: () => setSelected(v) }} />)}
        </MapContainer>
        <div className="card overflow-y-auto text-sm">
          {!selected ? <div className="text-muted">Click a vehicle for details. Colours: <span className="text-ok">healthy</span>, <span className="text-warn">degraded</span>, <span className="text-crit">critical</span>. Dashed circles are geofences (red = restricted).</div> : (
            <div className="space-y-2">
              <div className="font-mono text-xs">{selected.vin}</div>
              <div className="flex gap-2"><Badge tone={sevColor(selected.severity === 'ok' ? 'info' : selected.severity)}>{selected.status}</Badge><span className={`font-semibold ${healthTone(selected.health)}`}>health {selected.health}</span></div>
              <div className="text-xs text-muted">speed {selected.speed_kmh} km/h · driver risk {selected.risk}</div>
              {detail && <>
                <div className="text-xs">{detail.vehicle.make} {detail.vehicle.model} {detail.vehicle.model_year} · {detail.vehicle.fleet}</div>
                {detail.prediction && <div className="text-xs">14-day failure risk <b className="text-warn">{Math.round(Number(detail.prediction.failure_prob) * 100)}%</b></div>}
                {detail.recent_alerts.slice(0, 3).map((a: any) => <div key={a.id} className="rounded border border-line p-2 text-xs"><SevBadge s={a.severity} /> {a.cause}</div>)}
                <Link to={`/vehicles/${detail.vehicle.id}`} className="btn mt-2 w-full">Open vehicle</Link></>}
            </div>)}
        </div>
      </div>
    </div>
  )
}
