-- 0005: login must find a user before any tenant context exists, so it goes through a narrow
-- SECURITY DEFINER function (runs as the owner, bypassing RLS) instead of widening table access.
CREATE FUNCTION auth_lookup(p_email TEXT)
RETURNS TABLE (id BIGINT, tenant_id BIGINT, tenant_slug TEXT, email TEXT, display_name TEXT,
               password_hash TEXT, is_active BOOLEAN, roles TEXT[])
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT u.id, u.tenant_id, t.slug, u.email, u.display_name, u.password_hash, u.is_active,
         COALESCE(array_agg(ur.role_code) FILTER (WHERE ur.role_code IS NOT NULL), '{}')
  FROM users u
  JOIN tenants t ON t.id = u.tenant_id
  LEFT JOIN user_roles ur ON ur.user_id = u.id
  WHERE u.email = lower(p_email) AND t.status = 'active'
  GROUP BY u.id, t.slug;
$$;
REVOKE ALL ON FUNCTION auth_lookup(TEXT) FROM PUBLIC;
