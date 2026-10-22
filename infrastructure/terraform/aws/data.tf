# ---------------------------------------------------------------------------------------------------------------------
# Managed data tier. Every store is encrypted at rest (KMS) and in transit, Multi-AZ, and private.
#   PostgreSQL (RDS, pgvector)  -> relational + vector      ElastiCache Redis -> hot state / dedupe / rate limit
#   MSK (Kafka)                 -> durable event log         DocumentDB        -> MongoDB-compatible raw telemetry (HOT, TTL)
# ---------------------------------------------------------------------------------------------------------------------
resource "aws_kms_key" "data" {
  description             = "${var.name} data-at-rest key"
  enable_key_rotation     = true
  deletion_window_in_days = 14
}

resource "aws_kms_alias" "data" {
  name          = "alias/${var.name}-${var.environment}-data"
  target_key_id = aws_kms_key.data.key_id
}

resource "random_password" "db_owner" {
  length  = 32
  special = false
}
resource "random_password" "db_app" {
  length  = 32
  special = false
}
resource "random_password" "db_worker" {
  length  = 32
  special = false
}
resource "random_password" "docdb" {
  length  = 32
  special = false
}
resource "random_password" "jwt" {
  length  = 64
  special = false
}
resource "random_password" "ingest_key" {
  length  = 40
  special = false
}

# ---- PostgreSQL ----------------------------------------------------------------------------------------------------
resource "aws_db_parameter_group" "pg" {
  name_prefix = "${var.name}-pg16-"
  family      = "postgres16"
  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
  lifecycle { create_before_destroy = true }
}

resource "aws_db_instance" "postgres" {
  identifier                   = "${var.name}-${var.environment}"
  engine                       = "postgres"
  engine_version               = "16.4"
  instance_class               = var.db_instance_class
  allocated_storage            = var.db_allocated_storage_gb
  max_allocated_storage        = var.db_allocated_storage_gb * 4
  storage_type                 = "gp3"
  storage_encrypted            = true
  kms_key_id                   = aws_kms_key.data.arn
  db_name                      = "cvi"
  username                     = "cvi"
  password                     = random_password.db_owner.result
  multi_az                     = true # synchronous standby in another AZ
  db_subnet_group_name         = module.vpc.database_subnet_group_name
  vpc_security_group_ids       = [aws_security_group.data.id]
  parameter_group_name         = aws_db_parameter_group.pg.name
  backup_retention_period      = 14
  backup_window                = "02:00-03:00"
  maintenance_window           = "sun:03:30-sun:04:30"
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = !var.deletion_protection
  final_snapshot_identifier    = var.deletion_protection ? "${var.name}-${var.environment}-final" : null
  performance_insights_enabled = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  auto_minor_version_upgrade   = true
  copy_tags_to_snapshot        = true
}

resource "aws_db_instance" "postgres_replica" { # read replica: scales read-heavy analytics, second failover target
  identifier             = "${var.name}-${var.environment}-replica"
  replicate_source_db    = aws_db_instance.postgres.identifier
  instance_class         = var.db_instance_class
  storage_encrypted      = true
  kms_key_id             = aws_kms_key.data.arn
  vpc_security_group_ids = [aws_security_group.data.id]
  skip_final_snapshot    = true
  parameter_group_name   = aws_db_parameter_group.pg.name
}

# ---- Redis ---------------------------------------------------------------------------------------------------------
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.name}-${var.environment}"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${var.name}-${var.environment}"
  description                = "Hot vehicle state, dedupe sets, aggregates, rate limits"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.data.id]
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.data.arn
  transit_encryption_enabled = true
  snapshot_retention_limit   = 3
}

# ---- Kafka (MSK) ---------------------------------------------------------------------------------------------------
resource "aws_msk_configuration" "kafka" {
  name           = "${var.name}-${var.environment}"
  kafka_versions = ["3.6.0"]
  server_properties = <<-PROPS
    auto.create.topics.enable=false
    default.replication.factor=3
    min.insync.replicas=2
    num.partitions=48
    log.retention.hours=24
    unclean.leader.election.enable=false
  PROPS
}

resource "aws_msk_cluster" "kafka" {
  cluster_name           = "${var.name}-${var.environment}"
  kafka_version          = "3.6.0"
  number_of_broker_nodes = var.az_count # one broker per AZ => replication factor 3 survives an AZ loss

  broker_node_group_info {
    instance_type   = var.msk_broker_instance_type
    client_subnets  = module.vpc.private_subnets
    security_groups = [aws_security_group.data.id]
    storage_info {
      ebs_storage_info { volume_size = var.msk_broker_volume_gb }
    }
  }
  configuration_info {
    arn      = aws_msk_configuration.kafka.arn
    revision = aws_msk_configuration.kafka.latest_revision
  }
  encryption_info {
    encryption_at_rest_kms_key_arn = aws_kms_key.data.arn
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }
  enhanced_monitoring = "PER_TOPIC_PER_PARTITION" # exposes consumer-group lag to CloudWatch
  open_monitoring {
    prometheus {
      jmx_exporter { enabled_in_broker = true }
      node_exporter { enabled_in_broker = true }
    }
  }
}

# ---- DocumentDB (MongoDB API) --------------------------------------------------------------------------------------
resource "aws_docdb_subnet_group" "docdb" {
  name       = "${var.name}-${var.environment}"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_docdb_cluster" "docdb" {
  cluster_identifier      = "${var.name}-${var.environment}"
  engine                  = "docdb"
  master_username         = "cvi"
  master_password         = random_password.docdb.result
  db_subnet_group_name    = aws_docdb_subnet_group.docdb.name
  vpc_security_group_ids  = [aws_security_group.data.id]
  storage_encrypted       = true
  kms_key_id              = aws_kms_key.data.arn
  backup_retention_period = 7
  deletion_protection     = var.deletion_protection
  skip_final_snapshot     = !var.deletion_protection
}

resource "aws_docdb_cluster_instance" "docdb" {
  count              = 2
  identifier         = "${var.name}-${var.environment}-${count.index}"
  cluster_identifier = aws_docdb_cluster.docdb.id
  instance_class     = var.docdb_instance_class
}
