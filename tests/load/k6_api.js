// k6 load test for the REST API (targets: p95 < 200 ms, p99 < 500 ms).
//   k6 run -e BASE=http://localhost:8000 -e VUS=50 tests/load/k6_api.js
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE || 'http://localhost:8000';

export const options = {
  stages: [
    { duration: '30s', target: parseInt(__ENV.VUS || '50') },
    { duration: __ENV.HOLD || '3m', target: parseInt(__ENV.VUS || '50') },
    { duration: '15s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{endpoint:vehicles}': ['p(95)<200', 'p(99)<500'],
    'http_req_duration{endpoint:alerts}': ['p(95)<200', 'p(99)<500'],
    'http_req_duration{endpoint:summary}': ['p(95)<200', 'p(99)<500'],
  },
};

export function setup() {
  const r = http.post(`${BASE}/auth/login`, JSON.stringify({ email: __ENV.EMAIL || 'fleet_manager@acme.demo', password: __ENV.PASSWORD || 'Demo@12345' }),
    { headers: { 'Content-Type': 'application/json' } });
  return { token: r.json('access_token') };
}

export default function (data) {
  const h = { headers: { Authorization: `Bearer ${data.token}` } };
  const v = http.get(`${BASE}/api/v1/vehicles?limit=50`, { ...h, tags: { endpoint: 'vehicles' } });
  check(v, { 'vehicles 200': (r) => r.status === 200 });
  const a = http.get(`${BASE}/api/v1/alerts?status=open&limit=50`, { ...h, tags: { endpoint: 'alerts' } });
  check(a, { 'alerts 200': (r) => r.status === 200 });
  const s = http.get(`${BASE}/api/v1/analytics/summary`, { ...h, tags: { endpoint: 'summary' } });
  check(s, { 'summary 200': (r) => r.status === 200 });
  sleep(1);
}
