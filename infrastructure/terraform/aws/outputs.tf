output "cluster_name" {
  value = module.eks.cluster_name
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "kafka_bootstrap_tls" {
  value = aws_msk_cluster.kafka.bootstrap_brokers_tls
}

output "redis_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "postgres_endpoint" {
  value = aws_db_instance.postgres.address
}

output "docdb_endpoint" {
  value = aws_docdb_cluster.docdb.endpoint
}

output "telemetry_bucket" {
  value = aws_s3_bucket.telemetry.bucket
}

output "app_role_arn" {
  value = aws_iam_role.app.arn
}

output "external_secrets_role_arn" {
  value = aws_iam_role.external_secrets.arn
}

output "secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}
