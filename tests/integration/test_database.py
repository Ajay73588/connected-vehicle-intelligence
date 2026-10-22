"""Real PostgreSQL: migrations, constraints, row-level security, hash-chained audit, idempotent sinks."""

import datetime as dt

import psycopg
import pytest
from helpers import MemorySink  # noqa: F401  (keeps helper import side effects consistent)
from psycopg.rows import dict_row

from cvi.storage.pg import append_audit, migrate, tenant_tx, verify_chain
from cvi.stream.pg_sink import PgSink

pytestmark = pytest.mark.integration


def admin(seeded_db):
    return psycopg.connect(seeded_db["admin"], row_factory=dict_row, autocommit=True)


def tenant_ids(seeded_db):
    with admin(seeded_db) as c:
        return {r["slug"]: r["id"] for r in c.execute("SELECT slug, id FROM tenants")}


def test_migrations_are_idempotent_and_roles_are_least_privilege(seeded_db):
    assert migrate(seeded_db["admin"]) == []  # nothing pending on a second run
    with admin(seeded_db) as c:
        r = c.execute("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('cvi_app','cvi_worker') ORDER BY 1").fetchall()
        assert [(x["rolname"], x["rolsuper"], x["rolbypassrls"]) for x in r] == [("cvi_app", False, False), ("cvi_worker", False, True)]
        names = {x["name"] for x in c.execute("SELECT name FROM schema_migrations")}
        assert {"0001_core_schema.sql", "0003_row_level_security.sql", "0004_performance.sql"} <= names


def test_seed_loaded_expected_shape(seeded_db):
    with admin(seeded_db) as c:
        assert c.execute("SELECT count(*) AS n FROM vehicles").fetchone()["n"] == 600
        assert c.execute("SELECT count(DISTINCT tenant_id) AS n FROM vehicles").fetchone()["n"] == 3
        live = c.execute("SELECT count(*) AS n FROM vehicles WHERE erased_at IS NULL").fetchone()["n"]
        assert c.execute("SELECT count(DISTINCT vehicle_id) AS n FROM maintenance_predictions").fetchone()["n"] == live  # every live vehicle is scored
        assert c.execute("SELECT count(*) AS n FROM user_roles").fetchone()["n"] == 12
        assert c.execute("SELECT count(*) AS n FROM knowledge_docs").fetchone()["n"] >= 10
        assert c.execute("SELECT count(*) AS n FROM vehicles WHERE vin !~ '^[A-HJ-NPR-Z0-9]{17}$'").fetchone()["n"] == 0


def test_row_level_security_isolates_tenants(ctx, seeded_db):
    t = tenant_ids(seeded_db)
    with ctx.pool.connection() as c:  # app role, NO tenant context -> sees nothing
        assert c.execute("SELECT count(*) AS n FROM vehicles").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM alerts").fetchone()["n"] == 0
    for slug, tid in t.items():
        with tenant_tx(ctx.pool, tid) as c:
            tenants_seen = {r["tenant_id"] for r in c.execute("SELECT DISTINCT tenant_id FROM vehicles")}
            assert tenants_seen == {tid}, slug
            # even an explicit attempt to read another tenant's rows returns nothing
            other = next(v for k, v in t.items() if v != tid)
            assert c.execute("SELECT count(*) AS n FROM vehicles WHERE tenant_id = %s", [other]).fetchone()["n"] == 0


def test_rls_blocks_cross_tenant_writes(ctx, seeded_db):
    t = tenant_ids(seeded_db)
    a, b = t["acme-logistics"], t["borealis-ev"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_tx(ctx.pool, a) as c:
        c.execute("INSERT INTO fleets (tenant_id, name) VALUES (%s, 'sneaky')", [b])
    with tenant_tx(ctx.pool, a) as c:  # UPDATE/DELETE silently affect zero foreign rows
        assert c.execute("UPDATE vehicles SET status = 'retired' WHERE tenant_id = %s", [b]).rowcount == 0
        assert c.execute("DELETE FROM alerts WHERE tenant_id = %s", [b]).rowcount == 0


def test_composite_foreign_key_prevents_cross_tenant_vehicle_fleet_mix(seeded_db):
    with admin(seeded_db) as c:
        t = tenant_ids(seeded_db)
        fleet_b = c.execute("SELECT id FROM fleets WHERE tenant_id = %s LIMIT 1", [t["borealis-ev"]]).fetchone()["id"]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute("INSERT INTO vehicles (tenant_id, fleet_id, model_id, vin) VALUES (%s, %s, 1, '1FTAAAAAAAAAAAAAA')", [t["acme-logistics"], fleet_b])


def test_check_constraints(seeded_db):
    with admin(seeded_db) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("UPDATE vehicle_health SET health_score = 150 WHERE vehicle_id = (SELECT min(vehicle_id) FROM vehicle_health)")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO tenants (slug, name) VALUES ('Bad Slug!', 'x')")


def test_audit_log_hash_chain_detects_tampering(ctx, seeded_db):
    tid = tenant_ids(seeded_db)["metro-transit"]
    with tenant_tx(ctx.pool, tid) as c:
        for i in range(4):
            append_audit(c, "audit_log", tid, {"actor_id": None, "action": f"test{i}", "resource": "x", "resource_id": str(i), "ip": None,
                                               "details": {"n": i}})
    with tenant_tx(ctx.pool, tid) as c:
        ok, n = verify_chain(c, "audit_log", tid)
        assert ok and n >= 4
    with admin(seeded_db) as c:  # an attacker with DBA rights edits history...
        c.execute("UPDATE audit_log SET details = '{\"n\": 999}'::jsonb WHERE tenant_id = %s AND action = 'test1'", [tid])
    with tenant_tx(ctx.pool, tid) as c:
        assert verify_chain(c, "audit_log", tid)[0] is False  # ...and the chain proves it
    with admin(seeded_db) as c:
        c.execute("UPDATE audit_log SET details = '{\"n\": 1}'::jsonb WHERE tenant_id = %s AND action = 'test1'", [tid])
    with tenant_tx(ctx.pool, tid) as c:
        assert verify_chain(c, "audit_log", tid)[0]


def test_api_role_cannot_modify_or_delete_audit_rows(ctx, seeded_db):
    tid = tenant_ids(seeded_db)["acme-logistics"]
    with tenant_tx(ctx.pool, tid) as c:
        append_audit(c, "audit_log", tid, {"actor_id": None, "action": "probe", "resource": "x", "resource_id": None, "ip": None, "details": {}})
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_tx(ctx.pool, tid) as c:
        c.execute("UPDATE audit_log SET action = 'forged'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tenant_tx(ctx.pool, tid) as c:
        c.execute("DELETE FROM agent_audit")


def test_pgsink_alert_insert_is_idempotent_and_embeds_incident(ctx, seeded_db):
    sink = PgSink(ctx.worker_pool)
    with admin(seeded_db) as c:
        v = c.execute("SELECT v.id, v.tenant_id, v.vin FROM vehicles v LIMIT 1").fetchone()
    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    row = {"tenant_id": v["tenant_id"], "vehicle_id": v["id"], "vin": v["vin"], "type": "engine_overheating", "severity": "critical",
           "confidence": 0.9, "cause": "test cause", "recommended_action": "test action", "details": {"x": 1}, "event_ts": now_ms,
           "ingest_ts": now_ms, "dedup_key": f"it:{now_ms}"}
    assert len(sink.persist_alerts([row])) == 1
    assert sink.persist_alerts([row]) == []  # replay -> no duplicate
    with admin(seeded_db) as c:
        assert c.execute("SELECT count(*) AS n FROM alerts WHERE dedup_key = %s", [row["dedup_key"]]).fetchone()["n"] == 1
        assert c.execute("SELECT count(*) AS n FROM incident_embeddings e JOIN alerts a ON a.id = e.alert_id WHERE a.dedup_key = %s",
                         [row["dedup_key"]]).fetchone()["n"] == 1


def test_daily_stats_flush_is_absolute_so_replays_do_not_double_count(ctx, seeded_db):
    sink = PgSink(ctx.worker_pool)
    with admin(seeded_db) as c:
        v = c.execute("SELECT id, tenant_id FROM vehicles LIMIT 1").fetchone()
    r = {"vehicle_id": v["id"], "tenant_id": v["tenant_id"], "day": "2030-01-01", "events": 10, "distance_km": 5.5, "drive_seconds": 100,
         "idle_seconds": 20, "fuel_litres": 1.0, "energy_kwh": 0.0, "harsh_brake": 1, "harsh_accel": 0, "speeding": 2}
    sink.flush_daily([r])
    sink.flush_daily([r])
    with admin(seeded_db) as c:
        row = c.execute("SELECT events, distance_km FROM daily_vehicle_stats WHERE vehicle_id = %s AND day = '2030-01-01'", [v["id"]]).fetchone()
    assert row == {"events": 10, "distance_km": 5.5}


def test_keyset_query_uses_the_composite_index(ctx, seeded_db):
    tid = tenant_ids(seeded_db)["acme-logistics"]
    with admin(seeded_db) as c:
        c.execute("SET enable_seqscan = off")  # tiny test table: ask the planner which index path it would take at scale
        c.execute("SET enable_bitmapscan = off")
        plan = "\n".join(r["QUERY PLAN"] for r in c.execute(
            "EXPLAIN SELECT id FROM alerts WHERE tenant_id = %s AND status = 'open' AND severity = 'critical' "
            "ORDER BY created_at DESC, id DESC LIMIT 50", [tid]))
    assert "alerts_open_severity_idx" in plan and "Sort" not in plan  # partial index delivers the order directly
