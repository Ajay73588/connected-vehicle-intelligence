"""Central, environment-driven configuration (12-factor). No cloud-specific names live in application code:
object storage, queues and secrets are reached through generic endpoints/URLs."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    log_level: str = "INFO"

    # PostgreSQL (relational + pgvector)
    database_url: str = "postgresql://cvi_app:cvi_app_dev@localhost:5432/cvi"  # API role (RLS enforced)
    worker_database_url: str | None = None  # BYPASSRLS role for stream/lifecycle workers
    admin_database_url: str = "postgresql://cvi:cvi@localhost:5432/cvi"  # owner: migrations + seed
    app_db_password: str = "cvi_app_dev"
    worker_db_password: str = "cvi_worker_dev"
    vector_backend: str = "pgvector"  # pgvector | python (client-side cosine; only for hosts where the pgvector build cannot run)
    skip_vector_index: bool = False
    db_pool_min: int = 1
    db_pool_max: int = 10

    # Redis (latest state, dedupe, aggregates, rate limit, cache)
    redis_url: str = "redis://localhost:6379/0"

    # MongoDB (raw telemetry documents, DLQ documents)
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "cvi"
    raw_events_ttl_days: int = 7  # HOT tier retention

    # Streaming
    broker: str = "kafka"  # kafka | memory
    kafka_bootstrap: str = "localhost:9092"
    kafka_security_protocol: str = "PLAINTEXT"  # SSL for managed clusters (e.g. Amazon MSK on :9094)
    topic_partitions: int = 12
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883

    # Object storage (S3-compatible: AWS S3, GCS interop, Azure via gateway, MinIO) or local dir
    object_store: str = "s3"  # s3 | local
    s3_endpoint: str | None = None
    s3_bucket: str = "cvi-telemetry"
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    local_object_dir: str = "./.data/objects"

    # Auth / security
    jwt_secret: str = Field(default="dev-only-change-me-dev-only-change-me", min_length=16)
    jwt_issuer: str = "cvi-auth"
    jwt_audience: str = "cvi-api"
    jwt_access_ttl_s: int = 900
    jwt_refresh_ttl_s: int = 86400
    oidc_jwks_url: str | None = None  # when set, RS256 tokens from an external IdP are accepted
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    rate_limit_per_minute: int = 600
    demo_password: str = "Demo@12345"  # synthetic demo accounts only

    # Stream processor
    allowed_lateness_s: int = 300
    batch_size: int = 500
    batch_timeout_s: float = 0.5
    max_retries: int = 3
    alert_cooldown_s: int = 300
    snapshot_interval_s: float = 15.0  # min seconds between PostgreSQL health/safety upserts per vehicle (Redis state is live)

    # Agent
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5-5"
    fuel_price_per_litre: float = 1.45
    idle_fuel_lph: float = 2.5

    # Ports
    api_port: int = 8000
    ingestion_port: int = 8001
    ingest_api_key: str = "dev-device-key-change-me"
    otlp_endpoint: str | None = None  # e.g. http://otel-collector:4318
    metrics_port_stream: int = 9101

    model_dir: str = "./ml/artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()
