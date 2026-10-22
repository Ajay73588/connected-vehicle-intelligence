-- 0001: core relational schema (3NF). Tenant consistency is enforced with composite foreign keys.

CREATE TABLE plans (
    code          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    max_vehicles  INTEGER NOT NULL CHECK (max_vehicles > 0),
    price_cents   INTEGER NOT NULL CHECK (price_cents >= 0)
);

CREATE TABLE tenants (
    id          BIGSERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    plan_code   TEXT NOT NULL REFERENCES plans(code),
    starts_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ends_at     TIMESTAMPTZ,
    CHECK (ends_at IS NULL OR ends_at > starts_at)
);

CREATE TABLE roles (
    code         TEXT PRIMARY KEY,
    description  TEXT NOT NULL
);

CREATE TABLE users (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email          TEXT NOT NULL UNIQUE,
    display_name   TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_roles (
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_code  TEXT NOT NULL REFERENCES roles(code),
    PRIMARY KEY (user_id, role_code)
);

CREATE TABLE fleets (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    UNIQUE (tenant_id, name),
    UNIQUE (id, tenant_id)
);

CREATE TABLE vehicle_models (
    id             SERIAL PRIMARY KEY,
    make           TEXT NOT NULL,
    model          TEXT NOT NULL,
    model_year     SMALLINT NOT NULL,
    fuel_type      TEXT NOT NULL CHECK (fuel_type IN ('diesel', 'petrol', 'ev', 'hybrid')),
    vehicle_class  TEXT NOT NULL,
    capacity       NUMERIC(8, 1) NOT NULL,          -- litres (ICE) or kWh (EV/hybrid)
    UNIQUE (make, model, model_year)
);

CREATE TABLE vehicles (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        BIGINT NOT NULL,
    fleet_id         BIGINT NOT NULL,
    model_id         INTEGER NOT NULL REFERENCES vehicle_models(id),
    vin              CHAR(17) NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'maintenance', 'retired')),
    commissioned_at  DATE NOT NULL DEFAULT CURRENT_DATE,
    erased_at        TIMESTAMPTZ,
    FOREIGN KEY (fleet_id, tenant_id) REFERENCES fleets(id, tenant_id),
    UNIQUE (id, tenant_id)
);

CREATE TABLE drivers (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    display_name  TEXT NOT NULL,                      -- synthetic / pseudonymised
    licence_hash  TEXT NOT NULL,                      -- salted hash, never the raw licence number
    erased_at     TIMESTAMPTZ,
    UNIQUE (id, tenant_id)
);

CREATE TABLE vehicle_assignments (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL,
    vehicle_id  BIGINT NOT NULL,
    driver_id   BIGINT NOT NULL,
    from_ts     TIMESTAMPTZ NOT NULL,
    to_ts       TIMESTAMPTZ,
    FOREIGN KEY (vehicle_id, tenant_id) REFERENCES vehicles(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (driver_id, tenant_id) REFERENCES drivers(id, tenant_id) ON DELETE CASCADE,
    CHECK (to_ts IS NULL OR to_ts > from_ts)
);

CREATE TABLE geofences (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('operating', 'restricted')),
    center_lat  DOUBLE PRECISION NOT NULL,
    center_lon  DOUBLE PRECISION NOT NULL,
    radius_m    INTEGER NOT NULL CHECK (radius_m > 0),
    UNIQUE (tenant_id, name)
);

CREATE TABLE alert_types (
    code              TEXT PRIMARY KEY,
    category          TEXT NOT NULL CHECK (category IN ('health', 'safety', 'fleet', 'battery', 'security')),
    title             TEXT NOT NULL,
    default_severity  TEXT NOT NULL CHECK (default_severity IN ('info', 'warning', 'critical'))
);

CREATE TABLE alerts (
    id                  BIGSERIAL PRIMARY KEY,
    tenant_id           BIGINT NOT NULL,
    vehicle_id          BIGINT NOT NULL,
    alert_type_code     TEXT NOT NULL REFERENCES alert_types(code),
    severity            TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    confidence          NUMERIC(4, 3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    cause               TEXT NOT NULL,
    recommended_action  TEXT NOT NULL,
    details             JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_ts            TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_by     BIGINT REFERENCES users(id) ON DELETE SET NULL,
    resolved_at         TIMESTAMPTZ,
    dedup_key           TEXT NOT NULL UNIQUE,         -- idempotent alert creation under replay
    FOREIGN KEY (vehicle_id, tenant_id) REFERENCES vehicles(id, tenant_id) ON DELETE CASCADE
);

-- CQRS read models maintained by the stream processor (current state per vehicle)
CREATE TABLE vehicle_health (
    vehicle_id     BIGINT PRIMARY KEY REFERENCES vehicles(id) ON DELETE CASCADE,
    tenant_id      BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    health_score   SMALLINT NOT NULL CHECK (health_score BETWEEN 0 AND 100),
    factors        JSONB NOT NULL DEFAULT '{}'::jsonb,        -- current penalty breakdown
    signals        JSONB NOT NULL DEFAULT '{}'::jsonb,        -- slow EWMAs of raw signals (ML features)
    last_event_ts  TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE vehicle_safety (
    vehicle_id          BIGINT PRIMARY KEY REFERENCES vehicles(id) ON DELETE CASCADE,
    tenant_id           BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    risk_score          SMALLINT NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    harsh_brake_30m     INTEGER NOT NULL DEFAULT 0,
    harsh_accel_30m     INTEGER NOT NULL DEFAULT 0,
    speeding_30m        INTEGER NOT NULL DEFAULT 0,
    idle_seconds_today  INTEGER NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE maintenance_records (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    vehicle_id    BIGINT NOT NULL,
    performed_at  DATE NOT NULL,
    kind          TEXT NOT NULL,
    cost_cents    INTEGER NOT NULL CHECK (cost_cents >= 0),
    odometer_km   NUMERIC(10, 1) NOT NULL,
    notes         TEXT,
    FOREIGN KEY (vehicle_id, tenant_id) REFERENCES vehicles(id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE maintenance_predictions (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      BIGINT NOT NULL,
    vehicle_id     BIGINT NOT NULL,
    model_version  TEXT NOT NULL,
    failure_prob   NUMERIC(5, 4) NOT NULL CHECK (failure_prob BETWEEN 0 AND 1),
    horizon_days   SMALLINT NOT NULL,
    top_factors    JSONB NOT NULL DEFAULT '[]'::jsonb,
    predicted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (vehicle_id, tenant_id) REFERENCES vehicles(id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE trips (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT NOT NULL,
    vehicle_id   BIGINT NOT NULL,
    trip_key     TEXT NOT NULL UNIQUE,
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    distance_km  NUMERIC(9, 2) NOT NULL DEFAULT 0,
    max_speed    NUMERIC(6, 1) NOT NULL DEFAULT 0,
    harsh_events INTEGER NOT NULL DEFAULT 0,
    idle_seconds INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (vehicle_id, tenant_id) REFERENCES vehicles(id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE daily_vehicle_stats (
    vehicle_id     BIGINT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    day            DATE NOT NULL,
    tenant_id      BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    events         INTEGER NOT NULL DEFAULT 0,
    distance_km    DOUBLE PRECISION NOT NULL DEFAULT 0,
    drive_seconds  INTEGER NOT NULL DEFAULT 0,
    idle_seconds   INTEGER NOT NULL DEFAULT 0,
    fuel_litres    DOUBLE PRECISION NOT NULL DEFAULT 0,
    energy_kwh     DOUBLE PRECISION NOT NULL DEFAULT 0,
    harsh_brake    INTEGER NOT NULL DEFAULT 0,
    harsh_accel    INTEGER NOT NULL DEFAULT 0,
    speeding       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (vehicle_id, day)
);

-- Tamper-evident audit trails (hash chains)
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT,
    actor_id    BIGINT,
    action      TEXT NOT NULL,
    resource    TEXT NOT NULL,
    resource_id TEXT,
    ip          TEXT,
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);

CREATE TABLE agent_audit (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    session_id  TEXT NOT NULL,
    step        TEXT NOT NULL CHECK (step IN ('question', 'intent', 'tool_call', 'tool_result', 'guardrail', 'answer', 'confirmation')),
    tool        TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    allowed     BOOLEAN NOT NULL DEFAULT TRUE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);

CREATE TABLE agent_pending_actions (
    token       TEXT PRIMARY KEY,
    tenant_id   BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    action      TEXT NOT NULL,
    params      JSONB NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ
);

CREATE TABLE erasure_requests (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    subject_type  TEXT NOT NULL CHECK (subject_type IN ('vehicle', 'driver')),
    subject_ref   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'failed')),
    requested_by  BIGINT REFERENCES users(id) ON DELETE SET NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ,
    report        JSONB NOT NULL DEFAULT '{}'::jsonb
);
