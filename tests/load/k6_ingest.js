// k6 load test for the IoT edge: POST batches of telemetry to /v1/telemetry.
//   k6 run -e BASE=http://localhost:8001 -e KEY=dev-device-key-change-me -e RATE=100 -e BATCH=1000 tests/load/k6_ingest.js
// Throughput = RATE iterations/s x BATCH events. 100 it/s x 1000 = 100,000 events/s (needs several k6/edge replicas; see docs/testing.md).
// The 3x burst scenario multiplies RATE for 5 minutes after the steady phase, as required by the NFRs.
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const BASE = __ENV.BASE || 'http://localhost:8001';
const KEY = __ENV.KEY || 'dev-device-key-change-me';
const RATE = parseInt(__ENV.RATE || '20');
const BATCH = parseInt(__ENV.BATCH || '500');
const STEADY = __ENV.STEADY || '5m';
const BURST = __ENV.BURST || '5m';

const accepted = new Counter('events_accepted');
const backpressure = new Counter('backpressure_503');
const batchLatency = new Trend('batch_latency_ms', true);

export const options = {
  scenarios: {
    steady: { executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: STEADY, preAllocatedVUs: 50, maxVUs: 500, exec: 'send' },
    burst3x: { executor: 'constant-arrival-rate', rate: RATE * 3, timeUnit: '1s', duration: BURST, startTime: STEADY, preAllocatedVUs: 150, maxVUs: 1500, exec: 'send' },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<200', 'p(99)<500'],
  },
};

const FUELS = ['diesel', 'petrol', 'hybrid'];
let seq = 0;

// Same algorithm as cvi.algorithms.vin.make_vin (ISO 3779 check digit): a fleet of N distinct, valid VINs spreads load
// over all Kafka partitions (partition key = VIN) instead of hammering one.
const ALPHABET = 'ABCDEFGHJKLMNPRSTUVWXYZ0123456789';
const WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2];
const TRANSLIT = {};
'AJ,BKS,CLT,DMU,ENV,FW,GPX,HY,RZ'.split(',').forEach((cs, idx) => { cs.split('').forEach((c) => { TRANSLIT[c] = idx + 1; }); });
for (let d = 0; d < 10; d++) TRANSLIT[String(d)] = d;
const FLEET = parseInt(__ENV.VEHICLES || '100000');

function vin(i) {
  const serial = (__VU * 7919 + i * 104729 + seq) % FLEET;
  let body = '1FT';
  for (let k = 0; k < 5; k++) body += ALPHABET[(serial * 7 + k * 3) % ALPHABET.length];
  body += '0RA' + String(serial % 1000000).padStart(6, '0');
  let sum = 0;
  for (let k = 0; k < 17; k++) sum += TRANSLIT[body[k]] * WEIGHTS[k];
  const r = sum % 11;
  return body.slice(0, 8) + (r === 10 ? 'X' : String(r)) + body.slice(9);
}

export function send() {
  const now = Date.now();
  const events = [];
  for (let i = 0; i < BATCH; i++) {
    seq += 1;
    events.push({
      schema_version: 1, event_id: `${__VU}-${__ITER}-${i}-${now}`, tenant: 'acme-logistics', vin: vin(i), seq: seq, ts: now,
      lat: 41.88 + Math.random() * 0.1, lon: -87.63 + Math.random() * 0.1, speed_kmh: Math.random() * 100, odometer_km: 1000 + seq * 0.01,
      fuel_type: FUELS[0], rpm: 1500 + Math.random() * 1500, coolant_temp_c: 85 + Math.random() * 10, voltage_v: 13.8,
    });
  }
  const res = http.post(`${BASE}/v1/telemetry`, JSON.stringify(events), { headers: { 'Content-Type': 'application/json', 'X-Device-Key': KEY } });
  batchLatency.add(res.timings.duration);
  if (res.status === 202) accepted.add(BATCH);
  if (res.status === 503) backpressure.add(1);
  check(res, { 'accepted (202) or back-pressured (503)': (r) => r.status === 202 || r.status === 503 });
}
