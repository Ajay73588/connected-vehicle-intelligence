# Frontend

React 19 + TypeScript + Vite + Tailwind CSS 4 + Recharts + Leaflet.

```bash
npm ci
npm run dev        # http://localhost:5173  (proxies /api, /auth, /health to http://localhost:8000; override with VITE_PROXY_TARGET)
npm test           # Vitest
npm run build      # type-check + production build into dist/ (served by nginx in the Docker image)
```

Pages: Dashboard, Live map, Vehicles (+ detail), Alerts, Analytics, AI Copilot, Operations (scenarios, audit log, erasure).
Tokens are kept in `sessionStorage` and refreshed once on a 401; the nginx image proxies the API on the same origin, so no CORS surface is needed in production.
