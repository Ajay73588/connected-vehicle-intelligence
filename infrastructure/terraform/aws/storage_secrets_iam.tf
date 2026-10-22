# ---------------------------------------------------------------------------------------------------------------------
# Object storage: telemetry Parquet (WARM hourly files, COLD daily files). Lifecycle rules enforce the retention policy
# documented in docs/database.md even if the lifecycle service is down.
# ---------------------------------------------------------------------------------------------------------------------
resource "aws_s3_bucket" "telemetry" {
  bucket_prefix = "${var.name}-${var.environment}-telemetry-"
  force_destroy = !var.deletion_protection
}

resource "aws_s3_bucket_public_access_block" "telemetry" {
  bucket                  = aws_s3_bucket.telemetry.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "telemetry" {
  bucket = aws_s3_bucket.telemetry.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "telemetry" {
  bucket = aws_s3_bucket.telemetry.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "telemetry" {
  bucket = aws_s3_bucket.telemetry.id

  rule {
    id     = "warm-expiry"
    status = "Enabled"
    filter { prefix = "telemetry/tier=warm/" }
    expiration { days = var.telemetry_warm_retention_days }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 2 }
  }

  rule {
    id     = "cold-tiering"
    status = "Enabled"
    filter { prefix = "telemetry/tier=cold/" }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 180
      storage_class = "GLACIER_IR"
    }
    expiration { days = var.telemetry_cold_retention_days }
    noncurrent_version_expiration { noncurrent_days = 7 }
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# Secrets: generated here, stored in Secrets Manager, synced into Kubernetes by External Secrets (overlays/aws).
# ---------------------------------------------------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.name}/${var.environment}/app"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    DATABASE_URL        = "postgresql://cvi_app:${random_password.db_app.result}@${aws_db_instance.postgres.address}:5432/cvi?sslmode=require"
    WORKER_DATABASE_URL = "postgresql://cvi_worker:${random_password.db_worker.result}@${aws_db_instance.postgres.address}:5432/cvi?sslmode=require"
    ADMIN_DATABASE_URL  = "postgresql://cvi:${random_password.db_owner.result}@${aws_db_instance.postgres.address}:5432/cvi?sslmode=require"
    APP_DB_PASSWORD     = random_password.db_app.result
    WORKER_DB_PASSWORD  = random_password.db_worker.result
    MONGO_URL           = "mongodb://cvi:${random_password.docdb.result}@${aws_docdb_cluster.docdb.endpoint}:27017/?tls=true&retryWrites=false&tlsCAFile=/etc/ssl/certs/ca-certificates.crt"
    JWT_SECRET          = random_password.jwt.result
    INGEST_API_KEY      = random_password.ingest_key.result
  })
}

# ---------------------------------------------------------------------------------------------------------------------
# IAM: the application's service account (IRSA) may use only this bucket, this key and this one secret. No static keys.
# ---------------------------------------------------------------------------------------------------------------------
data "aws_iam_policy_document" "irsa_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:cvi:cvi"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "${var.name}-app"
  assume_role_policy = data.aws_iam_policy_document.irsa_assume.json
}

data "aws_iam_policy_document" "app" {
  statement {
    sid       = "TelemetryObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.telemetry.arn}/*"]
  }
  statement {
    sid       = "TelemetryBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.telemetry.arn]
  }
  statement {
    sid       = "UseDataKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.data.arn]
  }
}

resource "aws_iam_policy" "app" {
  name   = "${var.name}-app"
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_role_policy_attachment" "app" {
  role       = aws_iam_role.app.name
  policy_arn = aws_iam_policy.app.arn
}

# External Secrets reads exactly one secret.
data "aws_iam_policy_document" "eso_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:external-secrets:external-secrets"]
    }
  }
}

resource "aws_iam_role" "external_secrets" {
  name               = "${var.name}-external-secrets"
  assume_role_policy = data.aws_iam_policy_document.eso_assume.json
}

resource "aws_iam_role_policy" "external_secrets" {
  role = aws_iam_role.external_secrets.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"], Resource = aws_secretsmanager_secret.app.arn },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.data.arn }
    ]
  })
}
