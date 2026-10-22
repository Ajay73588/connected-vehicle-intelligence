# Security

Threat model: `docs/security/stride-threat-model.md`. Scan evidence: `docs/security/scan-results.md`. Demo accounts and device key in
`.env.example` are **development values for synthetic data**; production secrets come from a secret manager.

## Controls implemented (and where)
| Area | Implementation | Code / test |
|---|---|---|
| Authentication | JWT access (15 min) + refresh (24 h, rotation + revocation); HS256 locally, **RS256 via external OIDC IdP** (`OIDC_JWKS_URL`, issuer/audience checked); `alg=none` rejected; bcrypt password hashes; uniform failure message; timing-equalised unknown user; 5-failure lockout | `api/security.py`, `api/routers/auth.py`; `test_jwt_validation_*`, `test_login_failures_*`, `test_refresh_token_rotation_*` |
| Authorisation (RBAC) | roles `admin`, `fleet_manager`, `analyst`, `viewer` → permissions (`vehicles:read`, `telemetry:read`, `location:precise`, `alerts:read/write`, `analytics:read`, `agent:use/act`, `demo:run`, `admin:audit/erasure`); a `require("perm")` dependency on every route; contract test proves all protected routes reject anonymous calls | `ROLE_PERMISSIONS`; `test_every_protected_route_requires_a_token`, `test_rbac_*` |
| Tenant isolation | explicit `tenant_id` filters + PostgreSQL **row-level security** (`FORCE`, non-owner role, per-transaction `app.tenant_id`) + composite FKs + tenant-scoped Redis/Parquet keys; 404 for foreign objects | migrations `0003`; `test_row_level_security_*`, `test_object_level_access_*`, BDD |
| API protection | Redis token-bucket rate limit (600/min/token or IP; fallback in-process), body limit 1 MB (413), strict Pydantic validation, bounded pagination (≤200), parameterised SQL only | `middleware.py`; `test_rate_limiting_*`, `test_sql_injection_attempts_are_inert` |
| Secure headers / CORS | `X-Content-Type-Options`, `X-Frame-Options: DENY`, HSTS, CSP `default-src 'none'`, `Cache-Control: no-store`, `Referrer-Policy`, `Permissions-Policy`; CORS allow-list (methods GET/POST/PATCH, Authorization/Content-Type headers, no credentials); nginx CSP for the SPA; `/docs` off in production | `test_security_headers_cors_and_error_hygiene` |
| Secrets | env-based settings; `.env` ignored; Kubernetes Secret placeholder + External Secrets from AWS Secrets Manager; Terraform generates random passwords; secret-shape scan in tests and Trivy in CI | `test_no_secrets_committed_*` |
| Encryption | TLS at ingress; Terraform: KMS-encrypted RDS/ElastiCache/MSK/DocumentDB/S3/Secrets, TLS in transit to every store; app supports `rediss://`, Kafka `SSL`, `sslmode=require` | `infrastructure/terraform` |
| Audit | `audit_log` (security-relevant actions, login success/failure, mutations, erasure, demo scenarios) and `agent_audit` (every agent step) — per-tenant SHA-256 hash chains, INSERT-only for the API role, chain verification endpoints | `test_audit_log_hash_chain_detects_tampering`, `test_agent_audit_*` |
| AI agent safety | input screening, tool permissions, argument validation, output scrubbing, grounding check, confirmation for writes, rate limit, audit | `docs/ai-agent.md` |
| Privacy | synthetic data only; `location:precise` permission (others see a ~5 km geohash cell centre); pseudonymised drivers (`licence_hash`); erasure workflow across all tiers; retention (7 d HOT raw, 730 d COLD max) | `test_precise_location_is_masked_*`, `test_vehicle_erasure_*`, `test_driver_erasure_*` |
| Supply chain / containers | non-root image (uid 10001), slim base, read-only root FS and dropped caps in Kubernetes, `pip-audit`, `npm audit`, Trivy image scan, SBOM-ready multi-stage builds | Dockerfiles, manifests, CI |

## OWASP API Security Top 10 (2023) mapping
| Risk | Status |
|---|---|
| API1 Broken object-level authorisation | tenant filter + RLS + 404; tested with cross-tenant IDs on vehicles, telemetry, maintenance, alerts |
| API2 Broken authentication | JWT validation, lockout, rotation, bcrypt; MFA/SSO via external OIDC IdP is the production path |
| API3 Broken object property-level authorisation | response models expose only needed columns (no password hashes; SMTP-like secrets absent); location masking per role |
| API4 Unrestricted resource consumption | rate limit, body/page limits, caches, agent limits, back-pressure at ingestion |
| API5 Broken function-level authorisation | permission per route; destructive agent tools need `agent:act` + confirmation; admin routes need `admin:*` |
| API6 Unrestricted access to sensitive business flows | login lockout, scenario injection restricted to `demo:run` and the owning tenant, erasure restricted to admins with audit |
| API7 SSRF | the API never fetches user-supplied URLs; only the configured JWKS URL and LLM endpoint are contacted |
| API8 Security misconfiguration | secure headers, CORS allow-list, `/docs` off in production, generic errors, hardened containers |
| API9 Improper inventory management | OpenAPI surface pinned by a contract snapshot (`tests/contract/openapi_surface.txt`) |
| API10 Unsafe consumption of APIs | outbound LLM answers are grounding-checked; telemetry from devices is schema-validated and size-limited |

## Known gaps (explicit)
* Device authentication uses one shared key in the demo; per-device certificates (mTLS) / MQTT TLS + ACLs are documented but not implemented.
* No MFA, password-reset or user-management UI/API (accounts are seeded); production should delegate identity to an OIDC provider.
* The materialised views have no RLS (queries filter `tenant_id`; the app role has SELECT only).
* TLS termination and certificate management are delegated to the ingress/load balancer; compose runs plain HTTP on localhost.
* DAST and container scanning run in CI only (Docker unavailable on the dev machine).
