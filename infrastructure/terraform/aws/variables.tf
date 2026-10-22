variable "region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "name" {
  type    = string
  default = "cvi"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "az_count" {
  type    = number
  default = 3
}

variable "eks_version" {
  type    = string
  default = "1.30"
}

variable "node_instance_types" {
  type    = list(string)
  default = ["m6i.xlarge"]
}

variable "node_min" {
  type    = number
  default = 3
}

variable "node_max" {
  type    = number
  default = 12
}

variable "db_instance_class" {
  type    = string
  default = "db.r6g.xlarge"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 200
}

variable "redis_node_type" {
  type    = string
  default = "cache.r6g.large"
}

variable "msk_broker_instance_type" {
  type    = string
  default = "kafka.m5.large"
}

variable "msk_broker_volume_gb" {
  type    = number
  default = 500
}

variable "docdb_instance_class" {
  type    = string
  default = "db.r6g.large"
}

variable "alert_email" {
  description = "Subscribed to the SNS topic that receives CloudWatch alarms."
  type        = string
  default     = ""
}

variable "telemetry_warm_retention_days" {
  type    = number
  default = 45
}

variable "telemetry_cold_retention_days" {
  type    = number
  default = 730
}

variable "deletion_protection" {
  description = "Set false only for throw-away environments."
  type        = bool
  default     = true
}
