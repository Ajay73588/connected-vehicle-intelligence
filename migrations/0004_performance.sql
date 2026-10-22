-- 0004: indexes and materialised views driven by the query analysis in docs/performance/sql-before-after.md

-- keyset-paginated alert feed per tenant (ORDER BY created_at DESC, id DESC)
CREATE INDEX alerts_tenant_created_idx ON alerts (tenant_id, created_at DESC, id DESC);
-- the hot dashboard path: only OPEN alerts, filtered by severity (partial index stays small)
CREATE INDEX alerts_open_severity_idx ON alerts (tenant_id, severity, created_at DESC, id DESC) WHERE status = 'open';
-- vehicle detail page
CREATE INDEX alerts_vehicle_idx ON alerts (vehicle_id, created_at DESC);

CREATE INDEX vehicles_tenant_status_idx ON vehicles (tenant_id, status, id);
CREATE INDEX vehicles_fleet_idx ON vehicles (tenant_id, fleet_id, id);

CREATE INDEX health_tenant_score_idx ON vehicle_health (tenant_id, health_score);
CREATE INDEX predictions_vehicle_idx ON maintenance_predictions (vehicle_id, predicted_at DESC);
CREATE INDEX predictions_tenant_prob_idx ON maintenance_predictions (tenant_id, failure_prob DESC);
CREATE INDEX trips_vehicle_idx ON trips (vehicle_id, started_at DESC);
CREATE INDEX daily_stats_tenant_day_idx ON daily_vehicle_stats (tenant_id, day);
CREATE INDEX audit_tenant_ts_idx ON audit_log (tenant_id, ts DESC);
CREATE INDEX agent_audit_session_idx ON agent_audit (tenant_id, session_id, id);
CREATE INDEX assignments_vehicle_idx ON vehicle_assignments (vehicle_id, from_ts DESC);

-- fleet KPIs per tenant per day (refreshed CONCURRENTLY by the lifecycle job)
CREATE MATERIALIZED VIEW mv_fleet_daily_kpis AS
SELECT tenant_id, day,
       count(DISTINCT vehicle_id)                    AS active_vehicles,
       sum(distance_km)                              AS distance_km,
       sum(drive_seconds)                            AS drive_seconds,
       sum(idle_seconds)                             AS idle_seconds,
       sum(fuel_litres)                              AS fuel_litres,
       sum(energy_kwh)                               AS energy_kwh,
       sum(harsh_brake + harsh_accel + speeding)     AS safety_events
FROM daily_vehicle_stats
GROUP BY tenant_id, day;
CREATE UNIQUE INDEX mv_fleet_daily_kpis_pk ON mv_fleet_daily_kpis (tenant_id, day);

-- latest prediction per vehicle; security_invoker keeps row-level security of the caller in force
CREATE VIEW latest_maintenance_predictions WITH (security_invoker = true) AS
SELECT DISTINCT ON (vehicle_id) id, tenant_id, vehicle_id, model_version, failure_prob, horizon_days, top_factors, predicted_at
FROM maintenance_predictions
ORDER BY vehicle_id, predicted_at DESC, id DESC;

CREATE INDEX maintenance_records_vehicle_idx ON maintenance_records (vehicle_id, performed_at DESC);
