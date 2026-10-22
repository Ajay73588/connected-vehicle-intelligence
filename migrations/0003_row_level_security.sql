-- 0003: tenant isolation in the database itself (defence in depth on top of application-level filters).
-- The API sets `app.tenant_id` per transaction (set_config(..., true)). Workers connect with a BYPASSRLS role.

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users', 'fleets', 'vehicles', 'drivers', 'vehicle_assignments', 'geofences', 'alerts',
    'vehicle_health', 'vehicle_safety', 'maintenance_records', 'maintenance_predictions', 'trips',
    'daily_vehicle_stats', 'agent_audit', 'agent_pending_actions', 'erasure_requests', 'incident_embeddings'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::bigint) '
      'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::bigint)', t);
  END LOOP;
END $$;

-- knowledge docs: global (tenant_id NULL) readable by all, tenant docs only by their tenant
ALTER TABLE knowledge_docs ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_docs FORCE ROW LEVEL SECURITY;
CREATE POLICY knowledge_read ON knowledge_docs FOR SELECT
  USING (tenant_id IS NULL OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint);
CREATE POLICY knowledge_write ON knowledge_docs FOR ALL
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint);

-- audit_log: a tenant may read only its own rows; inserts are allowed for any tenant context
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_read ON audit_log FOR SELECT
  USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint);
CREATE POLICY audit_insert ON audit_log FOR INSERT WITH CHECK (true);
